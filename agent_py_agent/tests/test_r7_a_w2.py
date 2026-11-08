"""真实单一运行索引的当前集合、冷读、覆盖与 canonical 竞态回归。"""
import copy
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.subagents.manager import SubagentRunIndexUnavailable, request_scope_runs
from agent_py_agent.agent.subagents.services.persistence.run_index import SubagentRunIndex
from agent_py_agent.agent.subagents.services.persistence.service import SubAgentPersistenceService
from agent_py_agent.tests.test_subagent_listruns_cache import _selected_manager, _selected_task_file


def _task(manager, run_id, status="RUNNING", *, root="root", parent="", thread="thread", capability=""):
    path = _selected_task_file(manager, run_id, status)
    data = json.loads(path.read_text())
    data.update(root_id=root, parent_id=parent)
    data["attributes"]["conversation_thread_id"] = thread
    if capability:
        data["capability_requests"] = [{"id": "request-" + run_id, "from_run_id": run_id,
            "problem": "missing", "needed_capability": "tool", "status": capability}]
    path.write_text(json.dumps(data))
    old = time.time_ns() - 10_000_000_000
    os.utime(path, ns=(old, old))
    return manager.load(run_id)


def _current(manager, **selection):
    reader = getattr(manager, "list_current_runs_report", None)
    assert callable(reader), "missing frozen C-CURRENT provider"
    return reader(**selection)


@pytest.mark.parametrize("history", [100, 1000, 10000])
def test_exact_current_and_root_reads_are_independent_of_history(tmp_path, monkeypatch, record_property, history):
    manager = _selected_manager(tmp_path)
    index = manager.persistence.run_index
    statuses = ("RUNNING", "PLANNING", "PENDING", "BLOCKED", "WAITING_APPROVAL")
    with index.connect() as db:
        for number in range(history):
            task = _task(manager, f"old-{number}", "DONE", root="other", thread="other")
            index.upsert(task, connection=db)
        for number, status in enumerate((*statuses, "DONE", "DONE")):
            task = _task(manager, f"active-{number}", status, thread="" if number == 6 else "thread",
                         capability="OPEN" if number == 5 else "legacy-unknown" if number == 6 else "")
            index.upsert(task, connection=db)
    copies, reads, stats = [], [], []
    load, deepcopy, stat, glob = manager.persistence.load, copy.deepcopy, Path.stat, Path.glob

    def observed_load(run_id):
        reads.append(run_id)
        return load(run_id)

    def observed_copy(value, memo=None):
        if hasattr(value, "id"):
            copies.append(value.id)
        return deepcopy(value, memo)

    def observed_stat(path, *args, **kwargs):
        if path.name in {"task.json", "canonical_state.json"}:
            stats.append(path.parent.name)
        return stat(path, *args, **kwargs)

    def no_history(path, pattern, **kwargs):
        assert path != manager.workspace or pattern != "*/task.json", "indexed read scanned history"
        return glob(path, pattern, **kwargs)

    monkeypatch.setattr(manager.persistence, "load", observed_load)
    monkeypatch.setattr(index, "coverage", lambda: pytest.fail("hot query counted historical inventory"))
    monkeypatch.setattr(copy, "deepcopy", observed_copy)
    monkeypatch.setattr(Path, "stat", observed_stat)
    monkeypatch.setattr(Path, "glob", no_history)
    report = _current(manager, statuses=statuses, thread_id="thread", include_unbound_thread=True,
                      include_open_capabilities=True)
    expected = {f"active-{i}" for i in range(7)}
    assert not report.load_errors and {task.id for task in report.runs} == expected
    assert set(reads) == expected and len(reads) == len(copies) == 7
    assert set(stats) <= expected and len(stats) <= 7 * 6
    record_property("canonical_loads", len(reads))
    record_property("canonical_copies", len(copies))
    record_property("canonical_stats", len(stats))
    reads.clear()
    rooted = manager.list_runs_for_root_report("root")
    assert not rooted.load_errors and {task.id for task in rooted.runs} == expected
    assert not reads, "warm exact root lookup must retain the current cache"
    assert len(manager.persistence._run_cache) <= manager.persistence._MAX_RUN_CACHE_ENTRIES


