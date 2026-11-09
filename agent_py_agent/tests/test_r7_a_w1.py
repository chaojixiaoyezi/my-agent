"""纯控制面查询不借写权限，真实 SQLite 的未知和 WAL 语义保持。"""
import os
import shutil
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace

import pytest

from agent_py_agent.agent.common.global_write_commit import (
    GlobalWriteCommitUnavailable,
    global_write_commit_scope,
)
from agent_py_agent.agent.io.sqlite_connection import sqlite_error_details
from agent_py_agent.agent.local_storage import schema
from agent_py_agent.agent.local_storage.control_plane_codec import (
    AGENT_RUN_UPSERT_SQL,
    agent_run_values,
)
from agent_py_agent.agent.local_storage.control_plane_models import (
    AgentRunRecord,
    AgentRuntimeQueryContext,
    TaskRollupRecord,
)
from agent_py_agent.tests.test_local_store_coordinator_io import make_store


@pytest.fixture
def populated(tmp_path):
    store, door = make_store(tmp_path / "store", enable_fts=False)
    run = AgentRunRecord("child", "task", parent_run_id="parent", depth=1, status="RUNNING",
                         created_at=1.0, updated_at=1.0)
    rollup = TaskRollupRecord("task", status="RUNNING", running_agents=1, updated_at=1.0)
    with global_write_commit_scope(door):
        store.upsert_agent_run(run)
        store.upsert_task_rollup(rollup)
    return store, door, run, rollup


def test_public_tree_read_needs_no_writer_scope(populated):
    store, door, run, rollup = populated
    calls = door.calls
    report = store.list_agent_tree("task")
    assert report.runs == [run] and report.rollup == rollup
    assert store.get_agent_run(run.run_id) == run
    assert store.get_task_rollup("task") == rollup
    assert store.list_subtree(run.run_id).runs == [run]
    assert store.list_blocked_runs("task") == []
    assert store.query_agent_runtime(AgentRuntimeQueryContext(root_task_id="task")).report == report
    assert door.calls == calls


def test_passive_tree_has_only_selects_and_no_filesystem_writers(populated, monkeypatch, record_property):
    store, door, run, _rollup = populated
    actual_connect = sqlite3.connect
    connections, statements = [], []

    def connect(*args, **kwargs):
        conn = actual_connect(*args, **kwargs)
        connections.append((args, kwargs, conn))
        conn.set_trace_callback(statements.append)
        return conn

    def forbidden(*args, **kwargs):
        pytest.fail("passive query entered a filesystem/schema writer")

    # 保留一个真实 WAL 连接，避免把 SQLite 自身首读创建共享内存混入应用写者计数。
    with closing(actual_connect(store.db_path)) as keeper:
        keeper.execute("SELECT run_id FROM legacy_agent_runs").fetchall()
        before = {str(path.relative_to(store.root)): (path.stat().st_mode, path.stat().st_size,
                  path.stat().st_mtime_ns) for path in store.root.rglob("*")}
        with monkeypatch.context() as patch:
            patch.setattr(schema.sqlite3, "connect", connect)
            patch.setattr(schema, "ensure_private_dir", forbidden)
            patch.setattr(schema, "_private_sqlite_files", forbidden)
            patch.setattr(store, "_ensure_schema", forbidden)
            patch.setattr(schema.os, "chmod", forbidden)
            patch.setattr(schema.os, "fchmod", forbidden, raising=False)
            report = store.list_agent_tree("task")
        after = {str(path.relative_to(store.root)): (path.stat().st_mode, path.stat().st_size,
                 path.stat().st_mtime_ns) for path in store.root.rglob("*")}
        assert report.runs == [run] and before == after
    # 树和汇总共享一次只读连接，两条查询的写权限与关闭证明仍分别核验。
    assert len(connections) == 1 and len(statements) == 2
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    for args, kwargs, conn in connections:
        assert "mode=ro" in args[0] and "immutable" not in args[0] and kwargs["uri"] is True
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    record_property("passive_connects", len(connections))
    record_property("passive_sql_statements", statements)
    record_property("filesystem_changed_paths", [])


def test_passive_reader_does_not_grant_existing_writers_authority(populated):
    store, _door, run, rollup = populated
    for write, value in ((store.upsert_agent_run, replace(run, status="DONE")),
                         (store.upsert_task_rollup, replace(rollup, status="DONE"))):
        with pytest.raises(GlobalWriteCommitUnavailable):
            write(value)
    with pytest.raises(GlobalWriteCommitUnavailable):
        store.count_records()
    with store._passive_connection() as conn, pytest.raises(sqlite3.OperationalError) as caught:
        conn.execute("DELETE FROM legacy_agent_runs")
    assert caught.value.sqlite_errorcode == sqlite3.SQLITE_READONLY
    assert store.get_agent_run(run.run_id) == run


