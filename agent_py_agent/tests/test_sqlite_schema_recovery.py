"""MC4-04：完成指纹不掩盖迁移失败、FTS 待回填或新旧版本交替。"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from agent_py_agent.agent.io.sqlite_schema import schema_fingerprint
from agent_py_agent.agent.local_storage import schema as local_schema
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.runtime_db import schema as runtime_schema
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


def _metadata(path, key):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    finally:
        conn.close()


def _fts_titles(store):
    with store._connection() as conn:
        return [row[0] for row in conn.execute("SELECT title FROM records_fts ORDER BY title")]


def test_fts_enable_backfills_records_created_while_disabled(tmp_path):
    path = tmp_path / "local.db"
    store = LocalStore(path, enable_fts=False)
    store.upsert_record(source_type="memory", source_id="m", title="原文", content="启用之前的中文正文")
    enabled = LocalStore(path)
    assert enabled.fts_available
    assert enabled._fts_needs_rebuild is False
    assert _fts_titles(enabled) == ["原文"]
    disabled = LocalStore(path, enable_fts=False)
    disabled.upsert_record(source_type="memory", source_id="n", title="新增", content="关闭之后的中文正文")
    assert _fts_titles(LocalStore(path)) == ["原文", "新增"]


def test_fts_backfill_failure_remains_pending_and_recovers_on_reopen(tmp_path, monkeypatch):
    path = tmp_path / "local.db"
    initial = LocalStore(path)
    initial.upsert_record(source_type="memory", source_id="m", title="保留", content="正文事实仍在")
    monkeypatch.setattr(local_schema, "_FTS_SCHEMA_VERSION", "3")
    original = LocalStore.rebuild_fts

    def fail_backfill(self):
        raise sqlite3.OperationalError("injected backfill failure")

    monkeypatch.setattr(LocalStore, "rebuild_fts", fail_backfill)
    failed = LocalStore(path)
    assert failed._fts_needs_rebuild is True
    assert _metadata(path, "records_fts_rebuild_pending")[0] == "true"
    assert failed.count_records() == 1
    monkeypatch.setattr(LocalStore, "rebuild_fts", original)
    recovered = LocalStore(path)
    assert recovered._fts_needs_rebuild is False
    assert _fts_titles(recovered) == ["保留"]
    assert _metadata(path, "records_fts_rebuild_pending")[0] == "false"


def test_fts_unavailable_does_not_stamp_completed_schema(tmp_path, monkeypatch):
    path = tmp_path / "local.db"
    original = local_schema._FTS_TABLE_SQL
    monkeypatch.setattr(local_schema, "_FTS_TABLE_SQL", "CREATE VIRTUAL TABLE records_fts USING no_such_fts(id)")
    failed = LocalStore(path)
    assert failed.fts_available is False
    assert _metadata(path, "sqlite_schema_fingerprint") is None
    monkeypatch.setattr(local_schema, "_FTS_TABLE_SQL", original)
    assert LocalStore(path).fts_available is True


def test_failed_fts_statement_with_existing_projection_does_not_stamp(tmp_path, monkeypatch):
    path = tmp_path / "local.db"
    LocalStore(path)
    before = _metadata(path, "sqlite_schema_fingerprint")
    monkeypatch.setattr(local_schema, "_FTS_TABLE_SQL", "INSERT INTO missing_fts_table VALUES(1)")
    store = LocalStore(path)
    assert store.fts_available  # 物理投影可用，不代表本次初始化语句成功
    assert _metadata(path, "sqlite_schema_fingerprint") == before


def test_mid_backfill_failure_rolls_back_and_pending_survives(tmp_path, monkeypatch):
    path = tmp_path / "local.db"
    store = LocalStore(path, enable_fts=False)
    store.upsert_record(source_type="memory", source_id="m", title="保留", content="仍在正文文件")
    original = LocalStore._replace_fts_row

    def write_then_fail(self, *args):
        original(self, *args)
        raise sqlite3.OperationalError("injected after FTS row write")

    monkeypatch.setattr(LocalStore, "_replace_fts_row", write_then_fail)
    failed = LocalStore(path)
    assert failed._fts_needs_rebuild
    assert _fts_titles(failed) == []
    assert failed.count_records() == 1
    assert _metadata(path, "records_fts_rebuild_pending")[0] == "true"
    monkeypatch.setattr(LocalStore, "_replace_fts_row", original)
    assert _fts_titles(LocalStore(path)) == ["保留"]


@pytest.mark.parametrize("store_type,sql_name", [(RuntimeRepository, "_BASE_RUNTIME_SQL"), (LocalStore, "_BASE_SCHEMA_SQL")])
def test_failed_migration_rolls_back_and_reopen_recovers(tmp_path, monkeypatch, store_type, sql_name):
    module = runtime_schema if store_type is RuntimeRepository else local_schema
    path = tmp_path / "store.db"
    store_type(path)
    fingerprint = _metadata(path, "sqlite_schema_fingerprint")
    original = getattr(module, sql_name)
    broken = original + ("CREATE TABLE IF NOT EXISTS future_fact(id TEXT)", "INSERT INTO absent_table VALUES(1)")
    monkeypatch.setattr(module, sql_name, broken)
    with pytest.raises(sqlite3.OperationalError):
        store_type(path)
    assert _metadata(path, "sqlite_schema_fingerprint") == fingerprint
    monkeypatch.setattr(module, sql_name, original + (broken[-2],))
    store_type(path)
    assert _metadata(path, "sqlite_schema_fingerprint") != fingerprint


def test_runtime_post_migration_index_is_part_of_fingerprint(tmp_path, monkeypatch):
    path = tmp_path / "runtime.db"
    RuntimeRepository(path)
    monkeypatch.setattr(runtime_schema, "_RUNTIME_MIGRATION_SQL", runtime_schema._RUNTIME_MIGRATION_SQL + (
        "CREATE INDEX IF NOT EXISTS future_task_title ON tasks(title)",
    ))
    RuntimeRepository(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='future_task_title'").fetchone()
    finally:
        conn.close()


def test_local_fingerprint_includes_legacy_rename_and_fts_contract(monkeypatch):
    initial = local_schema._local_schema_fingerprint(True)
    monkeypatch.setattr(local_schema, "_LOCAL_MIGRATION_SQL", ("ALTER TABLE old_projection RENAME TO legacy_projection",))
    assert local_schema._local_schema_fingerprint(True) != initial
    renamed = local_schema._local_schema_fingerprint(True)
    monkeypatch.setattr(local_schema, "_FTS_TOKENIZE", "unicode61")
    assert local_schema._local_schema_fingerprint(True) != renamed


def test_new_runtime_then_old_then_new_preserves_future_facts(tmp_path, monkeypatch):
    path = tmp_path / "runtime.db"
    original = runtime_schema._BASE_RUNTIME_SQL
    newer = original + ("CREATE TABLE IF NOT EXISTS future_fact(value TEXT)",)
    monkeypatch.setattr(runtime_schema, "_BASE_RUNTIME_SQL", newer)
    RuntimeRepository(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO future_fact VALUES('preserved')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(runtime_schema, "_BASE_RUNTIME_SQL", original)
    RuntimeRepository(path)
    monkeypatch.setattr(runtime_schema, "_BASE_RUNTIME_SQL", newer)
    RuntimeRepository(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT value FROM future_fact").fetchone()[0] == "preserved"
    finally:
        conn.close()


def test_older_local_does_not_downgrade_newer_fts_projection(tmp_path, monkeypatch):
    path = tmp_path / "local.db"
    monkeypatch.setattr(local_schema, "_FTS_SCHEMA_VERSION", "3")
    newer = LocalStore(path)
    newer.upsert_record(source_type="memory", source_id="m", title="事实", content="新版本保留正文")
    monkeypatch.setattr(local_schema, "_FTS_SCHEMA_VERSION", "2")
    older = LocalStore(path)
    assert older.fts_available
    assert _metadata(path, "records_fts_schema_version")[0] == "3"
    assert _fts_titles(older) == ["事实"]
    monkeypatch.setattr(local_schema, "_FTS_SCHEMA_VERSION", "3")
    assert _fts_titles(LocalStore(path)) == ["事实"]


@pytest.mark.parametrize("store_type", [RuntimeRepository, LocalStore])
def test_simultaneous_first_init_rechecks_completion_under_write_lock(tmp_path, store_type):
    path = tmp_path / "store.db"
    barrier = Barrier(2)

    def init():
        barrier.wait(timeout=10)
        return store_type(path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(init) for _ in range(2)]
        stores = [future.result() for future in futures]
    assert len(stores) == 2
    assert _metadata(path, "sqlite_schema_fingerprint") is not None
    if store_type is LocalStore:
        assert all(store.fts_available for store in stores)
    else:
        assert _metadata(path, "sqlite_schema_fingerprint")[0] == schema_fingerprint((
            runtime_schema._BASE_RUNTIME_SQL, runtime_schema._RUNTIME_MIGRATIONS, runtime_schema._RUNTIME_MIGRATION_SQL,
        ))