def test_membership_union_thread_intersection_and_recovery_are_distinct(tmp_path):
    manager = _selected_manager(tmp_path)
    rows = [
        _task(manager, "blocked", "BLOCKED"), _task(manager, "approval", "WAITING_APPROVAL"),
        _task(manager, "terminal-open", "DONE", capability="OPEN"),
        _task(manager, "terminal-unknown", "DONE", capability="DENIED"),
        _task(manager, "unbound", "RUNNING", thread=""),
        _task(manager, "foreign", "RUNNING", thread="elsewhere"),
        _task(manager, "closed", "DONE", capability="CLOSED"),
    ]
    with manager.persistence.run_index.connect() as db:
        for task in rows:
            manager.persistence.run_index.upsert(task, connection=db)
    selection = dict(statuses=("BLOCKED", "WAITING_APPROVAL", "RUNNING"), thread_id="thread",
                     include_open_capabilities=True)
    report = _current(manager, **selection)
    assert not report.load_errors
    assert {task.id for task in report.runs} == {"blocked", "approval", "terminal-open", "terminal-unknown"}
    selection["include_unbound_thread"] = True
    assert {task.id for task in _current(manager, **selection).runs} == {
        "blocked", "approval", "terminal-open", "terminal-unknown", "unbound"}
    recovery = manager.list_recovery_runs_report()
    assert not recovery.load_errors and "blocked" not in {task.id for task in recovery.runs}
    assert "terminal-open" not in {task.id for task in recovery.runs}
    after, selected = "", []
    while True:
        page, after = manager.persistence.run_index.candidate_ids(**selection, after=after, limit=2)
        assert len(page) <= 2
        selected.extend(page)
        if not after:
            break
    assert selected == sorted({task.id for task in _current(manager, **selection).runs})
    thread_only = _current(manager, thread_id="thread", include_unbound_thread=True)
    assert not thread_only.load_errors and "unbound" in {task.id for task in thread_only.runs}
    assert "closed" in {task.id for task in thread_only.runs} and "foreign" not in {task.id for task in thread_only.runs}
    assert _current(manager).load_errors


@pytest.mark.parametrize("damage", ["missing", "corrupt", "partial", "pending", "old-schema", "bad-coverage"])
def test_cold_existing_only_never_writes_or_falls_back(tmp_path, monkeypatch, damage):
    manager = _selected_manager(tmp_path)
    task = _task(manager, "one")
    index = manager.persistence.run_index
    index.upsert(task)
    if damage == "missing":
        index.path.unlink()
    elif damage == "corrupt":
        index.path.write_bytes(b"not sqlite")
    else:
        with index.connect() as db:
            if damage == "partial":
                db.execute("UPDATE meta SET value=0 WHERE key='complete'")
            elif damage == "pending":
                db.execute("UPDATE runs SET pending=1")
            elif damage == "bad-coverage":
                db.execute("UPDATE meta SET value=2 WHERE key='complete'")
            else:
                db.execute("UPDATE meta SET value=3 WHERE key='schema'")
    before = {path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in manager.workspace.iterdir()}
    monkeypatch.setattr(threading.Thread, "start", lambda *a: pytest.fail("cold query started maintenance"))
    cold = SubAgentPersistenceService(SimpleNamespace(workspace=manager.workspace))
    monkeypatch.setattr(cold, "list_runs_selected_report", lambda *a: pytest.fail("history fallback"))
    assert _current(cold, statuses=("RUNNING",)).load_errors
    assert cold.list_runs_for_root_report("root").load_errors
    after = {path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in manager.workspace.iterdir()}
    assert before == after