@pytest.mark.parametrize("missing", ["database", "directory"])
def test_missing_passive_store_is_not_recreated(populated, missing):
    store, _door, _run, _rollup = populated
    if missing == "database":
        store.db_path.unlink()
    else:
        shutil.rmtree(store.root)
    with pytest.raises((FileNotFoundError, sqlite3.OperationalError)):
        store.list_agent_tree("task")
    assert not store.db_path.exists()
    if missing == "directory":
        assert not store.root.exists()


def test_corruption_is_explicit_and_does_not_repair(populated):
    store, _door, _run, _rollup = populated
    store.db_path.write_bytes(b"not a SQLite database" * 256)
    before = store.db_path.stat()
    with pytest.raises(sqlite3.DatabaseError) as caught:
        store.list_agent_tree("task")
    assert sqlite_error_details(caught.value)["category"] == "structural"
    after = store.db_path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_unavailable_source_preserves_the_original_error(populated, monkeypatch):
    store, _door, _run, _rollup = populated
    error = PermissionError("SQLite source unavailable")

    def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(schema.sqlite3, "connect", unavailable)
    with pytest.raises(PermissionError) as caught:
        store.list_agent_tree("task")
    assert caught.value is error and caught.value.__cause__ is None


@pytest.mark.parametrize("kind", ["database_link", "wal_link", "hardlink"])
def test_non_plain_sqlite_files_are_not_read_or_repaired(populated, kind):
    store, _door, _run, _rollup = populated
    if kind == "hardlink":
        os.link(store.db_path, store.db_path.with_suffix(".link"))
    elif kind == "database_link":
        target = store.db_path.with_suffix(".saved")
        store.db_path.rename(target)
        store.db_path.symlink_to(target)
    else:
        target = store.root / "other-wal"
        target.touch()
        wal = store.db_path.with_name(store.db_path.name + "-wal")
        wal.unlink(missing_ok=True)
        wal.symlink_to(target)
    with pytest.raises(ValueError, match="SQLite"):
        store.list_agent_tree("task")


def test_busy_store_does_not_become_a_healthy_empty_tree(populated):
    store, door, run, _rollup = populated
    with global_write_commit_scope(door), store._connection() as writer:
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError) as caught:
            store.list_agent_tree("task")
        assert sqlite_error_details(caught.value)["category"] in {"busy", "locked"}
        writer.rollback()
    assert store.list_agent_tree("task").runs == [run]


def test_wal_reader_sees_committed_state_while_writer_is_active(populated):
    store, door, run, _rollup = populated
    ready, release = threading.Event(), threading.Event()
    errors = []

    def write():
        try:
            with global_write_commit_scope(door), store._connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE legacy_agent_runs SET status='DONE' WHERE run_id=?", (run.run_id,))
                ready.set()
                assert release.wait(10)
                conn.commit()
        except BaseException as error:
            errors.append(error)

    with closing(sqlite3.connect(store.db_path)) as keeper:
        assert keeper.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        thread = threading.Thread(target=write)
        thread.start()
        try:
            assert ready.wait(10)
            assert store.get_agent_run(run.run_id).status == "RUNNING"
        finally:
            release.set()
            thread.join(10)
        assert not thread.is_alive() and not errors
        assert store.get_agent_run(run.run_id).status == "DONE"


@pytest.mark.parametrize("failure", [False, True])
def test_passive_connections_close_on_normal_and_exceptional_paths(populated, failure, record_property):
    store, _door, _run, _rollup = populated
    fd_dir = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    before = len(os.listdir(fd_dir)) if os.path.isdir(fd_dir) else None
    for _ in range(16):
        if failure:
            with pytest.raises(sqlite3.OperationalError), store._passive_connection() as conn:
                conn.execute("SELECT * FROM missing_table")
        else:
            with store._passive_connection() as conn:
                assert conn.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    if before is not None:
        delta = len(os.listdir(fd_dir)) - before
        assert delta == 0
        record_property("fd_delta", delta)


@pytest.mark.parametrize("history", [100, 1000, 10000])
def test_selected_tree_opens_are_independent_of_unrelated_history(populated, monkeypatch, history, record_property):
    store, door, run, _rollup = populated
    with global_write_commit_scope(door), store._connection() as conn:
        conn.executemany(AGENT_RUN_UPSERT_SQL, (
            agent_run_values(AgentRunRecord(f"old-{index}", "other", status="DONE"), 1.0, 1.0)
            for index in range(history)))
        conn.commit()
    original, opened = sqlite3.connect, []

    def connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(schema.sqlite3, "connect", connect)
    before = door.calls
    assert store.list_agent_tree("task").runs == [run]
    assert len(opened) == 1 and door.calls == before
    record_property("history_rows", history)
    record_property("passive_connects", len(opened))
