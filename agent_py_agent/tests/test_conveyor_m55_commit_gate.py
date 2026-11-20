from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from agent_py_agent.agent.conversation.background_shutdown import (
    BackgroundCommitRefused,
    BackgroundShutdown,
    commit_transition_gate,
)
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.managed_dispatch_recovery import (
    open_canonical_conversation_store,
    open_legacy_conversation_store,
    reconcile_managed_dispatches,
)
from agent_py_agent.tests.test_gw_stress_remediation_cold_owner import (  # noqa: F401 - pytest fixture
    _schedule,
    managed_host,
)
from agent_py_agent.tests.test_rework_r9_managed_dispatch_recovery import _legacy_dispatch


def test_hot_managed_dispatch_stays_pending_after_pause(managed_host):  # noqa: F811
    host = managed_host
    supervisor = _schedule(host, "alice")
    home = LocalUserStore(host.supervisor._base_agent.home_paths.root).owner_home("alice")
    host.managed.get(host.owners["alice"])
    dispatch = _legacy_dispatch(home, "Original dispatch")
    legacy = open_legacy_conversation_store(home)
    wake = legacy.storage.wake_signal_path(dispatch.legacy_wake)
    original = wake.stat().st_mtime_ns
    assert supervisor._shutdown_control.pause_for_restart() == []

    # 相当于外层 tick 在重启前已通过预检，热分支在暂停后才继续运行。
    supervisor._sync_owner_schedulers()
    canonical = open_canonical_conversation_store(home, initialize=False)
    assert canonical.tasks.load(dispatch.task_id) is None
    assert legacy.wakes.pending_one(dispatch.legacy_wake.wake_signal_id) is not None
    assert wake.stat().st_mtime_ns == original

    successor = BackgroundShutdown()
    supervisor._shutdown_control = successor
    for _ in range(16):
        supervisor._sync_owner_schedulers()
        if legacy.wakes.pending_one(dispatch.legacy_wake.wake_signal_id) is None:
            break
    task = canonical.tasks.load(dispatch.task_id)
    assert task is not None and task.status == "active"
    assert canonical.wakes.delivery_receipt(dispatch.thread_id, dispatch.task_id) == "pending"
    assert legacy.wakes.pending_one(dispatch.legacy_wake.wake_signal_id) is None


def test_cold_managed_dispatch_refuses_retirement_at_saved_watermark(managed_host, monkeypatch):  # noqa: F811
    host = managed_host
    supervisor = _schedule(host, "alice")
    control = supervisor._shutdown_control
    owner = host.owners["alice"]
    home = LocalUserStore(host.supervisor._base_agent.home_paths.root).owner_home("alice")
    dispatch = _legacy_dispatch(home, "Cold dispatch")
    legacy = open_legacy_conversation_store(home)
    wake = legacy.storage.wake_signal_path(dispatch.legacy_wake)
    old = (wake.read_bytes(), wake.stat().st_mtime_ns)
    reconcile = host.managed.reconcile
    receipt = home.home_dir / "data" / "managed_dispatch_handoffs" / f"{dispatch.task_id}.json"
    published = []

    def interleave(owner, *, commit_transition):
        def commit(operation):
            if published:
                with control.lock:
                    control.restart_paused = True
                return commit_transition(operation)
            result = commit_transition(operation)
            if result is not False:
                published.append(receipt.read_bytes())
            return result

        assert owner == host.owners["alice"]
        return reconcile_managed_dispatches(home, commit_transition=commit, max_ms=5_000)

    monkeypatch.setattr(host.managed, "reconcile", interleave)
    with pytest.raises(BackgroundCommitRefused) as refusal:
        supervisor._warm_owner(host.managed, owner, True)
    assert refusal.value.owner == home.owner_id
    assert refusal.value.watermark["task_id"] == dispatch.task_id
    assert refusal.value.watermark["phase"] == "prepared"
    assert refusal.value.watermark["deadline"] > 0
    assert (wake.read_bytes(), wake.stat().st_mtime_ns) == old
    assert receipt.read_bytes() == published[0]
    canonical = open_canonical_conversation_store(home, initialize=False)
    assert canonical.wakes.delivery_receipt(dispatch.thread_id, dispatch.task_id) == "pending"

    monkeypatch.setattr(host.managed, "reconcile", reconcile)
    with pytest.raises(InterruptedError):
        supervisor._warm_owner(host.managed, owner, True)
    assert receipt.read_bytes() == published[0]
    supervisor._shutdown_control = BackgroundShutdown()
    for _ in range(16):
        supervisor._warm_owner(host.managed, owner, True)
        if legacy.wakes.pending_one(dispatch.legacy_wake.wake_signal_id) is None:
            break
    assert legacy.wakes.pending_one(dispatch.legacy_wake.wake_signal_id) is None
    assert canonical.wakes.delivery_receipt(dispatch.thread_id, dispatch.task_id) == "pending"
    assert len(canonical.wakes.pending()) == 1