@pytest.mark.parametrize("explicit", [False, True])
def test_cold_valid_reads_are_independent_and_do_not_install_an_index(tmp_path, monkeypatch, explicit):
    manager = _selected_manager(tmp_path)
    task = _task(manager, "one")
    manager.persistence.run_index.upsert(task)
    before = {path.name for path in manager.workspace.iterdir()}
    monkeypatch.setattr(threading.Thread, "start", lambda *a: pytest.fail("cold query started maintenance"))
    cold = (SubAgentPersistenceService(manager, read_only=True) if explicit else
            SubAgentPersistenceService(SimpleNamespace(workspace=manager.workspace)))
    first = _current(cold, statuses=("RUNNING",))
    assert not first.load_errors and [task.id for task in first.runs] == ["one"]
    first.runs[0].attributes["nested"].append("caller")
    assert _current(cold, statuses=("RUNNING",)).runs[0].attributes["nested"] == []
    assert {path.name for path in manager.workspace.iterdir()} == before
    with pytest.raises(PermissionError):
        cold.save(task)
    missing = tmp_path / "never-created"
    absent = SubAgentPersistenceService(SimpleNamespace(workspace=missing))
    assert _current(absent, statuses=("RUNNING",)).load_errors and not missing.exists()


def test_old_schema_backfills_memberships_with_existing_coverage_protocol(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    task = _task(manager, "terminal-open", "DONE", capability="OPEN")
    index = manager.persistence.run_index
    index.upsert(task)
    with index.connect() as db:
        db.execute("DELETE FROM membership WHERE kind IN ('status','thread','capability')")
        db.execute("UPDATE meta SET value=3 WHERE key='schema'")
    cold = SubAgentPersistenceService(SimpleNamespace(workspace=manager.workspace))
    assert _current(cold, include_open_capabilities=True).load_errors
    monkeypatch.setattr(manager.persistence, "_start_run_index_maintenance", lambda: None)
    for _ in range(3):
        state = manager.persistence.backfill_run_index(batch_size=1)
        if state["complete"]:
            break
    assert state["complete"]
    report = _current(cold, include_open_capabilities=True)
    assert not report.load_errors and [task.id for task in report.runs] == ["terminal-open"]


@pytest.mark.parametrize("change", ["deleted", "status", "thread"])
def test_canonical_change_after_candidate_selection_is_explicit(tmp_path, monkeypatch, change):
    manager = _selected_manager(tmp_path)
    task = _task(manager, "one")
    index = manager.persistence.run_index
    index.upsert(task)
    original = index.candidate_ids

    def selected(**kwargs):
        result = original(**kwargs)
        path = manager.workspace / "one/task.json"
        if change == "deleted":
            path.unlink()
        else:
            data = json.loads(path.read_text())
            if change == "status":
                data["status"] = "DONE"
            else:
                data["attributes"]["conversation_thread_id"] = "other"
            path.write_text(json.dumps(data))
        return result

    monkeypatch.setattr(index, "candidate_ids", selected)
    report = _current(manager, statuses=("RUNNING",), thread_id="thread")
    assert not report.runs and report.load_errors and report.load_errors[0]["run_id"] == "one"


def test_root_parent_and_request_reads_revalidate_exact_canonical_relations(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    root = manager.create_run(goal="root")
    parent = manager.create_run(goal="parent", root_id=root.id, parent_id=root.id)
    child = manager.create_run(goal="child", root_id=root.id, parent_id=parent.id)
    other = manager.create_run(goal="other")
    monkeypatch.setattr(manager.persistence, "list_runs_selected_report", lambda *a: pytest.fail("full history"))
    report = manager.list_runs_for_root_report(root.id)
    assert not report.load_errors and {task.id for task in report.runs} == {root.id, parent.id, child.id}
    assert manager.persistence.run_index.active_child_ids(parent.id) == [child.id]
    assert manager.persistence.run_index.known_ids((child.id, "absent")) == {child.id}
    assert {task.id for task in request_scope_runs(manager, (parent.id,), "", None)} == {parent.id, child.id}
    source = manager.persistence.run_index.request_ids

    def stale(*args):
        return [*source(*args), other.id]

    monkeypatch.setattr(manager.persistence.run_index, "request_ids", stale)
    report = manager.list_runs_for_root_report(root.id)
    assert report.load_errors and other.id not in {task.id for task in report.runs}
    with pytest.raises(SubagentRunIndexUnavailable):
        request_scope_runs(manager, (root.id,), "", None)


def test_index_identity_change_during_selection_is_not_healthy_empty(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    index = manager.persistence.run_index
    index.upsert(_task(manager, "one"))
    replacement = SubagentRunIndex(tmp_path / "new-index")
    original = index.candidate_ids

    def changed(**kwargs):
        original(**kwargs)
        replacement.path.replace(index.path)
        return [], ""

    monkeypatch.setattr(index, "candidate_ids", changed)
    report = _current(manager, statuses=("RUNNING",))
    assert not report.runs and report.load_errors


def test_current_queries_preserve_existing_cache_bound(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    service = manager.persistence
    monkeypatch.setattr(service, "_MAX_RUN_CACHE_ENTRIES", 8)
    with service.run_index.connect() as db:
        for number in range(20):
            task = _task(manager, str(number), "RUNNING" if number == 0 else "DONE")
            service.run_index.upsert(task, connection=db)
    assert not manager.list_runs_report().load_errors
    assert len(service._run_cache) == 8
    assert not _current(manager, statuses=("RUNNING",)).load_errors
    assert len(service._run_cache) <= 8


@pytest.mark.parametrize("sidecars", ["complete", "missing-wal", "missing-shm"])
def test_cold_wal_reader_never_creates_missing_sidecars(tmp_path, sidecars):
    manager = _selected_manager(tmp_path)
    index = manager.persistence.run_index
    index.upsert(_task(manager, "one"))
    child = subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA journal_mode=WAL')
conn.execute("INSERT OR REPLACE INTO meta VALUES('fixture',1)")
conn.commit()
os._exit(0)
""", str(index.path)], capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr
    assert Path(f"{index.path}-wal").stat().st_size > 0 and Path(f"{index.path}-shm").exists()
    if sidecars != "complete":
        Path(f"{index.path}-" + ("wal" if sidecars == "missing-wal" else "shm")).unlink()
    before = {path.name for path in manager.workspace.iterdir()}
    cold = SubAgentPersistenceService(SimpleNamespace(workspace=manager.workspace))
    report = _current(cold, statuses=("RUNNING",))
    assert bool(report.load_errors) == (sidecars != "complete")
    if sidecars == "complete":
        assert [task.id for task in report.runs] == ["one"]
    assert {path.name for path in manager.workspace.iterdir()} == before


def test_existing_rollback_journal_reader_defers_without_uncommitted_rows(tmp_path):
    manager = _selected_manager(tmp_path)
    index = manager.persistence.run_index
    index.upsert(_task(manager, "one"))
    cold = SubAgentPersistenceService(SimpleNamespace(workspace=manager.workspace))
    with closing(sqlite3.connect(index.path)) as writer:
        try:
            writer.execute("BEGIN EXCLUSIVE")
            writer.execute("DELETE FROM runs")
            assert Path(f"{index.path}-journal").exists()
            before = {path.name for path in manager.workspace.iterdir()}
            report = _current(cold, statuses=("RUNNING",))
            assert report.load_errors and not report.runs
            assert {path.name for path in manager.workspace.iterdir()} == before
        finally:
            writer.rollback()
    assert [task.id for task in _current(cold, statuses=("RUNNING",)).runs] == ["one"]


def test_request_scope_preserves_terminal_retry_history_and_epoch(tmp_path):
    from agent_py_agent.agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
    from agent_py_agent.agent.subagents.services.persistence.run_index import request_selection

    manager = _selected_manager(tmp_path)
    with manager.persistence.run_index.connect() as db:
        for number, status in enumerate(("BLOCKED", "FAILED", "TIMEOUT", "FAILED")):
            task = _task(manager, str(number), status, root="scope" if number < 3 else "other")
            task.attributes.update({CONVERSATION_REQUEST_ID_ATTR: "request" if number < 3 else "other",
                                    "audit_run_epoch": 1 if number < 2 else 2})
            path = manager.workspace / task.id / "task.json"
            data = json.loads(path.read_text())
            data["attributes"] = task.attributes
            path.write_text(json.dumps(data))
            manager.persistence.run_index.upsert(manager.load(task.id), connection=db)
    assert {task.id for task in request_scope_runs(manager, ("scope",), "request", 1)} == {"0", "1"}
    agent = SimpleNamespace(_current_run_params=SimpleNamespace(request_id="", task_id="scope", run_id=""))
    assert request_selection(agent, "")[0] == {"scope"}
    assert {task.id for task in request_scope_runs(manager, *request_selection(agent, ""))} == {"0", "1", "2"}
