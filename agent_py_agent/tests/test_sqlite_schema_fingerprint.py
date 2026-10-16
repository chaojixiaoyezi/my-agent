"""E2：用真实 SQL trace 验证持久结构指纹，不以耗时判定缓存命中。"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.io.sqlite_scope import sqlite_connection_scope
from agent_py_agent.agent.local_storage import schema as local_schema
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.runtime_db import schema as runtime_schema
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


@pytest.fixture
def sql_trace(monkeypatch):
    statements = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced)
    return statements


def _ddl(statements):
    return [sql for sql in statements if sql.lstrip().upper().startswith(("CREATE", "ALTER", "DROP"))]


@pytest.mark.parametrize("store_type", [RuntimeRepository, LocalStore])
def test_unchanged_schema_skips_all_ddl_and_migration_checks(tmp_path, sql_trace, store_type):
    path = tmp_path / "store.db"
    store_type(path)
    assert _ddl(sql_trace)
    sql_trace.clear()
    store_type(path)
    assert _ddl(sql_trace) == []
    assert not any("TABLE_INFO" in sql.upper() for sql in sql_trace)
    assert not any(sql.lstrip().upper().startswith("INSERT INTO METADATA") for sql in sql_trace)


def _old_runtime(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE task_runs(task_run_id TEXT PRIMARY KEY, task_id TEXT, "
                     "status TEXT, created_at REAL, updated_at REAL, metadata_json TEXT)")
        conn.execute("INSERT INTO task_runs VALUES('old','t','running',1,1,'{}')")


def test_old_runtime_migrates_and_preserves_rows(tmp_path):
    path = tmp_path / "runtime.db"
    _old_runtime(path)
    repo = RuntimeRepository(path)
    assert repo.get_task_run("old")["closed_at"] == 0


@pytest.mark.parametrize('reuse_enabled', [False, True])
def test_replacing_runtime_file_with_old_copy_migrates_again(tmp_path, reuse_enabled):
    path = tmp_path / "runtime.db"
    with sqlite_connection_scope(reuse_enabled):
        RuntimeRepository(path)
        replacement = tmp_path / "replacement.db"
        _old_runtime(replacement)
        replacement.replace(path)
        assert RuntimeRepository(path).get_task_run("old")["closed_at"] == 0


def test_runtime_fingerprint_includes_migration_list(tmp_path, monkeypatch, sql_trace):
    path = tmp_path / "runtime.db"
    RuntimeRepository(path)
    migration = ("future_column", "tasks", "ALTER TABLE tasks ADD COLUMN future_column TEXT DEFAULT ''")
    monkeypatch.setattr(runtime_schema, "_RUNTIME_MIGRATIONS", runtime_schema._RUNTIME_MIGRATIONS + (migration,))
    sql_trace.clear()
    RuntimeRepository(path)
    assert any("ADD COLUMN future_column" in sql for sql in sql_trace)
    with sqlite3.connect(path) as conn:
        assert "future_column" in {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}


def test_runtime_fingerprint_includes_ddl_text(tmp_path, monkeypatch):
    path = tmp_path / "runtime.db"
    RuntimeRepository(path)
    monkeypatch.setattr(runtime_schema, "_BASE_RUNTIME_SQL", runtime_schema._BASE_RUNTIME_SQL + (
        "CREATE TABLE IF NOT EXISTS future_table(id TEXT)",
    ))
    RuntimeRepository(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='future_table'").fetchone()


def test_read_only_does_not_initialize_or_migrate(tmp_path, sql_trace):
    path = tmp_path / "runtime.db"
    _old_runtime(path)
    before = path.read_bytes(), path.stat().st_mtime_ns
    sql_trace.clear()
    repo = RuntimeRepository(path, read_only=True)
    assert repo.get_task_run("old")["status"] == "running"
    assert _ddl(sql_trace) == []
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_local_reopen_restores_fts_state_and_search(tmp_path, sql_trace):
    path = tmp_path / "local.db"
    first = LocalStore(path)
    first.upsert_record(source_type="memory", source_id="m", title="中文", content="恢复记忆连贯样本")
    sql_trace.clear()
    reopened = LocalStore(path)
    assert reopened.fts_available
    assert reopened._fts_needs_rebuild is False
    assert _ddl(sql_trace) == []
    assert any(hit.title == "中文" for hit in reopened._search_fts("记忆连贯", limit=5, source_type=None, visibility=None))


def test_local_fts_disabled_then_enabled_is_not_hidden_by_fingerprint(tmp_path):
    path = tmp_path / "local.db"
    disabled = LocalStore(path, enable_fts=False)
    assert disabled.fts_available is False
    assert LocalStore(path).fts_available is True
    assert LocalStore(path, enable_fts=False).fts_available is False


def test_local_fts_migration_invalidates_fingerprint_and_backfills(tmp_path, monkeypatch):
    path = tmp_path / "local.db"
    first = LocalStore(path)
    first.upsert_record(source_type="memory", source_id="m", title="保留", content="正文事实仍在")
    monkeypatch.setattr(local_schema, "_FTS_SCHEMA_VERSION", "next")
    monkeypatch.setattr(local_schema, "_FTS_TOKENIZE", "unicode61")
    reopened = LocalStore(path)
    with reopened._connection() as conn:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='records_fts'").fetchone()[0]
        assert "unicode61" in sql
        assert conn.execute("SELECT title FROM records_fts").fetchone()[0] == "保留"


@pytest.mark.parametrize('reuse_enabled', [False, True])
def test_local_file_replaced_by_legacy_projection_keeps_data(tmp_path, reuse_enabled):
    path = tmp_path / "local.db"
    with sqlite_connection_scope(reuse_enabled):
        LocalStore(path, enable_fts=False)
        replacement = tmp_path / "old.db"
        with sqlite3.connect(replacement) as conn:
            legacy_sql = local_schema._CONTROL_PLANE_SQL[0].replace("legacy_agent_runs", "agent_runs")
            conn.execute(legacy_sql)
            conn.execute("INSERT INTO agent_runs(run_id,root_task_id,created_at,updated_at) VALUES('r','t',1,1)")
        replacement.replace(path)
        reopened = LocalStore(path, enable_fts=False)
        with reopened._connection() as conn:
            assert conn.execute("SELECT run_id FROM legacy_agent_runs").fetchone()[0] == "r"


@pytest.mark.parametrize("store_type", [RuntimeRepository, LocalStore])
def test_concurrent_initialization_produces_complete_schema(tmp_path, store_type):
    path = tmp_path / "store.db"
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores = list(pool.map(lambda _: store_type(path), range(8)))
    assert len(stores) == 8
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
