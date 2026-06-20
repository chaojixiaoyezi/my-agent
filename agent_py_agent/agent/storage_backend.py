"""存储后端抽象(Tier 0.1 企业规模化):SQLAlchemy Core,同一仓储代码跑 SQLite(本地/开发
默认)或 PostgreSQL(10k–100k 并发规模),零代码改。

借库取舍(用户原则"能自建就自建、不能的才借库" + "稳定性第一"):多 dialect 差异(自增/参数
风格/UPSERT/事务行为)难以自建得稳,SQLAlchemy Core 自动处理、且 claw 已生产验证 → 正当借库,
设为可选 `scale` extra(不装则用现有 raw-sqlite3 local_storage,本模块不影响现状)。

存储层并发和连接生命周期约束：
- SQLite 写事务发 ``BEGIN IMMEDIATE``:起步即拿写锁,避免 WAL 下"先读后写"升级写锁时快照陈旧
  → SQLite 不等 busy_timeout 直接 "database is locked" 崩。
- ``busy_timeout`` **最先**设:WAL 切换要排他锁,若另一连接持锁而 timeout 还是 0 → connect 钩子里直接崩、实例起不来。
- WAL 切换对并发首启的瞬时锁竞争做小退避重试。

约束:仓储代码须用 SQLAlchemy Core 构造,**不写 dialect 专有 SQL 串**,才能两端通跑。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    from sqlalchemy import create_engine, event, inspect, text
    from sqlalchemy.engine import Connection, Engine

    _HAS_SQLALCHEMY = True
except ImportError:  # 不装 SQLAlchemy 也能 import 本模块(构造 StorageBackend 时才报错)
    _HAS_SQLALCHEMY = False


def sqlite_url(path: str | Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def _require_sqlalchemy() -> None:
    if not _HAS_SQLALCHEMY:
        raise RuntimeError(
            "存储后端抽象(PostgreSQL/规模路径)需 SQLAlchemy:pip install 'my-agent[scale]'"
            "(stdlib 无 dialect 抽象、不能自建得稳)"
        )


class StorageBackend:
    """一个 SQLAlchemy engine + 连接/事务助手。SQLite=本地默认,PostgreSQL=规模(同代码)。

    用法::

        db = StorageBackend.for_path(home / "store.db")      # SQLite
        db = StorageBackend("postgresql+psycopg://host/db")   # PostgreSQL(规模)
        with db.begin() as conn:    # 写事务(SQLite 自动 BEGIN IMMEDIATE)
            conn.execute(text("INSERT ..."), {...})
        with db.connect() as conn:  # 只读(SQLite WAL 快照不被写者阻塞)
            conn.execute(text("SELECT ..."))
    """

    def __init__(self, url: str, *, echo: bool = False, pool_size: int = 10, max_overflow: int = 20) -> None:
        _require_sqlalchemy()
        self.url = url
        kwargs: dict[str, Any] = {"echo": echo, "connect_args": {}}
        if url.startswith("sqlite"):
            # Gateway 多线程(web/IM worker)共享;sqlite3 默认拒绝跨线程,故关掉该检查 + 给等待窗。
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in url:
                # :memory: 默认每连接一个独立空 db(多线程各看各的)→ 用 StaticPool 单连接共享,
                # 一个逻辑库跨线程/连接持久(否则 ASGI handler 线程看不到建好的表)。
                from sqlalchemy.pool import StaticPool

                kwargs["poolclass"] = StaticPool
        else:
            # PG 连接池调优(研究发现 claw `db.py` 零调优、默认仅 5+10,撑不住 10k-100k):每实例池 +
            # 溢出 + pre_ping 预检活连接(防 PgBouncer/PG 掐死的陈连接复用直接报错)。前面再放 PgBouncer。
            kwargs.update(pool_size=pool_size, max_overflow=max_overflow, pool_pre_ping=True)
        self.engine: Engine = create_engine(url, **kwargs)
        if self.is_sqlite:
            event.listen(self.engine, "connect", _sqlite_on_connect)
            event.listen(self.engine, "begin", _sqlite_emit_begin)

    @classmethod
    def for_path(cls, path: str | Path) -> StorageBackend:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite_url(path))

    @classmethod
    def in_memory(cls) -> StorageBackend:
        return cls("sqlite+pysqlite:///:memory:")

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    @property
    def is_sqlite(self) -> bool:
        return self.engine.dialect.name == "sqlite"

    @property
    def is_postgres(self) -> bool:
        return self.engine.dialect.name == "postgresql"

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        with self.engine.connect() as conn:
            yield conn

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        """写事务。SQLite 上发 BEGIN IMMEDIATE(起步拿写锁,杜绝 WAL 下升级锁失败崩)。"""
        conn = self.engine.connect()
        conn.info["write_txn"] = True
        try:
            with conn.begin():
                yield conn
        finally:
            conn.info.pop("write_txn", None)  # info 随连接进池复用,必清,否则后续读连接被误升级
            conn.close()

    def table_names(self) -> list[str]:
        return sorted(inspect(self.engine).get_table_names())

    def scalar(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params or {}).scalar()

    def dispose(self) -> None:
        self.engine.dispose()


def _sqlite_on_connect(dbapi_connection: Any, _record: Any) -> None:
    dbapi_connection.isolation_level = None  # 关 pysqlite 自发 BEGIN,交 SQLAlchemy 控事务(迁移可事务化 DDL)
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout=30000")  # 必须最先设(见模块 docstring)
        _set_wal_journal(cursor)
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def _try_wal_once(cursor: Any) -> bool:
    """切一次 WAL,成功(库头已是 wal)返回 True。"""
    row = cursor.execute("PRAGMA journal_mode=WAL").fetchone()
    return bool(row and str(row[0]).lower() == "wal")


def _set_wal_journal(cursor: Any) -> None:
    last_exc: Exception | None = None
    for attempt in range(8):
        try:
            ok = _try_wal_once(cursor)
        except Exception as exc:  # SQLITE_BUSY 等瞬时锁竞争 → 退避重试
            last_exc = exc
            ok = False
        if ok:
            return
        time.sleep(0.02 * (attempt + 1))
    if last_exc is not None:
        raise last_exc


def _sqlite_emit_begin(conn: Any) -> None:
    conn.exec_driver_sql("BEGIN IMMEDIATE" if conn.info.get("write_txn") else "BEGIN")
