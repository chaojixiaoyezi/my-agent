# LLM: schema 与迁移兼容性由这里兜底，改表结构前先设计旧库路径。
# 模块用途: LocalStore SQLite 连接、schema 初始化和事务上下文。

from __future__ import annotations

"""owns SQLite connection setup, schema creation, and transactional context helpers.

给人看的解释：
这个文件只负责 LocalStore 的数据库地基。
建表、打开连接、启用 WAL/外键、处理 FTS5 是否可用，都在这里集中处理。
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

_BASE_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS records (
        id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL,
        source_id TEXT NOT NULL,
        title TEXT NOT NULL,
        content_path TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        content_preview TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        visibility TEXT NOT NULL DEFAULT 'private',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_records_source
    ON records(source_type, source_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_records_updated
    ON records(updated_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL,
        record_id TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL
    )
    """,
)

_TASK_REGISTRY_SQL = (
    """
    CREATE TABLE IF NOT EXISTS task_registry (
        task_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        status TEXT NOT NULL,
        goal TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_task_registry_session ON task_registry(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_registry_user ON task_registry(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_registry_status ON task_registry(status)",
    "CREATE INDEX IF NOT EXISTS idx_task_registry_updated ON task_registry(updated_at)",
    """
    INSERT INTO metadata(key, value)
    VALUES('task_registry_enabled', 'true')
    ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """,
)


# LLM: LocalStoreSchemaMixin 属于 LocalStore 本地事实索引 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: LocalStoreSchemaMixin 封装 LocalStore 本地事实索引 的一组相关操作，供上层组合调用。
class LocalStoreSchemaMixin:

    # LLM: LocalStoreSchemaMixin._init_schema 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 init_schema 步骤，并保持调用方依赖的数据形状。
    def _init_schema(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            self._execute_schema(conn, _BASE_SCHEMA_SQL)
            self._execute_schema(conn, _TASK_REGISTRY_SQL)
            if self.enable_fts:
                self._init_fts_schema(conn)
            conn.commit()

    # LLM: LocalStoreSchemaMixin._execute_schema 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 execute_schema 步骤，并保持调用方依赖的数据形状。
    def _execute_schema(self, conn: sqlite3.Connection, statements: tuple[str, ...]) -> None:
        for statement in statements:
            conn.execute(statement)

    # LLM: LocalStoreSchemaMixin._init_fts_schema 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 init_fts_schema 步骤，并保持调用方依赖的数据形状。
    def _init_fts_schema(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS records_fts
                USING fts5(id UNINDEXED, title, content)
                """
            )
            self._fts_available = True
        except sqlite3.OperationalError:
            self._fts_available = False

    # LLM: LocalStoreSchemaMixin._connect 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 connect 步骤，并保持调用方依赖的数据形状。
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # 另一个 gateway / CLI 进程短暂持锁时，状态查询不能因为切 WAL 失败而崩。
            pass
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # LLM: LocalStoreSchemaMixin._connection 属于 LocalStore 本地事实索引 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 LocalStore 本地事实索引 中的 connection 步骤，并保持调用方依赖的数据形状。
    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()
