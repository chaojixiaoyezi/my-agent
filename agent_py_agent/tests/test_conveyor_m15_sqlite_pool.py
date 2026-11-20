"""M15 的 SQLite 连接生命周期与监督分页回归。"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.dispatch import capability_auto_sweep as sweep
from agent_py_agent.agent.conversation.runtime import BackgroundMainAgentScheduler
from agent_py_agent.agent.io import sqlite_pool
from agent_py_agent.agent.io.sqlite_scope import retire_scoped_database
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.persistence import service
from agent_py_agent.agent.subagents.services.persistence.run_index import (
    SubagentRunIndex,
    SubagentRunIndexUnavailable,
)
from agent_py_agent.agent.subagents.services.persistence.service import SubAgentPersistenceService


@pytest.fixture(autouse=True)
def _retire_test_connections():
    yield
    sqlite_pool.retire_idle_connections(now=sqlite_pool._now() + sqlite_pool.SQLITE_POOL_IDLE_TIMEOUT_SECONDS)


def _seed_running(manager, count):
    index = manager.persistence.run_index
    with index.connect() as db:
        for number in range(count):
            run_id = f"run-{number:03d}"
            directory = manager.workspace / run_id
            directory.mkdir()
            (directory / "task.json").write_text(json.dumps({
                "id": run_id, "goal": "selection fixture", "thought": "", "plan": [],
                "status": "RUNNING", "created_at": 1.0, "updated_at": 2.0,
            }), encoding="utf-8")
            index.upsert(manager.load(run_id), connection=db)
    return index


def _readonly_manager(workspace):
    manager = SubAgentManager(workspace)
    manager.persistence = SubAgentPersistenceService(manager, read_only=True)
    return manager


def _database_fd_count(path):
    directory = Path("/dev/fd")
    if not directory.exists():
        return None
    expected = path.stat()
    identity = expected.st_dev, expected.st_ino
    count = 0
    with os.scandir(directory) as descriptors:
        for descriptor in descriptors:
            try:
                info = os.fstat(int(descriptor.name))
            except (OSError, ValueError):
                continue
            count += (info.st_dev, info.st_ino) == identity
    return count


def test_reuses_one_sqlite_open_for_500_checkouts(tmp_path, monkeypatch):
    index = SubagentRunIndex(tmp_path)
    real_connect = sqlite3.connect
    opens = []

    def observed_connect(*args, **kwargs):
        opens.append(args[0])
        return real_connect(*args, **kwargs)

    def checkouts():
        for _ in range(500):
            with index.connect() as db:
                assert db.execute("SELECT 1").fetchone() == (1,)

    monkeypatch.setattr(sqlite3, "connect", observed_connect)
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(checkouts).result()
    assert len(opens) == 1


def test_readonly_supervision_defers_one_pending_row_in_fifty(tmp_path):
    manager = SubAgentManager(tmp_path)
    index = _seed_running(manager, 50)
    cold = _readonly_manager(tmp_path)
    index.mark_pending("run-017")

    with pytest.raises(SubagentRunIndexUnavailable, match="pending canonical commits"):
        index.candidate_ids(statuses=("RUNNING",), limit=50)

    errors = []
    runs = sweep._select_supervision_current_runs(cold, statuses=("RUNNING",), errors_out=errors)
    assert {run.id for run in runs} == {f"run-{n:03d}" for n in range(50)} - {"run-017"}
    assert len(runs) == 49
    report = cold.list_current_runs_report(statuses=("RUNNING",), tolerate_pending=True)
    assert report.pending_ids == ["run-017"]
    assert report.complete and not report.load_errors
    assert [(err["run_id"], err["stage"], err["error_code"]) for err in errors] == [
        ("run-017", "current_selection", SubagentRunIndexUnavailable.error_code),
    ]
    index.upsert(manager.load("run-017"))
    assert len(sweep._select_supervision_current_runs(cold, statuses=("RUNNING",))) == 50


def test_inode_swap_and_scoped_retirement_reopen_connections(tmp_path):
    original = SubagentRunIndex(tmp_path / "original")
    replacement = SubagentRunIndex(tmp_path / "replacement")
    with original.connect() as old:
        old_revision = original._membership_revision(old)
    replacement_revision = replacement.current_membership_revision()
    assert replacement_revision != old_revision
    sqlite_pool.invalidate(replacement.path)
    replacement.path.replace(original.path)

    with original.connect() as swapped:
        assert swapped is not old
        assert original._membership_revision(swapped) == replacement_revision
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        old.execute("SELECT 1")

    assert retire_scoped_database(original.path)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        swapped.execute("SELECT 1")
    with original.connect() as reopened:
        assert reopened is not swapped
        assert original._membership_revision(reopened) == replacement_revision
        sqlite_pool.invalidate(original.path)
        assert reopened.execute("SELECT 1").fetchone() == (1,)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        reopened.execute("SELECT 1")


def test_idle_retirement_from_another_thread_skips_active_lease(tmp_path, monkeypatch):
    index = SubagentRunIndex(tmp_path)
    sqlite_pool.invalidate(index.path)
    now = [sqlite_pool._now()]
    monkeypatch.setattr(sqlite_pool, "_now", lambda: now[0])
    idle_window = sqlite_pool.SQLITE_POOL_IDLE_TIMEOUT_SECONDS
    leased, release, returned, finish = (Event() for _ in range(4))
    connections = []

    def worker():
        with index.connect() as db:
            connections.append(db)
            leased.set()
            assert release.wait(15)
            assert db.execute("SELECT 1").fetchone() == (1,)
        returned.set()
        assert finish.wait(15)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker)
        try:
            assert leased.wait(15)
            now[0] += idle_window
            sqlite_pool.retire_idle_connections()
            if (count := _database_fd_count(index.path)) is not None:
                assert count == 1
            release.set()
            assert returned.wait(15)
            assert not future.done()
            assert sqlite_pool.retire_idle_connections(now=now[0] + idle_window - 1) == 0
            now[0] += idle_window
            assert sqlite_pool.retire_idle_connections() == 1
            if (count := _database_fd_count(index.path)) is not None:
                assert count == 0
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connections[0].execute("SELECT 1")
        finally:
            release.set()
            finish.set()
        future.result()


def test_128_workers_open_at_most_one_connection_each(tmp_path, monkeypatch):
    index = SubagentRunIndex(tmp_path)
    sqlite_pool.invalidate(index.path)
    real_connect = sqlite3.connect
    guard = Lock()
    opens = []

    def observed_connect(*args, **kwargs):
        with guard:
            opens.append(args[0])
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", observed_connect)
    workers = 128
    ready = Barrier(workers)

    def checkouts():
        ready.wait(timeout=30)
        for _ in range(200):
            with index.connect() as db:
                assert db.execute("SELECT 1").fetchone() == (1,)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = [executor.submit(checkouts) for _ in range(workers)]
        for result in results:
            result.result(timeout=45)
    assert len(opens) == workers
    if (count := _database_fd_count(index.path)) is not None:
        assert 0 < count <= workers
    assert sqlite_pool.retire_idle_connections(now=sqlite_pool._now() + sqlite_pool.SQLITE_POOL_IDLE_TIMEOUT_SECONDS) == workers
    if (count := _database_fd_count(index.path)) is not None:
        assert count == 0


def test_writer_transaction_rolls_back_and_does_not_hold_the_read_guard(tmp_path):
    index = SubagentRunIndex(tmp_path)
    writer_entered, writer_release = Event(), Event()

    def write_then_rollback():
        with pytest.raises(ValueError, match="rollback fixture"), index.connect(write=True) as db:
            assert db.in_transaction and db.execute("PRAGMA busy_timeout").fetchone() == (5000,)
            db.execute("UPDATE meta SET value=0 WHERE key='complete'")
            writer_entered.set()
            assert writer_release.wait(15)
            raise ValueError("rollback fixture")

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(write_then_rollback)
        try:
            assert writer_entered.wait(15)
            # 此线程在构造时已缓存连接；写事务正打开时仍能读到已提交内容。
            assert index.readiness() == {"complete": True, "pending": False}
        finally:
            writer_release.set()
        future.result()
    assert index.readiness()["complete"]
    with index.connect(write=True) as db:
        db.execute("UPDATE meta SET value=0 WHERE key='complete'")
    assert not index.readiness()["complete"]


def test_readonly_manager_pages_pending_and_committed_from_each_snapshot(tmp_path, monkeypatch):
    manager = SubAgentManager(tmp_path)
    index = _seed_running(manager, 121)
    cold = _readonly_manager(tmp_path)
    pending = {"run-049", "run-120", *(f"run-{n:03d}" for n in range(50, 100))}
    for run_id in sorted(pending):
        index.mark_pending(run_id)
    monkeypatch.setattr(service, "RETENTION_SCAN_SLICE_ENTRIES", 50)
    reader = cold.persistence.run_index
    original = reader.candidate_page
    pages = []

    def observed_page(**kwargs):
        assert kwargs["limit"] == 50
        page = original(**kwargs)
        pages.append(page)
        with index.connect(write=True) as db:
            assert index._membership_revision(db) == page.generation
            db.execute("UPDATE meta SET value=value+1 WHERE key='current_membership_revision'")
        return page

    monkeypatch.setattr(reader, "candidate_page", observed_page)
    report = cold.list_current_runs_report(statuses=("RUNNING",), tolerate_pending=True)
    assert not report.load_errors and report.complete and report.coverage == "complete"
    assert report.pending_ids == sorted(pending)
    assert [run.id for run in report.runs] == [f"run-{n:03d}" for n in range(121) if f"run-{n:03d}" not in pending]
    assert [page.after for page in pages] == ["run-049", "run-099", ""]
    assert not pages[1].run_ids
    assert [page.complete for page in pages] == [True, True, True]
    assert [page.generation for page in pages] == [pages[0].generation + offset for offset in range(3)]
    assert report.generation == str(pages[-1].generation)

    with index.connect(write=True) as db:
        db.execute("UPDATE runs SET pending=0")
    assert reader.candidate_ids(statuses=("RUNNING",), limit=2) == (["run-000", "run-001"], "run-001")
    with index.connect(write=True) as db:
        db.execute("UPDATE meta SET value=0 WHERE key='complete'")
    for tolerate in (False, True):
        with pytest.raises(SubagentRunIndexUnavailable, match="coverage is partial"):
            original(statuses=("RUNNING",), limit=50, tolerate_pending=tolerate)
        failed = cold.list_current_runs_report(statuses=("RUNNING",), tolerate_pending=tolerate)
        assert not failed.complete and not failed.runs
        assert failed.load_errors[0]["error_code"] == SubagentRunIndexUnavailable.error_code
    with pytest.raises(SubagentRunIndexUnavailable, match="coverage is partial"):
        reader.candidate_ids(statuses=("RUNNING",), limit=50)


def test_prewake_defers_exact_pending_run_and_records_stage(tmp_path, monkeypatch, caplog):
    manager = SubAgentManager(tmp_path)
    index = _seed_running(manager, 2)
    cold = _readonly_manager(tmp_path)
    index.mark_pending("run-000")
    monkeypatch.setattr(sweep, "auto_grant_open_requests", lambda *_: pytest.fail("pending run was granted"))
    monkeypatch.setattr(sweep, "auto_start_orphan_run", lambda *_: pytest.fail("pending run was launched"))
    scheduler = SimpleNamespace(runtime=SimpleNamespace(agent=SimpleNamespace(subagents=cold)))
    signal = SimpleNamespace(source_agent_id="run-000", metadata={"audit_source_worker": True})
    with caplog.at_level(logging.INFO):
        BackgroundMainAgentScheduler._pre_wake_capability_sweep(scheduler, "subagent_runner_finished", signal)
    assert "pre-wake capability sweep deferred runs" in caplog.text
    assert "pre_wake_exact_run" in caplog.text
    assert SubagentRunIndexUnavailable.error_code in caplog.text
    assert "run-000" in caplog.text


def test_tolerant_pages_advance_past_pending_and_missing_rows(tmp_path):
    manager = SubAgentManager(tmp_path)
    index = _seed_running(manager, 4)
    index.mark_pending("run-000a")
    with index.connect(write=True) as db:
        db.execute("INSERT INTO membership VALUES ('RUNNING','status','run-001a')")
    args = {"statuses": ("RUNNING",), "limit": 2, "tolerate_pending": True}
    first = index.candidate_page(**args)
    assert first.run_ids == ("run-000",) and first.pending_ids == ("run-000a",)
    second = index.candidate_page(after=first.after, **args)
    assert second.run_ids == ("run-001",) and second.after == "run-001a"
    third = index.candidate_page(after=second.after, **args)
    assert third.run_ids == ("run-002", "run-003") and not third.after
