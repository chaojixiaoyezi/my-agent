from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.local_storage.schema import LocalStoreSchemaMixin


class MinimalSchemaStore(LocalStoreSchemaMixin):
    """Minimal concrete store for schema testing."""

    def __init__(self, tmp_path: Path, enable_fts: bool = True):
        self.root = tmp_path / "store"
        self.root.mkdir()
        self.files_dir = self.root / "files"
        self.files_dir.mkdir()
        self.events_path = self.root / "events.jsonl"
        self.db_path = self.root / "test.db"
        self.enable_fts = enable_fts
        self._fts_available = False
        self._init_schema()


class TestInitSchema:
    def test_creates_tables(self, tmp_path):
        store = MinimalSchemaStore(tmp_path)
        with store._connection() as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {row["name"] for row in tables}
            assert "metadata" in table_names
            assert "records" in table_names
            assert "events" in table_names
            assert "task_registry" in table_names

    def test_creates_records_indexes(self, tmp_path):
        store = MinimalSchemaStore(tmp_path)
        with store._connection() as conn:
            indexes = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            index_names = {row["name"] for row in indexes}
            assert "idx_records_source" in index_names
            assert "idx_records_updated" in index_names

    def test_creates_task_registry_indexes(self, tmp_path):
        store = MinimalSchemaStore(tmp_path)
        with store._connection() as conn:
            indexes = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            index_names = {row["name"] for row in indexes}
            assert "idx_task_registry_session" in index_names
            assert "idx_task_registry_user" in index_names
            assert "idx_task_registry_status" in index_names
            assert "idx_task_registry_updated" in index_names

    def test_creates_events_table(self, tmp_path):
        store = MinimalSchemaStore(tmp_path)
        with store._connection() as conn:
            cursor = conn.execute("PRAGMA table_info(events)")
            columns = {row["name"] for row in cursor.fetchall()}
            assert "seq" in columns
            assert "event_id" in columns
            assert "event_type" in columns
            assert "record_id" in columns
            assert "payload_json" in columns
            assert "created_at" in columns

    def test_creates_records_table_columns(self, tmp_path):
        store = MinimalSchemaStore(tmp_path)
        with store._connection() as conn:
            cursor = conn.execute("PRAGMA table_info(records)")
            columns = {row["name"] for row in cursor.fetchall()}
            assert "id" in columns
            assert "source_type" in columns
            assert "source_id" in columns
            assert "title" in columns
            assert "content_path" in columns
            assert "content_hash" in columns
            assert "content_preview" in columns
            assert "metadata_json" in columns
            assert "visibility" in columns
            assert "created_at" in columns
            assert "updated_at" in columns


class TestFtsSetup:
    def test_fts_enabled_when_supported(self, tmp_path):
        store = MinimalSchemaStore(tmp_path, enable_fts=True)
        assert store._fts_available is True

    def test_fts_disabled_when_not_supported(self, tmp_path):
        store = MinimalSchemaStore(tmp_path, enable_fts=False)
        assert store._fts_available is False

    def test_fts_virtual_table_created(self, tmp_path):
        store = MinimalSchemaStore(tmp_path, enable_fts=True)
        with store._connection() as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {row["name"] for row in tables}
            assert "records_fts" in table_names

    def test_fts_not_created_when_disabled(self, tmp_path):
        store = MinimalSchemaStore(tmp_path, enable_fts=False)
        with store._connection() as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {row["name"] for row in tables}
            assert "records_fts" not in table_names


class TestConnection:
    def test_connection_returns_row_factory(self, tmp_path):
        store = MinimalSchemaStore(tmp_path)
        with store._connection() as conn:
            cursor = conn.execute("SELECT 1 as col")
            row = cursor.fetchone()
            assert row["col"] == 1

    def test_connection_sets_busy_timeout(self, tmp_path):
        store = MinimalSchemaStore(tmp_path)
        with store._connection() as conn:
            cursor = conn.execute("PRAGMA busy_timeout")
            row = cursor.fetchone()
            assert row[0] == 30000

    def test_connection_enables_foreign_keys(self, tmp_path):
        store = MinimalSchemaStore(tmp_path)
        with store._connection() as conn:
            cursor = conn.execute("PRAGMA foreign_keys")
            row = cursor.fetchone()
            assert row[0] == 1


class TestWalMode:
    def test_wal_mode_enabled(self, tmp_path):
        store = MinimalSchemaStore(tmp_path)
        with store._connection() as conn:
            cursor = conn.execute("PRAGMA journal_mode")
            row = cursor.fetchone()
            assert row[0].upper() == "WAL"


class TestSchemaIdempotent:
    def test_init_schema_twice_no_error(self, tmp_path):
        store = MinimalSchemaStore(tmp_path)
        store._init_schema()
        store._init_schema()
        with store._connection() as conn:
            tables = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            ).fetchall()
            assert tables[0][0] >= 4


class TestMetadataTable:
    def test_metadata_task_registry_enabled(self, tmp_path):
        store = MinimalSchemaStore(tmp_path)
        with store._connection() as conn:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key='task_registry_enabled'"
            ).fetchone()
            assert row["value"] == "true"


class TestSchemaMigrations:
    def test_records_table_has_correct_schema(self, tmp_path):
        store = MinimalSchemaStore(tmp_path)
        with store._connection() as conn:
            cursor = conn.execute("PRAGMA table_info(records)")
            columns = {row["name"]: row for row in cursor.fetchall()}
            assert columns["id"]["pk"] == 1  # Primary key
            assert columns["source_type"]["notnull"] == 1
            assert columns["created_at"]["notnull"] == 1

    def test_events_table_auto_increment(self, tmp_path):
        store = MinimalSchemaStore(tmp_path)
        with store._connection() as conn:
            conn.execute(
                "INSERT INTO events(event_id, event_type, record_id, payload_json, created_at) "
                "VALUES('evt1', 'test', '', '{}', 12345)"
            )
            conn.execute(
                "INSERT INTO events(event_id, event_type, record_id, payload_json, created_at) "
                "VALUES('evt2', 'test', '', '{}', 12346)"
            )
            rows = conn.execute("SELECT seq FROM events ORDER BY seq").fetchall()
            assert rows[0][0] < rows[1][0]

    def test_task_registry_insert_and_select(self, tmp_path):
        store = MinimalSchemaStore(tmp_path)
        with store._connection() as conn:
            now = time.time()
            conn.execute(
                "INSERT INTO task_registry(task_id, session_id, user_id, status, goal, created_at, updated_at) "
                "VALUES('task-1', 'sess-1', 'user-1', 'RUNNING', 'test goal', ?, ?)",
                (now, now),
            )
            row = conn.execute("SELECT * FROM task_registry WHERE task_id='task-1'").fetchone()
            assert row["task_id"] == "task-1"
            assert row["status"] == "RUNNING"

    def test_records_content_preview_default(self, tmp_path):
        store = MinimalSchemaStore(tmp_path)
        with store._connection() as conn:
            cursor = conn.execute("PRAGMA table_info(records)")
            columns = {row["name"]: row for row in cursor.fetchall()}
            assert columns["content_preview"]["dflt_value"] == "''"

    def test_records_metadata_default(self, tmp_path):
        store = MinimalSchemaStore(tmp_path)
        with store._connection() as conn:
            cursor = conn.execute("PRAGMA table_info(records)")
            columns = {row["name"]: row for row in cursor.fetchall()}
            assert columns["metadata_json"]["dflt_value"] == "'{}'"