def test_c128_commit_gate(tmp_path, monkeypatch):
    path = tmp_path / "maintenance.sqlite3"
    workers = 128
    preadmitted = 8
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE work (id INTEGER PRIMARY KEY, status TEXT, watermark INTEGER, deadline INTEGER)")
        conn.executemany("INSERT INTO work(id,status,watermark,deadline) VALUES (?,'pending',?,?)",
                         ((i, i + 100, 9000 + i) for i in range(workers)))
        conn.commit()

    control = BackgroundShutdown()
    start = threading.Barrier(workers + 1, timeout=15)
    writers = threading.Barrier(preadmitted + 1, timeout=15)
    intent, release, leased = threading.Event(), threading.Event(), threading.Event()
    observe = control.blockers

    def blockers():
        if control.restart_paused:
            intent.set()
        return observe()

    monkeypatch.setattr(control, "blockers", blockers)

    def persist(i):
        with closing(sqlite3.connect(path, timeout=5)) as conn:
            conn.execute("UPDATE work SET status='done' WHERE id=? AND status='pending'", (i,))
            conn.commit()

    def race(i):
        start.wait()
        mark = (i + 100, 9000 + i)
        if i >= preadmitted:
            assert intent.wait(10)

        def operation():
            assert i < preadmitted
            writers.wait()
            assert release.wait(10)
            persist(i)
            assert not leased.is_set()

        with control.commit_scope():
            try:
                commit_transition_gate(operation, owner=f"owner-{i}", watermark=mark)
            except BackgroundCommitRefused as error:
                return error.owner, error.watermark
        return None

    def drain():
        result = control.pause_for_restart()
        if not result:
            leased.set()
        return result

    with ThreadPoolExecutor(max_workers=workers) as pool, ThreadPoolExecutor(max_workers=1) as coordinator:
        futures = [pool.submit(race, i) for i in range(workers)]
        start.wait()
        writers.wait()
        draining = coordinator.submit(drain)
        try:
            assert intent.wait(5)
            assert not leased.is_set()
        finally:
            release.set()
        assert draining.result(timeout=10) == []
        outcomes = [future.result(timeout=10) for future in futures]

    assert all(outcome is None for outcome in outcomes[:preadmitted])
    assert outcomes[preadmitted:] == [(f"owner-{i}", (i + 100, 9000 + i)) for i in range(preadmitted, workers)]
    assert control.blockers() == []
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("SELECT id,status,watermark,deadline FROM work ORDER BY id").fetchall()
    assert rows == [(i, "done" if i < preadmitted else "pending", i + 100, 9000 + i) for i in range(workers)]

    stamp = path.stat().st_mtime_ns
    with control.commit_scope(), pytest.raises(BackgroundCommitRefused):
        commit_transition_gate(lambda: persist(preadmitted), owner="retired")
    assert path.stat().st_mtime_ns == stamp

    successor = BackgroundShutdown()
    with successor.commit_scope():
        for i in range(preadmitted, workers):
            commit_transition_gate(lambda: persist(i), owner=f"owner-{i}", watermark=(i + 100, 9000 + i))
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM work WHERE status='done'").fetchone() == (workers,)


def test_process_drain_phase_refuses_before_background_pause():
    from agent_py_agent.agent.gateway_parts.restart_service import (
        reset_restart_drain_state,
        run_restart_drain,
    )

    control = BackgroundShutdown()
    entered = []
    watermark = {"cursor": "original", "deadline": 42}

    def progress(facts):
        entered.append(facts["phase"])
        with control.commit_scope(), pytest.raises(BackgroundCommitRefused) as refusal:
            commit_transition_gate(lambda: pytest.fail("commit ran during drain"),
                                   owner="local/alice", watermark=watermark)
        assert refusal.value.watermark is watermark

    try:
        result = run_restart_drain({"request_id": "test-drain"}, active_turns=lambda: 0,
                                   turn_wait_seconds=0, drain_timeout_seconds=1, on_progress=progress)
        assert result["ok"]
        assert entered == ["turn_wait", "tool_drain"]
        assert control.pause_for_restart() == []
    finally:
        reset_restart_drain_state()


def test_drain_uses_original_effect_classifier_for_legacy_failure(tmp_path):
    import json

    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / "runtime.db")
    record = repo.record_run_creation(owner_id="local/main", run_id="test-run", goal="test")
    attempt = record["attempt_id"]
    with repo.transaction() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id,agent_run_id,attempt_id,attempt_generation,"
            "tool_operation_generation,operation_type,status,handler_started_at,settled_at,"
            "outcome_json,created_at,updated_at) VALUES('op',?,?,1,1,'fixture','FAILED',1,1,?,1,1)",
            (record["agent_run_id"], attempt, json.dumps({"result": {"effect_outcome": "unknown"}})),
        )
    assert repo.unsettled_attempt_operations(attempt) == []
    assert repo.attempt_has_uncertain_operation_effects(attempt)
    with repo.transaction() as conn:
        conn.execute("UPDATE tool_operations SET outcome_json=? WHERE operation_id='op'",
                     (json.dumps({"result": {"effect_outcome": "failed"}}),))
    assert not repo.attempt_has_uncertain_operation_effects(attempt)
