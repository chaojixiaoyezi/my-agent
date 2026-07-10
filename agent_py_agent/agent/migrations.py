"""Schema 版本化迁移(审计 #10):有序、幂等、单事务,已上线客户库可安全加列/改约束。

原问题:所有建表是 ``CREATE TABLE IF NOT EXISTS``,对已存在表是 no-op → 老库要加列时静默缺列或运行期炸,
几十国客户在跑、不停机滚动升级时无安全迁移路径。迁移执行器须处理：

- ``schema_migrations`` 版本表记录已应用版本;``apply_pending`` 在**单事务**里按序应用未应用的迁移,任一步
  失败整批回滚、不记版本(原子)。
- 并发串行:SQLite 走 ``begin()`` 的 BEGIN IMMEDIATE;PostgreSQL 多实例取事务级 ``pg_advisory_xact_lock``。
- 拿锁后**重读**已应用版本:并发首启时等到锁的输家看到赢家已写入 → 跳过、干净 no-op(不拿陈旧 pending 撞 DDL)。
- 约定:新列须 nullable + default(扩张-收缩式发布),老行读得到默认值、不丢数据。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from agent_py_agent.agent.storage_backend import StorageBackend

try:
    from sqlalchemy import (
        Column,
        Float,
        Integer,
        MetaData,
        String,
        Table,
        insert,
        inspect,
        select,
        text,
    )
    from sqlalchemy.engine import Connection

    _HAS_SQLALCHEMY = True
except ImportError:
    _HAS_SQLALCHEMY = False

_ADVISORY_LOCK_KEY = 7242319002  # 迁移串行用的 PG 事务级 advisory lock key(固定常量)


class MigrationError(Exception):
    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


@dataclass(frozen=True)
class Migration:
    """一个迁移:版本号 + 名称 + apply(conn) 执行 DDL(加列/改约束/建新表)。"""

    version: int
    name: str
    apply: Callable[[Connection], None]
    transactional: bool = True


def _safe_table_name(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise MigrationError(f"迁移版本表名非法: {name!r}")
    return name


def _version_table(name: str) -> Table:
    meta = MetaData()
    return Table(
        _safe_table_name(name),
        meta,
        Column("version", Integer, primary_key=True),
        Column("name", String(200), nullable=False),
        Column("applied_at", Float, nullable=False),
    )


class MigrationRunner:
    """对 StorageBackend 应用有序迁移并记录版本。SQLite/PostgreSQL 同代码。"""

    def __init__(
        self,
        backend: StorageBackend,
        migrations: Sequence[Migration],
        *,
        table_name: str = "schema_migrations",
    ) -> None:
        self._backend = backend
        self._migrations = sorted(migrations, key=lambda m: m.version)
        versions = [m.version for m in self._migrations]
        if len(set(versions)) != len(versions):
            raise MigrationError("配置了重复的迁移版本号")
        self._table = _version_table(table_name)

    def _ensure_version_table(self, conn: Connection) -> None:
        self._table.create(conn, checkfirst=True)

    def applied_versions(self) -> list[int]:
        with self._backend.begin() as conn:
            self._ensure_version_table(conn)
            rows = conn.execute(select(self._table.c.version).order_by(self._table.c.version)).all()
        return [int(r[0]) for r in rows]

    def pending(self) -> list[Migration]:
        applied = set(self.applied_versions())
        return [m for m in self._migrations if m.version not in applied]

    def pending_read_only(self) -> list[Migration]:
        """只读检查待迁移项；应用 Pod readiness 用它，绝不在启动时偷偷建版本表。"""
        if self._table.name not in inspect(self._backend.engine).get_table_names():
            return list(self._migrations)
        with self._backend.connect() as conn:
            applied = {int(r[0]) for r in conn.execute(select(self._table.c.version)).all()}
        return [m for m in self._migrations if m.version not in applied]

    def require_current(self) -> None:
        pending = self.pending_read_only()
        if pending:
            versions = ",".join(str(item.version) for item in pending)
            raise MigrationError(
                f"数据库 schema 未升级到当前版本；待应用迁移: {versions}",
                detail={"pending_versions": [item.version for item in pending]},
            )

    def apply_pending(self) -> list[Migration]:
        """单事务应用所有未应用迁移;失败整批回滚。返回本次实际应用的迁移。"""
        if not self.pending():
            return []  # 无锁快速路径:已最新(常态),不进写锁
        if self._backend.is_postgres and any(not item.transactional for item in self._migrations):
            return self._apply_postgres_online()
        with self._backend.begin() as conn:  # SQLite=BEGIN IMMEDIATE 串行
            if self._backend.is_postgres:
                conn.execute(text(f"SELECT pg_advisory_xact_lock({_ADVISORY_LOCK_KEY})"))  # 多实例串行
            self._ensure_version_table(conn)
            return self._apply_unapplied(conn)

    def _apply_postgres_online(self) -> list[Migration]:
        """PG 在线迁移：会话 advisory lock 串行，expand DDL 逐项提交，CONCURRENTLY 可走 autocommit。

        非事务迁移必须自身幂等；若进程在 DDL 成功、版本落账前退出，重跑仍安全。
        """
        done: list[Migration] = []
        lock_conn = self._backend.engine.connect()
        try:
            lock_conn.execute(text(f"SELECT pg_advisory_lock({_ADVISORY_LOCK_KEY})"))
            lock_conn.commit()
            with self._backend.begin() as conn:
                self._ensure_version_table(conn)
            applied = set(self.applied_versions())
            self._apply_online_migrations(applied, done)
        finally:
            self._release_online_lock(lock_conn)
        return done

    def _apply_online_migrations(self, applied: set[int], done: list[Migration]) -> None:
        for migration in self._migrations:
            if migration.version in applied:
                continue
            self._apply_online_one(migration)
            done.append(migration)
            applied.add(migration.version)

    @staticmethod
    def _release_online_lock(lock_conn: Connection) -> None:
        try:
            lock_conn.execute(text(f"SELECT pg_advisory_unlock({_ADVISORY_LOCK_KEY})"))
            lock_conn.commit()
        finally:
            lock_conn.close()

    def _apply_online_one(self, migration: Migration) -> None:
        if migration.transactional:
            with self._backend.begin() as conn:
                self._apply_one(conn, migration)
            return
        try:
            with self._backend.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                migration.apply(conn)
        except Exception as exc:
            raise MigrationError(
                f"迁移 {migration.version} ({migration.name}) 失败",
                detail={"version": migration.version, "reason": str(exc)[:200]},
            ) from exc
        with self._backend.begin() as conn:
            conn.execute(
                insert(self._table).values(
                    version=migration.version,
                    name=migration.name,
                    applied_at=time.time(),
                )
            )

    def _apply_unapplied(self, conn: Connection) -> list[Migration]:
        applied = {int(r[0]) for r in conn.execute(select(self._table.c.version)).all()}  # 拿锁后重读
        done: list[Migration] = []
        for migration in self._migrations:
            if migration.version not in applied:
                self._apply_one(conn, migration)
                done.append(migration)
        return done

    def _apply_one(self, conn: Connection, migration: Migration) -> None:
        try:
            migration.apply(conn)
        except Exception as exc:
            raise MigrationError(
                f"迁移 {migration.version} ({migration.name}) 失败",
                detail={"version": migration.version, "reason": str(exc)[:200]},
            ) from exc
        conn.execute(
            insert(self._table).values(version=migration.version, name=migration.name, applied_at=time.time())
        )
