from __future__ import annotations

"""LLM: owns SQLite connection setup, schema creation, and transactional context helpers.

给人看的解释：
这个文件只负责 LocalStore 的数据库地基。
建表、打开连接、启用 WAL/外键、处理 FTS5 是否可用，都在这里集中处理。
"""

import sqlite3
from contextlib import contextmanager
from typing import Iterator


class LocalStoreSchemaMixin:
    """LLM: mixin providing schema initialization and safe SQLite connection helpers.

    给人看的解释：
    LocalStore 启动时会先靠这个 mixin 把数据库准备好。
    其他模块只需要用 `_connection()`，不用关心连接怎么创建和关闭。
    """

    def _init_schema(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
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
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_records_source
                ON records(source_type, source_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_records_updated
                ON records(updated_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    record_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
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
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_registry_session ON task_registry(session_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_registry_user ON task_registry(user_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_registry_status ON task_registry(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_registry_updated ON task_registry(updated_at)"
            )
            conn.execute(
                """
                INSERT INTO metadata(key, value)
                VALUES('task_registry_enabled', 'true')
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
            )
            if self.enable_fts:
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
            conn.commit()

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

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()
