from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.dispatch import capability_auto_sweep
from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate
from agent_py_agent.agent.common.audit_activation import (
    AUDIT_ATTR,
    AUDIT_SOURCE_BINDING_PENDING_ATTR,
    AUDIT_SOURCE_ID_ATTR,
    AUDIT_SOURCE_OWNER_HOME_ATTR,
    AUDIT_SOURCE_WATCH_ID_ATTR,
    AUDIT_SOURCE_WORKER_ATTR,
    AUDIT_SOURCE_WORKER_KEY_ATTR,
    audit_source_worker_key,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from agent_py_agent.agent.ingestion.watch_state import new_state, persist_state, state_dir
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.process_control import is_pid_alive
from agent_py_agent.agent.subagents.runner_session_liveness import has_fresh_runner_session


def _session(status: str = "running", *, age_seconds: float = 0.0, interval: float = 5.0) -> dict:
    now = time.time()
    return {
        "schema_version": "runner_session_pool.v1",
        "session_id": "runsess-test",
        "run_id": "run-test",
        "worker_pid": 12345,
        "status": status,
        "heartbeat_at": now - age_seconds,
        "interval_seconds": interval,
        "started_at": now - age_seconds - 10,
        "ended_at": 0.0,
    }


def _task_with_session(session: dict | None, **fields) -> SimpleNamespace:
    attrs = {"runner_session": session} if session is not None else {}
    defaults = dict(
        id="run-test",
        status="PENDING",
        attributes=attrs,
        runner_active_attempt_id="",
        runner_attempts=0,
        capability_requests=[],
        capability_gaps=[],
        channel_status="",
        verification_status="UNVERIFIED",
    )
    defaults.update(fields)
    return SimpleNamespace(**defaults)


def _make_child(
    manager: SubAgentManager,
    *,
    status: str,
    session: dict | None = None,
    attempts: int = 0,
    attributes: dict[str, object] | None = None,
):
    task = manager.create_run(goal=f"liveness {status}", thought="liveness", plan=["run"], depth=1)
    task.status = status
    task.runner_attempts = attempts
    if status == "RUNNING":
        task.runner_active_attempt_id = "attempt-live-1"
    attrs = {**dict(task.attributes or {}), **dict(attributes or {})}
    if session is not None:
        attrs["runner_session"] = session
    task.attributes = attrs
    manager.save(task)
    return task


def _audit_source_attributes(
    owner_home: Path,
    *,
    audit_id: str,
    watch_id: str,
) -> dict[str, object]:
    return {
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: "source-stalled",
        AUDIT_SOURCE_WATCH_ID_ATTR: watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(audit_id, watch_id),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
    }


def _audit_source_binding_attributes(*, audit_id: str) -> dict[str, object]:
    return {
        AUDIT_ATTR: True,
        AUDIT_SOURCE_BINDING_PENDING_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
    }


def _agent(tmp_path: Path, manager: SubAgentManager) -> SimpleNamespace:
    return SimpleNamespace(
        config=AgentConfig(),
        tools=SimpleNamespace(workspace_root=tmp_path),
        root=tmp_path,
        subagents=manager,
    )


def _capture_auto_start(monkeypatch) -> list[list[str]]:
    from agent_py_agent.agent.agent_core.orchestration.background import dispatch as bg

    calls: list[list[str]] = []

    def _fake_auto_start(agent, tasks, request_params):
        run_ids = [str(task.id) for task in tasks]
        calls.append(run_ids)
        return {"status": "started", "run_ids": run_ids}

    monkeypatch.setattr(bg, "auto_start_tasks", _fake_auto_start)
    return calls


def test_runner_session_freshness() -> None:
    assert has_fresh_runner_session(_task_with_session(_session())) is True
    assert has_fresh_runner_session(_task_with_session(_session(age_seconds=60.0))) is False
    assert has_fresh_runner_session(_task_with_session(_session("completed"))) is False
    assert has_fresh_runner_session(_task_with_session(None)) is False


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux /proc required")
def test_subagent_liveness_treats_non_child_zombie_as_dead() -> None:
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import subprocess,sys,time;"
                "p=subprocess.Popen([sys.executable,'-c','pass']);"
                "print(p.pid,flush=True);"
                "time.sleep(30)"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert parent.stdout is not None
        child_pid = int(parent.stdout.readline().strip())
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                raw = Path(f"/proc/{child_pid}/stat").read_text(encoding="ascii")
            except OSError:
                raw = ""
            tail = raw[raw.rfind(")") + 1 :].strip() if ")" in raw else ""
            if tail.startswith("Z "):
                break
            time.sleep(0.05)
        assert tail.startswith("Z ")
        assert is_pid_alive(child_pid) is False
    finally:
        parent.terminate()
        parent.wait(timeout=5)


def test_large_interval_widens_fresh_window() -> None:
    assert has_fresh_runner_session(_task_with_session(_session(age_seconds=120.0, interval=30.0))) is True


def test_dispatch_candidate_rejects_fresh_ghost_but_accepts_stale_orphan() -> None:
    assert _is_dispatch_runner_candidate(_task_with_session(_session(), status="PENDING")) is False
    assert _is_dispatch_runner_candidate(_task_with_session(_session(age_seconds=60.0), status="PENDING")) is True


def test_supervision_revives_stalled_pending_and_planning(tmp_path: Path, monkeypatch) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    pending = _make_child(manager, status="PENDING", session=_session(age_seconds=120.0))
    planning = _make_child(manager, status="PLANNING")
    calls = _capture_auto_start(monkeypatch)

    summary = capability_auto_sweep.supervise_stalled_orphans(_agent(tmp_path, manager))

    assert summary["orphans_revived"] == 2
    assert sorted(calls[0]) == sorted([pending.id, planning.id])


def test_targeted_orphan_revive_starts_only_requested_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    requested = _make_child(
        manager,
        status="PENDING",
        session=_session(age_seconds=120.0),
    )
    _make_child(manager, status="PLANNING")
    calls = _capture_auto_start(monkeypatch)

    summary = capability_auto_sweep.auto_start_orphan_run(
        _agent(tmp_path, manager),
        requested.id,
    )

    assert summary["started"] == 1
    assert calls == [[requested.id]]


def test_targeted_orphan_revive_rejects_fresh_runner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    requested = _make_child(manager, status="PENDING", session=_session())
    calls = _capture_auto_start(monkeypatch)

    summary = capability_auto_sweep.auto_start_orphan_run(
        _agent(tmp_path, manager),
        requested.id,
    )

    assert summary["status"] == "not_needed"
    assert calls == []


def test_orphan_revive_stops_before_runtime_unknown_attempt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = SubAgentManager(
        tmp_path / "subagents",
        owner_id="local/main",
        owner_home_dir=str(tmp_path / "owner"),
    )
    requested = _make_child(manager, status="PENDING")
    prepared = manager.lifecycle.prepare_runner_attempt(requested.id)
    run = manager.runtime_db.agent_run_for_run_id(requested.id)
    assert run is not None
    assert manager.runtime_db._mark_attempt_unknown(
        prepared.runner_active_attempt_id,
        str(run["agent_run_id"]),
        reason="test worker disappeared",
        operator="test",
    )
    requested = manager.load(requested.id)
    requested.status = "PENDING"
    requested.runner_active_attempt_id = ""
    manager.save(requested)
    calls = _capture_auto_start(monkeypatch)

    targeted = capability_auto_sweep.auto_start_orphan_run(
        _agent(tmp_path, manager),
        requested.id,
    )
    swept = capability_auto_sweep.auto_start_stalled_orphans(
        _agent(tmp_path, manager)
    )

    assert targeted["status"] == "authority_recovery_blocked"
    assert targeted["recovery_block"]["reason"] == "attempt_unknown_terminal"
    assert swept["started"] == 0
    assert swept["authority_recovery_blocked"] == 1
    assert swept["recovery_blocks"][0]["run_id"] == requested.id
    assert calls == []


def test_orphan_revive_waits_for_abandoned_attempt_thread_to_exit(
    tmp_path: Path,
) -> None:
    from agent_py_agent.agent.concurrency.interrupt import register_interruptible

    manager = SubAgentManager(tmp_path / "subagents")
    requested = _make_child(manager, status="PENDING")
    requested.runner_active_attempt_id = ""
    requested.runner_abandoned_attempt_ids = ["attempt-still-unwinding"]
    manager.save(requested)
    decision = SimpleNamespace(allowed=True)
    name = (
        f"subagent-runner-attempt:{requested.id}:"
        "attempt-still-unwinding"
    )

    with register_interruptible(name):
        assert (
            capability_auto_sweep._is_stalled_dispatchable_orphan(
                requested,
                decision,
            )
            is False
        )

    assert capability_auto_sweep._is_stalled_dispatchable_orphan(
        requested,
        decision,
    ) is True


def test_supervision_skips_live_capped_and_running(tmp_path: Path, monkeypatch) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    _make_child(manager, status="PENDING", session=_session())
    _make_child(manager, status="PENDING", attempts=4)
    _make_child(manager, status="RUNNING", session=_session())
    calls = _capture_auto_start(monkeypatch)

    summary = capability_auto_sweep.supervise_stalled_orphans(_agent(tmp_path, manager))

    assert summary["orphans_revived"] == 0
    assert calls == []


def test_supervision_lock_serializes_competing_trigger_paths(tmp_path: Path, monkeypatch) -> None:
    from agent_py_agent.agent.agent_core.orchestration.dispatch.lock import _DispatchWatchLock

    manager = SubAgentManager(tmp_path / "subagents")
    _make_child(manager, status="PENDING", session=_session(age_seconds=120.0))
    calls = _capture_auto_start(monkeypatch)
    lock_path = manager.workspace / "subagent_orphan_supervision.lock"

    with _DispatchWatchLock(lock_path):
        summary = capability_auto_sweep.supervise_stalled_orphans(_agent(tmp_path, manager))

    assert summary["skipped_locked"] == 1
    assert summary["orphans_revived"] == 0
    assert calls == []


def test_supervision_does_not_misreport_body_runtime_error_as_lock_contention(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = SubAgentManager(tmp_path / "subagents")

    def _raise(_agent) -> dict[str, object]:
        raise RuntimeError("reconcile body failed")

    monkeypatch.setattr(capability_auto_sweep, "_supervise_stalled_orphans_unlocked", _raise)

    with pytest.raises(RuntimeError, match="reconcile body failed"):
        capability_auto_sweep.supervise_stalled_orphans(_agent(tmp_path, manager))


def test_scheduler_supervision_interval_gating(monkeypatch) -> None:
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentScheduler,
        _maybe_supervise_orphans,
    )

    agent = SimpleNamespace(config=AgentConfig(orphan_supervision_interval_seconds=60), collaboration_store=None)
    scheduler = BackgroundMainAgentScheduler({"runtime": SimpleNamespace(agent=agent), "store": SimpleNamespace()})
    swept: list[int] = []
    monkeypatch.setattr(capability_auto_sweep, "supervise_stalled_orphans", lambda _agent: swept.append(1) or {})
    base = time.time()

    _maybe_supervise_orphans(scheduler, base)
    _maybe_supervise_orphans(scheduler, base + 10)
    _maybe_supervise_orphans(scheduler, base + 61)

    assert len(swept) == 2


def test_supervision_restarts_requeued_orphan_before_source_reconciliation(
    monkeypatch,
) -> None:
    from agent_py_agent.agent.ingestion import source_worker

    order: list[str] = []
    monkeypatch.setattr(
        capability_auto_sweep,
        "_reconcile_conversation_parent_lifecycle",
        lambda _agent: order.append("parent") or {},
    )
    monkeypatch.setattr(
        capability_auto_sweep,
        "_reclaim_dead_running_runs",
        lambda _agent: order.append("reclaim") or [],
    )
    monkeypatch.setattr(
        capability_auto_sweep,
        "auto_start_stalled_orphans",
        lambda _agent: order.append("start") or {"started": 1},
    )
    monkeypatch.setattr(
        source_worker,
        "reconcile_audit_source_workers",
        lambda _agent: order.append("source")
        or {"workers": [{"created": False}]},
    )

    summary = capability_auto_sweep._supervise_stalled_orphans_unlocked(
        SimpleNamespace()
    )

    assert order == ["parent", "reclaim", "start", "source"]
    assert summary["orphans_revived"] == 1


def test_supervision_reclaims_running_with_dead_worker(tmp_path: Path, monkeypatch) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    dead = _make_child(
        manager,
        status="RUNNING",
        session={**_session(age_seconds=120.0), "worker_pid": 999999999},
    )
    fresh = _make_child(manager, status="RUNNING", session=_session())
    calls = _capture_auto_start(monkeypatch)

    summary = capability_auto_sweep.supervise_stalled_orphans(_agent(tmp_path, manager))

    assert summary["running_reclaimed"] == 1
    assert manager.load(dead.id).status == "PENDING"
    assert manager.load(fresh.id).status == "RUNNING"
    assert calls and dead.id in calls[0]


def test_managed_supervision_waits_for_live_runtime_attempt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    owner_home = tmp_path / "owner"
    manager = SubAgentManager(
        tmp_path / "subagents",
        owner_id="tui-test/recovery-wait",
        owner_home_dir=str(owner_home),
    )
    task = manager.create_run(
        goal="wait for runtime authority",
        thought="recovery",
        plan=["resume"],
        depth=1,
    )
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    prepared.attributes = {
        **dict(prepared.attributes or {}),
        "runner_session": {
            **_session(age_seconds=120.0),
            "run_id": task.id,
            "worker_pid": 999999999,
        },
    }
    manager.save(prepared)
    calls = _capture_auto_start(monkeypatch)

    summary = capability_auto_sweep.supervise_stalled_orphans(
        _agent(tmp_path, manager)
    )

    assert summary["running_reclaimed"] == 0
    assert manager.load(task.id).status == "RUNNING"
    assert manager.runtime_db.get_attempt(prepared.runner_active_attempt_id)["status"] == "running"
    assert calls == []


def test_managed_supervision_settles_dead_attempt_before_requeue(
    tmp_path: Path,
    monkeypatch,
) -> None:
    owner_home = tmp_path / "owner"
    manager = SubAgentManager(
        tmp_path / "subagents",
        owner_id="tui-test/recovery-requeue",
        owner_home_dir=str(owner_home),
    )
    task = manager.create_run(
        goal="requeue after runtime fence",
        thought="recovery",
        plan=["resume"],
        depth=1,
    )
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    old_attempt_id = prepared.runner_active_attempt_id
    prepared.attributes = {
        **dict(prepared.attributes or {}),
        "runner_session": {
            **_session(age_seconds=120.0),
            "run_id": task.id,
            "worker_pid": 999999999,
        },
    }
    manager.save(prepared)
    with manager.runtime_db.transaction() as conn:
        conn.execute(
            "UPDATE resource_locks SET pid = ?, start_token = '', lease_expires_at = ? "
            "WHERE attempt_id = ?",
            (999999999, time.time() - 1000, old_attempt_id),
        )
    calls = _capture_auto_start(monkeypatch)

    summary = capability_auto_sweep.supervise_stalled_orphans(
        _agent(tmp_path, manager)
    )

    assert summary["running_reclaimed"] == 1
    recovered = manager.load(task.id)
    assert recovered.status == "PENDING"
    assert old_attempt_id in recovered.runner_abandoned_attempt_ids
    assert manager.runtime_db.get_attempt(old_attempt_id)["status"] == "cancelled"
    assert calls and calls[0] == [task.id]
    resumed = manager.lifecycle.prepare_runner_attempt(task.id)
    assert resumed.runner_active_attempt_id != old_attempt_id
    authority = manager.runtime_db.agent_run_for_run_id(task.id)
    assert authority is not None
    assert int(authority["current_attempt_generation"]) == 2
    assert manager.runtime_db.get_attempt(resumed.runner_active_attempt_id)["status"] == "running"


def test_cross_generation_inprocess_session_ignores_reused_pid(tmp_path: Path, monkeypatch) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    frozen = _make_child(
        manager,
        status="RUNNING",
        session={
            **_session(age_seconds=120.0),
            "worker_pid": os.getpid(),
            "process_epoch": "old-generation",
            "in_process": True,
        },
    )
    _capture_auto_start(monkeypatch)

    summary = capability_auto_sweep.supervise_stalled_orphans(_agent(tmp_path, manager))

    assert summary["running_reclaimed"] == 1
    assert manager.load(frozen.id).status == "PENDING"


def test_cross_generation_subprocess_keeps_live_pid_guard(tmp_path: Path, monkeypatch) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    child = _make_child(
        manager,
        status="RUNNING",
        session={
            **_session(age_seconds=120.0),
            "worker_pid": os.getpid(),
            "process_epoch": "other-process",
            "in_process": False,
        },
    )
    _capture_auto_start(monkeypatch)

    summary = capability_auto_sweep.supervise_stalled_orphans(_agent(tmp_path, manager))

    assert summary["running_reclaimed"] == 0
    assert manager.load(child.id).status == "RUNNING"


@pytest.mark.parametrize(
    ("worker_pid", "expected_reason", "expected_stalled_count"),
    [
        (os.getpid(), "runner_session_stalled", 1),
        (999999999, "runner_process_died", 0),
    ],
)
def test_failed_audit_source_worker_is_fenced_and_requeued(
    tmp_path: Path,
    monkeypatch,
    worker_pid: int,
    expected_reason: str,
    expected_stalled_count: int,
) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    owner_home = tmp_path / "owner"
    audit_id = "audit-stalled-live"
    watch_id = "ws-stalled-live"
    task = _make_child(
        manager,
        status="RUNNING",
        attempts=99,
        session={
            **_session(age_seconds=120.0),
            "worker_pid": worker_pid,
            "process_epoch": "stalled-subprocess",
            "in_process": False,
        },
        attributes=_audit_source_attributes(
            owner_home,
            audit_id=audit_id,
            watch_id=watch_id,
        ),
    )
    state = new_state(
        owner_home,
        "http://audit-source.example/events",
        {"background_harvest": 0},
        watch_id=watch_id,
    )
    state.audit_guarantee = True
    state.audit_root_task_id = audit_id
    state.source_id = "source-stalled"
    state.totals["spool_candidates"] = 1
    persist_state(state)
    lease_path = state_dir(owner_home) / f"{watch_id}.worker-lease.json"
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    lease_path.write_text(
        '{"worker_key":"held","expires_at":9999999999}',
        encoding="utf-8",
    )
    calls = _capture_auto_start(monkeypatch)

    summary = capability_auto_sweep.supervise_stalled_orphans(
        _agent(tmp_path, manager)
    )

    persisted = manager.load(task.id)
    assert summary["running_reclaimed"] == 1
    assert summary["running_source_stalls_reclaimed"] == expected_stalled_count
    assert persisted.status == "PENDING"
    assert "attempt-live-1" in persisted.runner_abandoned_attempt_ids
    assert persisted.attributes["audit_source_recovery"][
        f"{expected_reason}_count"
    ] == 1
    assert persisted.attributes["audit_source_recovery"][
        "consecutive_stalls"
    ] == 1
    assert not lease_path.exists()
    assert calls and task.id in calls[0]


def test_stalled_prebinding_source_worker_is_fenced_and_requeued(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    audit_id = "audit-stalled-before-binding"
    task = _make_child(
        manager,
        status="RUNNING",
        attempts=99,
        session={
            **_session(age_seconds=120.0),
            "worker_pid": os.getpid(),
            "process_epoch": "stalled-binding",
            "in_process": False,
        },
        attributes=_audit_source_binding_attributes(audit_id=audit_id),
    )
    calls = _capture_auto_start(monkeypatch)

    summary = capability_auto_sweep.supervise_stalled_orphans(
        _agent(tmp_path, manager)
    )

    persisted = manager.load(task.id)
    assert summary["running_reclaimed"] == 1
    assert summary["running_source_stalls_reclaimed"] == 1
    assert persisted.status == "PENDING"
    assert persisted.attributes[AUDIT_SOURCE_BINDING_PENDING_ATTR] is True
    assert "attempt-live-1" in persisted.runner_abandoned_attempt_ids
    assert persisted.attributes["audit_source_recovery"][
        "runner_session_stalled_count"
    ] == 1
    assert calls and task.id in calls[0]


def test_supervision_kills_fully_stalled_source_worker_host(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    owner_home = tmp_path / "owner"
    audit_id = "audit-frozen-host"
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    tasks = []
    try:
        for index in range(2):
            watch_id = f"ws-frozen-host-{index}"
            task = _make_child(
                manager,
                status="RUNNING",
                session={
                    **_session(age_seconds=120.0),
                    "worker_pid": process.pid,
                    "process_epoch": "frozen-host",
                    "in_process": False,
                },
                attributes=_audit_source_attributes(
                    owner_home,
                    audit_id=audit_id,
                    watch_id=watch_id,
                ),
            )
            state = new_state(
                owner_home,
                f"http://audit-source-{index}.example/events",
                {"background_harvest": 0},
                watch_id=watch_id,
            )
            state.audit_guarantee = True
            state.audit_root_task_id = audit_id
            state.source_id = "source-stalled"
            state.totals["spool_candidates"] = 1
            persist_state(state)
            tasks.append(task)
        calls = _capture_auto_start(monkeypatch)
        events: list[str] = []
        from agent_py_agent.agent.subagents import process_control

        real_terminate = process_control.terminate_pid_with_escalation
        real_requeue = capability_auto_sweep._requeue_dead_running

        def _terminate_first(pid: int):
            events.append(f"terminate:{pid}")
            return real_terminate(pid)

        def _requeue_after_terminate(*args, **kwargs):
            assert events and events[0] == f"terminate:{process.pid}"
            events.append(f"requeue:{args[2]}")
            return real_requeue(*args, **kwargs)

        monkeypatch.setattr(
            process_control,
            "terminate_pid_with_escalation",
            _terminate_first,
        )
        monkeypatch.setattr(
            capability_auto_sweep,
            "_requeue_dead_running",
            _requeue_after_terminate,
        )

        summary = capability_auto_sweep.supervise_stalled_orphans(
            _agent(tmp_path, manager)
        )

        process.wait(timeout=5)
        assert summary["running_reclaimed"] == 2
        assert summary["stalled_source_hosts_cleanup_attempted"] == 1
        assert summary["stalled_source_hosts_terminated"] == 1
        assert all(manager.load(task.id).status == "PENDING" for task in tasks)
        assert calls and sorted(calls[0]) == sorted(task.id for task in tasks)
        assert events[0] == f"terminate:{process.pid}"
        assert sum(item.startswith("requeue:") for item in events) == 2
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_supervision_keeps_host_when_one_source_worker_is_healthy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    owner_home = tmp_path / "owner"
    audit_id = "audit-shared-healthy-host"
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        stale_watch_id = "ws-shared-stale"
        stale = _make_child(
            manager,
            status="RUNNING",
            session={
                **_session(age_seconds=120.0),
                "worker_pid": process.pid,
                "process_epoch": "shared-host",
                "in_process": False,
            },
            attributes=_audit_source_attributes(
                owner_home,
                audit_id=audit_id,
                watch_id=stale_watch_id,
            ),
        )
        healthy_watch_id = "ws-shared-healthy"
        healthy = _make_child(
            manager,
            status="RUNNING",
            session={
                **_session(),
                "worker_pid": process.pid,
                "process_epoch": "shared-host",
                "in_process": False,
            },
            attributes=_audit_source_attributes(
                owner_home,
                audit_id=audit_id,
                watch_id=healthy_watch_id,
            ),
        )
        for watch_id in (stale_watch_id, healthy_watch_id):
            state = new_state(
                owner_home,
                f"http://{watch_id}.example/events",
                {"background_harvest": 0},
                watch_id=watch_id,
            )
            state.audit_guarantee = True
            state.audit_root_task_id = audit_id
            state.source_id = "source-stalled"
            state.totals["spool_candidates"] = 1
            persist_state(state)
        calls = _capture_auto_start(monkeypatch)

        summary = capability_auto_sweep.supervise_stalled_orphans(
            _agent(tmp_path, manager)
        )

        assert summary["running_reclaimed"] == 1
        assert summary["stalled_source_hosts_cleanup_attempted"] == 0
        assert process.poll() is None
        assert manager.load(stale.id).status == "PENDING"
        assert manager.load(healthy.id).status == "RUNNING"
        assert calls and stale.id in calls[0]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_ended_audit_source_runner_with_unfinished_ledger_is_requeued(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    owner_home = tmp_path / "owner"
    audit_id = "audit-ended-with-work"
    watch_id = "ws-ended-with-work"
    task = _make_child(
        manager,
        status="RUNNING",
        attempts=99,
        session={
            **_session("completed", age_seconds=0.0),
            "worker_pid": os.getpid(),
            "process_epoch": "same-process",
            "in_process": True,
            "ended_at": time.time(),
        },
        attributes=_audit_source_attributes(
            owner_home,
            audit_id=audit_id,
            watch_id=watch_id,
        ),
    )
    state = new_state(
        owner_home,
        "http://audit-source.example/events",
        {"background_harvest": 0},
        watch_id=watch_id,
    )
    state.audit_guarantee = True
    state.audit_root_task_id = audit_id
    state.source_id = "source-stalled"
    state.totals["spool_candidates"] = 1
    persist_state(state)
    calls = _capture_auto_start(monkeypatch)

    summary = capability_auto_sweep.supervise_stalled_orphans(
        _agent(tmp_path, manager)
    )

    persisted = manager.load(task.id)
    assert summary["running_reclaimed"] == 1
    assert summary["running_source_ended_reclaimed"] == 1
    assert persisted.status == "PENDING"
    assert "attempt-live-1" in persisted.runner_abandoned_attempt_ids
    assert persisted.attributes["audit_source_recovery"][
        "runner_session_ended_count"
    ] == 1
    assert calls and task.id in calls[0]


def test_ended_ordinary_runner_keeps_existing_result_reconciliation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    task = _make_child(
        manager,
        status="RUNNING",
        session={
            **_session("completed", age_seconds=0.0),
            "worker_pid": os.getpid(),
            "process_epoch": "same-process",
            "in_process": True,
            "ended_at": time.time(),
        },
    )
    calls = _capture_auto_start(monkeypatch)

    summary = capability_auto_sweep.supervise_stalled_orphans(
        _agent(tmp_path, manager)
    )

    assert summary["running_reclaimed"] == 0
    assert manager.load(task.id).status == "RUNNING"
    assert calls == []


def test_closed_audit_source_worker_is_never_revived_as_pending_orphan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    owner_home = tmp_path / "owner"
    audit_id = "audit-already-closed"
    watch_id = "ws-already-closed"
    task = _make_child(
        manager,
        status="PENDING",
        attempts=99,
        attributes=_audit_source_attributes(
            owner_home,
            audit_id=audit_id,
            watch_id=watch_id,
        ),
    )
    state = new_state(
        owner_home,
        "http://audit-source.example/events",
        {"background_harvest": 0},
        watch_id=watch_id,
    )
    state.audit_guarantee = True
    state.audit_root_task_id = audit_id
    state.source_id = "source-stalled"
    state.closed = True
    persist_state(state)
    calls = _capture_auto_start(monkeypatch)

    assert _is_dispatch_runner_candidate(task) is False
    summary = capability_auto_sweep.supervise_stalled_orphans(
        _agent(tmp_path, manager)
    )

    assert summary["orphans_revived"] == 0
    assert calls == []
    assert manager.load(task.id).status == "CANCELLED"
