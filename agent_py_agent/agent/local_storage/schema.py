
from __future__ import annotations

"""owns SQLite connection setup, schema creation, and transactional context helpers.

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

_CONTROL_PLANE_SQL = (
    """
    CREATE TABLE IF NOT EXISTS agent_runs (
        run_id TEXT PRIMARY KEY,
        root_task_id TEXT NOT NULL,
        parent_run_id TEXT NOT NULL DEFAULT '',
        depth INTEGER NOT NULL DEFAULT 0,
        role TEXT NOT NULL DEFAULT '',
        agent_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        progress REAL NOT NULL DEFAULT 0,
        current_step TEXT NOT NULL DEFAULT '',
        latest_summary TEXT NOT NULL DEFAULT '',
        workspace_path TEXT NOT NULL DEFAULT '',
        checkpoint_ref TEXT NOT NULL DEFAULT '',
        latest_compact_ref TEXT NOT NULL DEFAULT '',
        compact_count INTEGER NOT NULL DEFAULT 0,
        heartbeat_at REAL NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        reserved_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_root ON agent_runs(root_task_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_parent ON agent_runs(parent_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_updated ON agent_runs(updated_at)",
    """
    CREATE TABLE IF NOT EXISTS agent_events (
        event_id TEXT PRIMARY KEY,
        root_task_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        parent_run_id TEXT NOT NULL DEFAULT '',
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        reserved_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_events_root ON agent_events(root_task_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events(run_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_events_type ON agent_events(event_type)",
    """
    CREATE TABLE IF NOT EXISTS task_rollups (
        task_id TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT '',
        progress REAL NOT NULL DEFAULT 0,
        running_agents INTEGER NOT NULL DEFAULT 0,
        blocked_agents INTEGER NOT NULL DEFAULT 0,
        completed_agents INTEGER NOT NULL DEFAULT 0,
        failed_agents INTEGER NOT NULL DEFAULT 0,
        latest_summary TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        reserved_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_task_rollups_updated ON task_rollups(updated_at)",
)

_RUNTIME_GATE_LEDGER_SQL = (
    """
    CREATE TABLE IF NOT EXISTS runtime_gate_ledger (
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL DEFAULT '',
        operation_id TEXT NOT NULL,
        tool TEXT NOT NULL DEFAULT '',
        parameters_json TEXT NOT NULL DEFAULT '{}',
        runtime_gate_json TEXT NOT NULL DEFAULT '{}',
        idempotency_key TEXT NOT NULL DEFAULT '',
        args_hash TEXT NOT NULL DEFAULT '',
        approval_id TEXT NOT NULL DEFAULT '',
        result_ref TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY(run_id, operation_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runtime_gate_ledger_run ON runtime_gate_ledger(run_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_gate_ledger_task ON runtime_gate_ledger(task_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_runtime_gate_ledger_idem ON runtime_gate_ledger(run_id, idempotency_key)",
)


class LocalStoreSchemaMixin:

    def _init_schema(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            self._execute_schema(conn, _BASE_SCHEMA_SQL)
            self._execute_schema(conn, _TASK_REGISTRY_SQL)
            self._execute_schema(conn, _CONTROL_PLANE_SQL)
            self._execute_schema(conn, _RUNTIME_GATE_LEDGER_SQL)
            if self.enable_fts:
                self._init_fts_schema(conn)
            conn.commit()

    def _execute_schema(self, conn: sqlite3.Connection, statements: tuple[str, ...]) -> None:
        for statement in statements:
            conn.execute(statement)

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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
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
