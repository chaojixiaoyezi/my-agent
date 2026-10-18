"""C3-L：真实临时SQLite、原scope、逐writer撤权与FTS精确CAS；不接模型或服务。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextvars import copy_context

import pytest

from agent_py_agent.agent.common import global_write_commit as commit_module
from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.common.global_write_commit import (
    GlobalWriteCommitUnavailable,
    global_write_commit_scope,
)
from agent_py_agent.agent.gateway_parts.multiprocess_local_control import (
    LocalControlCoordinatorUnavailable,
)
from agent_py_agent.agent.io.sqlite_scope import sqlite_connection_scope
from agent_py_agent.agent.local_storage import schema as schema_module
from agent_py_agent.agent.local_storage.maintenance import _typed_rebuild_rows_equal
from agent_py_agent.agent.local_storage.store import LocalStore


# LLM: 仅测试替代外层同步末门，真实global scope/SQLite/文件算法不替换；记录门内耗时，不声称HTTP来源已接通。
# 类用途: 注入同对象来源拒绝并测真实本地提交门持时。
class Door:
    def __init__(self):
        self.calls = 0
        self.inside = False
        self.reject_at = None
        self.error = LocalControlCoordinatorUnavailable("injected_original_source_revoked")
        self.durations = []

    def __call__(self, operation):
        self.calls += 1
        if self.calls == self.reject_at:
            raise self.error
        assert not self.inside
        self.inside = True
        started = time.perf_counter()
        try:
            return operation()
        finally:
            self.durations.append(time.perf_counter() - started)
            self.inside = False


# LLM: 只建真实Store；原门运输当次Door，scope退出不向Store留下callback。
# 函数用途: 为每个测试构造同一个C资源并返回观测门。
def make_store(tmp_path, **kwargs):
    door = Door()
    with global_write_commit_scope(door):
        store = LocalStore(tmp_path / "index.db", coordinator_io=True, **kwargs)
    return store, door


# LLM: 诊断只读实际SQLite结果，不用Store缺源fallback或修账；原连接及时关闭。
# 函数用途: 在撤权后核对真实partial和原恢复标记。
def read_sql(store, sql, values=()):
    conn = sqlite3.connect(store.db_path)
    try:
        return conn.execute(sql, values).fetchall()
    finally:
        conn.close()


# LLM: 操作只调用获批真实API，不mock业务；门scope归还后用于逐writer重新失效。
# 函数用途: 执行一个原业务变化，便于参数化每个真实提交边界。
def run_operation(store, name):
    if name == "upsert":
        return store.upsert_record(source_type="memory", source_id="new", title="新增", content="新的长期记忆正文")
    if name == "delete":
        return store.delete_record(store.make_record_id("memory", "old"))
    if name == "orphan":
        return store.delete_record("orphan")
    return store.rebuild_fts()


def test_coordinator_missing_scope_has_no_directories(tmp_path):
    with pytest.raises(GlobalWriteCommitUnavailable):
        LocalStore(tmp_path / "absent" / "index.db", coordinator_io=True)
    assert not (tmp_path / "absent").exists()


def test_default_owner_store_does_not_enter_global_guard(tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError("ordinary owner must not enter global guard")
    monkeypatch.setattr(commit_module, "require_global_writer", forbidden)
    store = LocalStore(tmp_path / "index.db")
    record = store.upsert_record(source_type="memory", source_id="x", title="原默认", content="保留默认行为")
    assert store.rebuild_fts() == 1
    assert store.delete_record(record.id)


def test_same_store_uses_new_borrow_scope_without_saved_source(tmp_path):
    store, first = make_store(tmp_path)
    second = Door()
    with global_write_commit_scope(second):
        record = run_operation(store, "upsert")
    assert second.calls > 0
    assert all(not callable(value) for value in store.__dict__.values())
    assert not any(value is first or value is second for value in store.__dict__.values())
    with pytest.raises(GlobalWriteCommitUnavailable):
        store.delete_record(record.id)


def test_c_scope_expiry_and_copied_context_do_not_reopen_db(tmp_path):
    store, door = make_store(tmp_path)
    with global_write_commit_scope(door):
        copied = copy_context()
    before = door.calls
    with pytest.raises(GlobalWriteCommitUnavailable):
        copied.run(store.count_records)
    assert door.calls == before


@pytest.mark.parametrize("name", ["upsert", "delete", "orphan", "rebuild"])
def test_each_writer_source_failure_preserves_same_object_and_stops(tmp_path, name):
    def prepared(directory):
        store, door = make_store(directory)
        with global_write_commit_scope(door):
            store.upsert_record(source_type="memory", source_id="old", title="旧记录", content="原始正文")
        if name == "orphan":
            (store.files_dir / "orphan.txt").write_text("待擦除正文", encoding="utf-8")
        return store, door
    original, observation = prepared(tmp_path / "observe")
    start = observation.calls
    with global_write_commit_scope(observation):
        run_operation(original, name)
    total = observation.calls - start
    assert total >= 2
    for offset in range(1, total + 1):
        store, door = prepared(tmp_path / str(offset))
        door.reject_at = door.calls + offset
        with global_write_commit_scope(door), pytest.raises(LocalControlCoordinatorUnavailable) as caught:
            run_operation(store, name)
        assert caught.value is door.error
        assert door.calls == door.reject_at


@pytest.mark.parametrize("position", [1, 2])
def test_constructor_sources_each_fixed_birth(tmp_path, position):
    door = Door()
    door.reject_at = position
    with global_write_commit_scope(door), pytest.raises(LocalControlCoordinatorUnavailable) as caught:
        LocalStore(tmp_path / "index.db", coordinator_io=True)
    assert caught.value is door.error
    assert door.calls == position


def test_c_never_borrows_scope_or_batch_connections_and_uses_timeout_zero(tmp_path, monkeypatch):
    connects = []
    actual = sqlite3.connect
    def connect(*args, **kwargs):
        conn = actual(*args, **kwargs)
        connects.append((kwargs.get("timeout"), conn))
        return conn
    monkeypatch.setattr(schema_module.sqlite3, "connect", connect)
    store, door = make_store(tmp_path)
    with global_write_commit_scope(door), sqlite_connection_scope(True), store.connection_batch():
        with store._connection() as first:
            assert first.execute("PRAGMA busy_timeout").fetchone()[0] == 0
        with store._connection() as second:
            assert second is not first
    assert not hasattr(store._connection_local, "connection")
    assert all(timeout == 0.0 for timeout, _ in connects)
    for _, conn in connects:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_c_begin_busy_returns_without_sleep_and_keeps_body_partial(tmp_path):
    store, door = make_store(tmp_path)
    blocker = sqlite3.connect(store.db_path)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        started = time.perf_counter()
        with global_write_commit_scope(door), pytest.raises(sqlite3.OperationalError):
            run_operation(store, "upsert")
        assert time.perf_counter() - started < 1.0
    finally:
        blocker.rollback()
        blocker.close()
    record_id = store.make_record_id("memory", "new")
    assert store._content_file(record_id).read_text() == "新的长期记忆正文"
    assert read_sql(store, "SELECT id FROM records") == []


def test_sql_and_body_writers_are_inside_but_file_lock_waits_outside(tmp_path, monkeypatch):
    door = Door()
    actual_connect = sqlite3.connect
    statements = []
    violations = []
    def connect(*args, **kwargs):
        assert door.inside
        conn = actual_connect(*args, **kwargs)
        def trace(statement):
            statements.append(statement)
            if statement.split()[0].upper() in {"INSERT", "DELETE", "UPDATE", "CREATE", "DROP", "ALTER", "BEGIN", "COMMIT"}:
                if not door.inside:
                    violations.append(statement)
        conn.set_trace_callback(trace)
        return conn
    actual_flock = json_io._flock_descriptor
    def flock(*args, **kwargs):
        assert not door.inside
        return actual_flock(*args, **kwargs)
    monkeypatch.setattr(schema_module.sqlite3, "connect", connect)
    monkeypatch.setattr(json_io, "_flock_descriptor", flock)
    with global_write_commit_scope(door):
        store = LocalStore(tmp_path / "index.db", coordinator_io=True)
        record = run_operation(store, "upsert")
        store.rebuild_fts()
        store.delete_record(record.id)
    assert any(sql.startswith("BEGIN IMMEDIATE") for sql in statements)
    assert violations == []


def test_event_append_failure_rolls_back_sql_but_keeps_prior_body(tmp_path, monkeypatch):
    store, door = make_store(tmp_path)
    injected = OSError("event append after SQL insert")
    def fail(*args, **kwargs):
        raise injected
    monkeypatch.setattr(schema_module, "append_private_jsonl_records_unlocked", fail)
    with global_write_commit_scope(door), pytest.raises(OSError) as caught:
        run_operation(store, "upsert")
    assert caught.value is injected
    assert read_sql(store, "SELECT id FROM records") == []
    assert read_sql(store, "SELECT event_type FROM events") == []
    assert store._content_file(store.make_record_id("memory", "new")).read_text() == "新的长期记忆正文"


def test_event_jsonl_before_failed_db_commit_is_honest_partial(tmp_path, monkeypatch):
    store, door = make_store(tmp_path)
    injected = sqlite3.OperationalError("after original event append")
    original = store._append_coordinator_event_records
    def append_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise injected
    monkeypatch.setattr(store, "_append_coordinator_event_records", append_then_fail)
    with global_write_commit_scope(door), pytest.raises(sqlite3.OperationalError) as caught:
        run_operation(store, "upsert")
    assert caught.value is injected
    assert read_sql(store, "SELECT id FROM records") == []
    assert json.loads(store.events_path.read_text().splitlines()[-1])["event_type"] == "record_upserted"


@pytest.mark.parametrize("change", ["insert", "update", "delete", "body", "state"])
def test_rebuild_cas_conflict_before_delete_event_or_pending_clear(tmp_path, monkeypatch, change):
    store, door = make_store(tmp_path)
    with global_write_commit_scope(door):
        record = store.upsert_record(source_type="memory", source_id="old", title="旧记录", content="旧的检索正文")
    before_fts = read_sql(store, "SELECT * FROM records_fts")
    before_events = store.events_path.read_bytes()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE metadata SET value='true' WHERE key='records_fts_rebuild_pending'")
    store._fts_needs_rebuild = True
    actual = store._prepare_fts_rebuild
    def prepare_then_change():
        snapshot = actual()
        if change == "body":
            path = store._content_file(record.id)
            replacement = path.with_suffix(".replacement")
            replacement.write_text("正文先改而SQL还没变", encoding="utf-8")
            replacement.replace(path)
        else:
            with sqlite3.connect(store.db_path) as conn:
                if change == "insert":
                    conn.execute("INSERT INTO records SELECT 'new',source_type,source_id,title,content_path,content_hash,content_preview,metadata_json,visibility,created_at,updated_at FROM records LIMIT 1")
                elif change == "update":
                    conn.execute("UPDATE records SET metadata_json=? WHERE id=?", ('{"changed":true}', record.id))
                elif change == "delete":
                    conn.execute("DELETE FROM records WHERE id=?", (record.id,))
                else:
                    conn.execute("UPDATE metadata SET value='changed' WHERE key='records_fts_schema_version'")
        return snapshot
    monkeypatch.setattr(store, "_prepare_fts_rebuild", prepare_then_change)
    with global_write_commit_scope(door), pytest.raises(BlockingIOError):
        store.rebuild_fts()
    assert read_sql(store, "SELECT * FROM records_fts") == before_fts
    assert store.events_path.read_bytes() == before_events
    assert read_sql(store, "SELECT value FROM metadata WHERE key='records_fts_rebuild_pending'") == [("true",)]
    assert store._fts_needs_rebuild


def test_rebuild_does_not_conflict_on_unrelated_event(tmp_path, monkeypatch):
    store, door = make_store(tmp_path)
    with global_write_commit_scope(door):
        run_operation(store, "upsert")
    actual = store._prepare_fts_rebuild
    def prepare_then_event():
        snapshot = actual()
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("INSERT INTO events(event_id,event_type,payload_json,created_at) VALUES('unrelated','note','{}',0)")
        return snapshot
    monkeypatch.setattr(store, "_prepare_fts_rebuild", prepare_then_event)
    with global_write_commit_scope(door):
        assert store.rebuild_fts() == 1


def test_rebuild_preparation_body_change_raises_before_write(tmp_path, monkeypatch):
    store, door = make_store(tmp_path)
    with global_write_commit_scope(door):
        run_operation(store, "upsert")
    read = store._read_content
    def read_then_replace(row):
        content = read(row)
        path = store._resolve_content_path(row["content_path"])
        tmp = path.with_suffix(".new")
        tmp.write_text("变化", encoding="utf-8")
        tmp.replace(path)
        return content
    monkeypatch.setattr(store, "_read_content", read_then_replace)
    before = store.events_path.read_bytes()
    with global_write_commit_scope(door), pytest.raises(BlockingIOError):
        store.rebuild_fts()
    assert store.events_path.read_bytes() == before


def test_typed_raw_snapshot_does_not_fold_numeric_or_bytes_values():
    assert not _typed_rebuild_rows_equal(((1,),), ((1.0,),))
    assert not _typed_rebuild_rows_equal(((b"a",),), (("a",),))
    assert _typed_rebuild_rows_equal(((None, "{ }"),), ((None, "{ }"),))


def test_rebuild_digest_and_root_measurement_are_actual(tmp_path, record_property, monkeypatch):
    store, door = make_store(tmp_path)
    with global_write_commit_scope(door):
        for index in range(12):
            store.upsert_record(source_type="memory", source_id=str(index), title=f"标题{index}", content="中文正文" * (index + 1))
        snapshot = store._prepare_fts_rebuild()
    assert snapshot.content_bytes == sum(len(content.encode()) for _, _, content in snapshot.contents)
    assert [row[2] for row in snapshot.versions] == [hashlib.sha256(content.encode()).hexdigest() for _, _, content in snapshot.contents]
    actual_read = store._read_content
    def read(row):
        assert not door.inside
        return actual_read(row)
    monkeypatch.setattr(store, "_read_content", read)
    start = len(door.durations)
    with global_write_commit_scope(door):
        assert store.rebuild_fts() == 12
    duration = max(door.durations[start:])
    record_property("records", 12)
    record_property("content_bytes", snapshot.content_bytes)
    record_property("max_root_seconds", duration)
    assert duration >= 0


def test_rebuild_fts_migration_pending_recovers_with_c_strategy(tmp_path, monkeypatch):
    path = tmp_path / "index.db"
    initial = LocalStore(path, enable_fts=False)
    initial.upsert_record(source_type="memory", source_id="x", title="原记录", content="原有中文正文事实")
    door = Door()
    with global_write_commit_scope(door):
        enabled = LocalStore(path, coordinator_io=True)
        assert enabled._fts_needs_rebuild is False
        assert enabled.rebuild_fts() == 1
    assert read_sql(enabled, "SELECT title FROM records_fts") == [("原记录",)]
    monkeypatch.setattr(schema_module, "_FTS_SCHEMA_VERSION", "next")
    replace = LocalStore._replace_fts_row
    def fail_after_row(self, *args):
        replace(self, *args)
        raise sqlite3.OperationalError("mid rebuild")
    monkeypatch.setattr(LocalStore, "_replace_fts_row", fail_after_row)
    with global_write_commit_scope(door):
        failed = LocalStore(path, coordinator_io=True)
    assert failed._fts_needs_rebuild
    assert read_sql(failed, "SELECT value FROM metadata WHERE key='records_fts_rebuild_pending'") == [("true",)]
    monkeypatch.setattr(LocalStore, "_replace_fts_row", replace)
    with global_write_commit_scope(door):
        recovered = LocalStore(path, coordinator_io=True)
    assert not recovered._fts_needs_rebuild
    assert read_sql(recovered, "SELECT title FROM records_fts") == [("原记录",)]


def test_original_legacy_schema_migration_preserves_old_rows(tmp_path):
    path = tmp_path / "index.db"
    with sqlite3.connect(path) as conn:
        conn.execute(schema_module._CONTROL_PLANE_SQL[0].replace("legacy_agent_runs", "agent_runs"))
        conn.execute("INSERT INTO agent_runs(run_id,root_task_id,created_at,updated_at) VALUES('r','t',1,1)")
    door = Door()
    with global_write_commit_scope(door):
        store = LocalStore(path, coordinator_io=True)
    assert read_sql(store, "SELECT run_id FROM legacy_agent_runs") == [("r",)]


def test_prepare_fd_cleanup_still_runs_after_revocation_without_new_connection(tmp_path, monkeypatch):
    store, door = make_store(tmp_path)
    conn = None
    with global_write_commit_scope(door), pytest.raises(LocalControlCoordinatorUnavailable) as caught:
        with store._connection() as original:
            conn = original
            door.reject_at = door.calls + 1
            store._local_write(lambda: None)
    assert caught.value is door.error
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_cleanup_attempts_close_and_preserves_primary():
    primary = LocalControlCoordinatorUnavailable("primary")
    class Connection:
        in_transaction = True
        closed = False
        def rollback(self):
            raise KeyboardInterrupt("cleanup")
        def close(self):
            self.closed = True
    conn = Connection()
    schema_module._retire_coordinator_connection(conn, primary)
    assert conn.closed


@pytest.mark.parametrize("reject_offset,expected_rows,expected_file", [(5, 1, True), (6, 0, True), (7, 0, False)])
def test_delete_revocation_keeps_exact_body_sql_unlink_checkpoint_partial(tmp_path, reject_offset, expected_rows, expected_file):
    store, door = make_store(tmp_path)
    with global_write_commit_scope(door):
        record = store.upsert_record(source_type="memory", source_id="old", title="旧记录", content="删除前原文")
    path = store._content_file(record.id)
    door.reject_at = door.calls + reject_offset
    with global_write_commit_scope(door), pytest.raises(LocalControlCoordinatorUnavailable) as caught:
        store.delete_record(record.id)
    assert caught.value is door.error
    assert len(read_sql(store, "SELECT id FROM records")) == expected_rows
    assert path.exists() is expected_file
    if expected_file:
        assert path.read_text() == ""


def test_upsert_post_commit_read_source_failure_does_not_undo_sql(tmp_path):
    store, door = make_store(tmp_path)
    door.reject_at = door.calls + 5
    with global_write_commit_scope(door), pytest.raises(LocalControlCoordinatorUnavailable) as caught:
        run_operation(store, "upsert")
    assert caught.value is door.error
    assert len(read_sql(store, "SELECT id FROM records")) == 1
    assert json.loads(store.events_path.read_text().splitlines()[-1])["event_type"] == "record_upserted"
