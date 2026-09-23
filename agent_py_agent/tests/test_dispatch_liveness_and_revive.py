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
from agent_py_agent.agent.runtime_db.operations import AGENT_RUN_TERMINAL_STATUSES
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.process_control import is_pid_alive
from agent_py_agent.agent.subagents.runner_result_admission import make_rejected_runner_result
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


def test_supervision_reconciles_superseded_runtime_attempts(tmp_path, monkeypatch) -> None:
    reconciled: list[str] = []
    runtime_db = SimpleNamespace(
        reconcile_superseded_attempts=lambda: reconciled.extend(["attempt-old"]) or [
            "attempt-old"
        ]
    )
    manager = SimpleNamespace(
        workspace=tmp_path,
        runtime_db=runtime_db,
        list_runs=lambda: [],
    )
    monkeypatch.setattr(
        capability_auto_sweep,
        "_reconcile_conversation_parent_lifecycle",
        lambda _agent: {},
    )
    monkeypatch.setattr(
        capability_auto_sweep,
        "_reconcile_direct_parent_waits",
        lambda _agent: {},
    )
    monkeypatch.setattr(
        capability_auto_sweep,
        "auto_start_stalled_orphans",
        lambda _agent: {"started": 0},
    )

    summary = capability_auto_sweep._supervise_stalled_orphans_unlocked(
        _agent(tmp_path, manager)
    )

    assert reconciled == ["attempt-old"]
    assert summary["superseded_attempts_reconciled"] == 1


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


@pytest.mark.parametrize("attempts", [4, 64])
def test_supervision_keeps_liveness_and_retry_gates_without_lifetime_slice_cap(
    tmp_path: Path, monkeypatch, attempts: int,
) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    _make_child(manager, status="PENDING", session=_session())
    waiting = _make_child(manager, status="PENDING", attempts=attempts)
    waiting.turn_end_reason = "interrupted"
    manager.save(waiting)
    _make_child(manager, status="FAILED", attempts=attempts)
    _make_child(manager, status="RUNNING", session=_session())
    calls = _capture_auto_start(monkeypatch)

    summary = capability_auto_sweep.supervise_stalled_orphans(_agent(tmp_path, manager))

    assert summary["orphans_revived"] == 1
    assert calls == [[waiting.id]]


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
            **_session(age_seconds=0.0),
            "run_id": task.id,
            "worker_pid": 999999999,
            "process_epoch": "old-gateway-generation",
            "in_process": True,
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
    assert recovered.attributes["runner_session"]["status"] == "failed"
    assert recovered.attributes["runner_session"]["reclaim_reason"] == "runner_process_died"
    assert manager.runtime_db.get_attempt(old_attempt_id)["status"] == "cancelled"
    assert calls and calls[0] == [task.id]
    resumed = manager.lifecycle.prepare_runner_attempt(task.id)
    assert resumed.runner_active_attempt_id != old_attempt_id
    authority = manager.runtime_db.agent_run_for_run_id(task.id)
    assert authority is not None
    assert int(authority["current_attempt_generation"]) == 2
    assert manager.runtime_db.get_attempt(resumed.runner_active_attempt_id)["status"] == "running"


@pytest.mark.parametrize(
    ("run_status", "runtime_status", "runtime_reason", "task_status", "session_status"),
    [
        ("done", "ok", "", "DONE", "completed"),
        ("failed", "error", "provider_failed", "FAILED", "failed"),
        ("cancelled", "cancelled", "user_stop", "CANCELLED", "cancelled"),
    ],
)
def test_managed_supervision_projects_natural_runtime_terminal_without_rerun(
    tmp_path: Path,
    monkeypatch,
    run_status: str,
    runtime_status: str,
    runtime_reason: str,
    task_status: str,
    session_status: str,
) -> None:
    owner_home = tmp_path / "owner"
    manager = SubAgentManager(
        tmp_path / "subagents",
        owner_id="tui-test/terminal-projection",
        owner_home_dir=str(owner_home),
    )
    task = manager.create_run(
        goal="project a runtime terminal fact",
        thought="recovery",
        plan=["finish once"],
        depth=1,
    )
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    attempt_id = prepared.runner_active_attempt_id
    prepared.attributes = {
        **dict(prepared.attributes or {}),
        "runner_session": {
            **_session(age_seconds=0.0),
            "run_id": task.id,
            "worker_pid": 999999999,
            "process_epoch": "old-gateway-generation",
            "in_process": True,
        },
    }
    manager.save(prepared)
    authority = manager.runtime_db.agent_run_for_run_id(task.id)
    assert authority is not None
    settled = manager.runtime_db.settle_agent_run(
        agent_run_id=str(authority["agent_run_id"]),
        status=run_status,
        attempt_id=attempt_id,
        payload={
            "status": run_status,
            "runtime_status": runtime_status,
            "runtime_reason": runtime_reason,
            "runtime_source": "model_turn",
            "tool_rounds": 2,
        },
    )
    assert settled["settled"] is True
    calls = _capture_auto_start(monkeypatch)

    summary = capability_auto_sweep.supervise_stalled_orphans(
        _agent(tmp_path, manager)
    )

    repaired = manager.load(task.id)
    assert summary["running_reclaimed"] == 1
    assert summary["running_terminal_projected"] == 1
    assert repaired.status == task_status
    assert repaired.runner_active_attempt_id == ""
    assert repaired.runner_attempts == 1
    assert repaired.runner_abandoned_attempt_ids == []
    assert repaired.attributes["runner_session"]["status"] == session_status
    repair = repaired.attributes["runtime_terminal_projection_repair"]
    assert repair["attempt_id"] == attempt_id
    assert repair["run_status"] == run_status
    assert manager.runtime_db.agent_run_for_run_id(task.id)["status"] == run_status
    assert int(manager.runtime_db.agent_run_for_run_id(task.id)["current_attempt_generation"]) == 1
    assert calls == []


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


def test_cross_generation_dead_process_reclaims_even_with_fresh_heartbeat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    frozen = _make_child(
        manager,
        status="RUNNING",
        session={
            **_session(age_seconds=0.0),
            "worker_pid": 999999999,
            "process_epoch": "old-generation",
            "in_process": True,
        },
    )
    calls = _capture_auto_start(monkeypatch)

    summary = capability_auto_sweep.supervise_stalled_orphans(_agent(tmp_path, manager))

    assert summary["running_reclaimed"] == 1
    recovered = manager.load(frozen.id)
    assert recovered.status == "PENDING"
    assert recovered.attributes["runner_session"]["status"] == "failed"
    assert recovered.attributes["runner_session"]["reclaim_reason"] == "runner_process_died"
    assert calls == [[frozen.id]]


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


# LLM: 真实链路验收（f856ddd3）：走生产入口 settle_agent_attempt 造出事故形态
# （run=created、attempt=done、ended_at>0、is_current=True），再经真正的 RecordRunnerResult 入口
# 验证 task 终态 / runner_result / 直属父 wake 一致；过期保护用**真实下一代 attempt** 验证。
# 函数用途: 验证流不完整后的同一 attempt 能收口、旧代结果仍被拒、且父级真的收到唤醒。
def test_stream_incomplete_same_attempt_closes_authoritative_run(tmp_path: Path) -> None:
    import json as _json

    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.subagents.runner_result_admission import (
        _managed_runtime_result_conflict,
        _runner_result_matches_settled_attempt,
    )

    manager = SubAgentManager(
        tmp_path / "subagents",
        owner_id="tui-test/stream-incomplete",
        owner_home_dir=str(tmp_path / "owner"),
    )
    task = manager.create_run(goal="stream incomplete closure", thought="", plan=["finish"], depth=1)
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    attempt_id = prepared.runner_active_attempt_id
    authority = manager.runtime_db.agent_run_for_run_id(task.id)
    assert authority is not None
    agent_run_id = str(authority["agent_run_id"])

    # 生产入口：宿主按"可续跑族"只结清 attempt，AgentRun 仍保持 created（runtime_mixin.py:483 同路）。
    settled = manager.runtime_db.settle_agent_attempt(
        agent_run_id=agent_run_id,
        attempt_id=attempt_id,
        payload={
            "status": "attempt_done",
            "run_status": "created",
            "runtime_status": "error",
            "runtime_reason": "MODEL_STREAM_INCOMPLETE",
            "runtime_source": "model_provider",
            "tool_rounds": 3,
        },
    )
    assert settled.get("settled") is True, settled

    # guard 之前的硬事实。
    facts = manager.runtime_db.runner_result_commit_authority(run_id=task.id, attempt_id=attempt_id)
    assert facts is not None
    assert facts["run_status"] == "created"
    assert facts["attempt_status"] == "done"
    assert facts["is_current"] is True
    run_row = manager.runtime_db.agent_run_for_run_id(task.id)
    assert run_row is not None and str(run_row["status"] or "") == "created"
    attempt_rows = manager.runtime_db.attempts_for_run(agent_run_id)
    assert [str(row["status"]) for row in attempt_rows] == ["done"]
    assert float(attempt_rows[0]["ended_at"] or 0.0) > 0.0

    params = RecordRunnerResultParams(
        run_id=task.id,
        dry_run=False,
        ok=False,
        message="runner 执行失败: MODEL_STREAM_INCOMPLETE",
        attempt_id=attempt_id,
        status="FAILED",
        turn_end_reason="error",
        failure_type="runner_error",
    )

    # 旧规则（只放行 PENDING/BLOCKED）在同一事实上必然拒绝：这就是修复前的事故分支。
    old_rule_accepts = (
        str(facts["run_status"]) in {"", "created"}
        and str(facts["attempt_status"]) == "done"
        and str(params.status or "").strip().upper() in {"PENDING", "BLOCKED"}
    )
    assert old_rule_accepts is False, "旧规则必须拒绝 FAILED 终态（修复前事故形态）"
    assert _runner_result_matches_settled_attempt("created", "done", params) is True
    assert _managed_runtime_result_conflict(manager.runtime_db, manager.load(task.id), params) == ""

    # 真正的落账入口：写 runner_result + task 终态。
    result = manager.runner_result.record_runner_result(params)
    assert result.ok is False and result.status == "FAILED", result.status
    reloaded = manager.load(task.id)
    assert str(reloaded.status or "").upper() == "FAILED"
    assert Path(str(getattr(reloaded, "runner_result_file", "") or "")).exists()

    # 关键：runner 最终结论必须把 runtime 权威 run 一并收口（task 说 FAILED、run 不能还停在 created）。
    run_after = manager.runtime_db.agent_run_for_run_id(task.id)
    assert run_after is not None and str(run_after["status"] or "") == "failed", dict(run_after)
    events_after = manager.runtime_db.events_for_attempt(attempt_id, limit=100)
    assert any(str(item["event_type"] or "") == "agent_run.completed" for item in events_after), (
        "最终 FAILED 必须落 agent_run.completed，不能只有 agent_attempt.completed"
    )

    # 重放幂等：同一结果再写一次，不新增 run 终态事件、不重复业务。
    completed_before = sum(
        1 for item in events_after if str(item["event_type"] or "") == "agent_run.completed"
    )
    replay_same = manager.runner_result.record_runner_result(params)
    assert replay_same.status == "FAILED"
    events_replay = manager.runtime_db.events_for_attempt(attempt_id, limit=100)
    assert sum(
        1 for item in events_replay if str(item["event_type"] or "") == "agent_run.completed"
    ) == completed_before, "重放不得重复收口"
    assert str(manager.runtime_db.agent_run_for_run_id(task.id)["status"] or "") == "failed"

    # 过期保护：真实创建下一代 attempt 后，旧代结果必须仍被拒。
    next_prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    next_attempt_id = next_prepared.runner_active_attempt_id
    assert next_attempt_id != attempt_id
    stale_conflict = _managed_runtime_result_conflict(
        manager.runtime_db,
        manager.load(task.id),
        RecordRunnerResultParams(
            run_id=task.id,
            dry_run=False,
            ok=False,
            message="",
            attempt_id=attempt_id,
            status="FAILED",
            turn_end_reason="error",
        ),
    )
    assert stale_conflict != "", "旧代 attempt 的结果绝不能被接纳"

    # 直属父级唤醒：诊断事件不等于父级收到；接上父会话后用**当前代** attempt 重放失败结果。
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "owner-parent",
            "channel": "tui",
            "channel_conversation_id": "session-parent",
            "channel_user_id": "owner-parent",
        }
    )
    parent = manager.create_run(goal="parent waits for child", thought="", plan=["wait"], depth=1)
    manager.conversation_store = store
    # 直属父级唤醒按"孩子的 task → thread"绑定解析（store.thread_for_task），先建立该绑定。
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": task.id,
            "goal": "stream incomplete closure",
            "status": "active",
        }
    )
    child = manager.load(task.id)
    child.attributes = {
        **dict(child.attributes or {}),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent.id,
    }
    manager.save(child)
    replay = manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            dry_run=False,
            ok=False,
            message="runner 执行失败: MODEL_STREAM_INCOMPLETE",
            attempt_id=next_attempt_id,
            status="FAILED",
            turn_end_reason="error",
            failure_type="runner_error",
        )
    )
    assert replay.status == "FAILED"

    wake_payloads = []
    for candidate in sorted(tmp_path.rglob("*.json")):
        try:
            payload = _json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict) and "subagent-finished" in str(payload.get("dedupe_key") or ""):
            wake_payloads.append(payload)
    assert wake_payloads, "有父会话时 record_runner_result 必须给直属父级发结构化唤醒"
    assert str(wake_payloads[-1].get("reason") or "") == "subagent_runner_finished"
    assert str((wake_payloads[-1].get("metadata") or {}).get("status") or "").upper() == "FAILED"

# LLM: 非终态（PENDING/BLOCKED）是可恢复等待，绝不能被当成最终失败收口 run。
# 函数用途: 验证非终态 runner 结论不会给 runtime run 写终态。
def test_blocked_runner_result_does_not_settle_runtime_run(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.services.runner_result_service import (
        RecordRunnerResultParams,
    )

    manager = SubAgentManager(
        tmp_path / "subagents",
        owner_id="tui-test/blocked-no-settle",
        owner_home_dir=str(tmp_path / "owner"),
    )
    task = manager.create_run(goal="blocked stays resumable", thought="", plan=["wait"], depth=1)
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    attempt_id = prepared.runner_active_attempt_id

    result = manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            dry_run=False,
            ok=False,
            message="runner 本轮结束: blocked",
            attempt_id=attempt_id,
            status="BLOCKED",
            turn_end_reason="blocked",
        )
    )
    assert result.status == "BLOCKED"
    run_after = manager.runtime_db.agent_run_for_run_id(task.id)
    assert run_after is not None
    # 非终态结论必须排除**全部**终态，而不是只排除 done：failed/cancelled 同样是把
    # 可恢复等待误判成结束。
    observed = str(run_after["status"] or "")
    assert observed not in AGENT_RUN_TERMINAL_STATUSES, (
        f"可恢复等待不得被收口成任何终态（观测到 {observed!r}）"
    )
    attempt_rows = manager.runtime_db.attempts_for_run(str(run_after["agent_run_id"]))
    # 本 fixture 没有宿主 settle_agent_attempt，收口语义必须"什么都不做"：
    # attempt 保持 running、没被 closeout 顺带结束。
    assert [str(row["status"]) for row in attempt_rows] == ["running"], (
        "非终态结论不得顺带结清 attempt"
    )
    assert all(float(row["ended_at"] or 0.0) == 0.0 for row in attempt_rows)
    events = manager.runtime_db.events_for_attempt(attempt_id, limit=100)
    assert not any(str(item["event_type"] or "") == "agent_run.completed" for item in events), (
        "非终态结论不得写 agent_run.completed"
    )


# LLM: 可恢复收口的三个真实缺口共用同一组生产助手（settle / WAL / 恢复扫描），因此在这里
# 用"真实 runtime.db + 真实会话存储 + 真实落盘 runner_result"造出事故形态，而不是打桩断言：
#   1) 收口已提交、父级通知前中断 → 恢复链必须补通知，且只补一次；
#   2) 收口写库一次失败 → 必须留下可诊断的待重试事实，恢复链补收口后再补通知；
#   3) 重复恢复 → 不产生重复终态事件、不产生重复父级唤醒。
# 函数用途: 构造一个带父会话绑定的真实子代理 run，并返回收口测试所需的事实。
def _closeout_fixture(tmp_path: Path, *, owner: str):
    import json as _json

    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.subagents.services.runner_result_service import (
        RecordRunnerResultParams,
    )

    manager = SubAgentManager(
        tmp_path / "subagents",
        owner_id=owner,
        owner_home_dir=str(tmp_path / "owner"),
    )
    store = ConversationStore(tmp_path / "conversations")
    manager.conversation_store = store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": f"{owner}-parent",
            "channel": "tui",
            "channel_conversation_id": f"{owner}-session",
            "channel_user_id": f"{owner}-parent",
        }
    )
    task = manager.create_run(goal="recoverable closeout", thought="", plan=["finish"], depth=1)
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    attempt_id = prepared.runner_active_attempt_id
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": task.id,
            "goal": "recoverable closeout",
            "status": "active",
        }
    )
    child = manager.load(task.id)
    child.attributes = {
        **dict(child.attributes or {}),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": task.id,
    }
    manager.save(child)
    authority = manager.runtime_db.agent_run_for_run_id(task.id)
    assert authority is not None
    agent_run_id = str(authority["agent_run_id"])
    # 生产入口：宿主按可续跑族只结清 attempt，run 保持 created。
    settled = manager.runtime_db.settle_agent_attempt(
        agent_run_id=agent_run_id,
        attempt_id=attempt_id,
        payload={"status": "attempt_done", "run_status": "created", "runtime_status": "error"},
    )
    assert settled.get("settled") is True, settled
    params = RecordRunnerResultParams(
        run_id=task.id,
        dry_run=False,
        ok=False,
        message="runner 执行失败: MODEL_STREAM_INCOMPLETE",
        attempt_id=attempt_id,
        status="FAILED",
        turn_end_reason="error",
        failure_type="runner_error",
    )
    return manager, store, task, attempt_id, agent_run_id, params, _json


# LLM: 只统计"这个 exact attempt 的真实唤醒信号"：按 wake_signal_id 去重 pending/handled 两份，
# 去重回执文件（wake_dedupe.v1，无 reason）不是唤醒，换代后的新 attempt 也不能被算成重复。
# 函数用途: 统计唤醒队列里属于某 attempt 的结构化唤醒数量。
def _wakes_for_attempt(tmp_path: Path, task_id: str, attempt_id: str):
    import json as _json

    seen: dict[str, tuple[Path, dict]] = {}
    for candidate in sorted(tmp_path.rglob("*.json")):
        try:
            payload = _json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("reason") or "") != "subagent_runner_finished":
            continue
        key = str(payload.get("dedupe_key") or "")
        if not key.startswith(f"subagent-finished:{task_id}:"):
            continue
        if not key.endswith(f":{attempt_id}"):
            continue
        wake_signal_id = str(payload.get("wake_signal_id") or candidate.stem)
        seen.setdefault(wake_signal_id, (candidate, payload))
    return list(seen.values())


# LLM: 缺口 1——run 已收口但父级通知前中断：事实必须在盘、恢复链必须补通知且只补一次。
# 函数用途: 验证"收口后通知前中断"可恢复，且通知不重。
def test_closeout_interrupted_before_notify_recovers_once(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents import runner_completion_wake
    from agent_py_agent.agent.subagents.services import runtime_closeout

    manager, store, task, attempt_id, agent_run_id, params, _json = _closeout_fixture(
        tmp_path, owner="tui-test/closeout-interrupted"
    )
    # 注入"通知前中断"：在真正的发布点之前抛错（等同进程在收口后、通知前被杀）。
    def _explode(*_args, **_kwargs):
        raise RuntimeError("interrupted before notify")

    original = runner_completion_wake.notify_parent_on_runner_result
    runner_completion_wake.notify_parent_on_runner_result = _explode
    try:
        result = manager.runner_result.record_runner_result(params)
    finally:
        runner_completion_wake.notify_parent_on_runner_result = original
    assert result.status == "FAILED"

    # 收口已提交（run 终态 + 事件），但父级还没收到通知。
    run_row = manager.runtime_db.agent_run_for_run_id(task.id)
    assert str(run_row["status"] or "") == "failed"
    assert _wakes_for_attempt(tmp_path, task.id, attempt_id) == [], "中断时不能假装已通知"
    fact = runtime_closeout.pending_closeout(manager.load(task.id))
    assert fact is not None, "中断必须留下持久化的待重试事实"
    assert str(fact["attempt_id"]) == attempt_id
    assert str(fact["target_run_status"]) == "failed"

    # 恢复：补通知且只补一次。
    summary = runtime_closeout.recover_pending_closeouts(manager)
    assert summary["runtime_closeouts_recovered"] == 1, summary
    wakes = _wakes_for_attempt(tmp_path, task.id, attempt_id)
    assert len(wakes) == 1, f"恢复必须恰好补一次通知，实际 {len(wakes)}"
    assert str(wakes[0][1].get("reason") or "") == "subagent_runner_finished"
    assert runtime_closeout.pending_closeout(manager.load(task.id)) is None, "交付完成后必须清账"

    # 重复恢复：不得再发一次。
    again = runtime_closeout.recover_pending_closeouts(manager)
    assert again["runtime_closeouts_recovered"] == 0, again
    assert len(_wakes_for_attempt(tmp_path, task.id, attempt_id)) == 1, "重复恢复不得重复通知"


# LLM: 缺口 2——收口写库一次失败：不得静默返回 None，必须留下可诊断的待重试事实，
# 恢复链补收口后再补通知；期间父级绝不能被当成"已完成"唤醒。
# 函数用途: 验证收口写库失败一次后可恢复，且恢复前不通知、恢复后恰通知一次。
def test_closeout_write_failure_leaves_retryable_fact_then_recovers(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.services import runtime_closeout

    manager, store, task, attempt_id, agent_run_id, params, _json = _closeout_fixture(
        tmp_path, owner="tui-test/closeout-write-fail"
    )
    repo = manager.runtime_db
    original_settle = repo.settle_agent_run
    calls = {"count": 0, "failures": 0}

    def _fail_once(*args, **kwargs):
        calls["count"] += 1
        if calls["failures"] == 0:
            calls["failures"] += 1
            raise RuntimeError("runtime.db temporarily unavailable")
        return original_settle(*args, **kwargs)

    repo.settle_agent_run = _fail_once
    try:
        result = manager.runner_result.record_runner_result(params)
    finally:
        repo.settle_agent_run = original_settle
    assert result.status == "FAILED"
    assert calls["failures"] == 1, "本用例要求恰好注入一次写库失败"

    # 权威 run 仍是 created：收口没落成，且不能被冒充成已完成。
    run_row = manager.runtime_db.agent_run_for_run_id(task.id)
    assert str(run_row["status"] or "") == "created", dict(run_row)
    assert _wakes_for_attempt(tmp_path, task.id, attempt_id) == [], (
        "收口未提交时父级绝不能被唤醒成已完成"
    )
    fact = runtime_closeout.pending_closeout(manager.load(task.id))
    assert fact is not None, "写库失败必须留下持久化待重试事实，而不是静默 fail-soft"
    assert str(fact["closeout_state"]) == "write_error"
    assert "unavailable" in str(fact["closeout_detail"]), fact["closeout_detail"]

    # 恢复：先补收口，再补通知。
    summary = runtime_closeout.recover_pending_closeouts(manager)
    assert summary["runtime_closeouts_recovered"] == 1, summary
    run_after = manager.runtime_db.agent_run_for_run_id(task.id)
    assert str(run_after["status"] or "") == "failed", dict(run_after)
    events = manager.runtime_db.events_for_attempt(attempt_id, limit=100)
    assert sum(
        1 for item in events if str(item["event_type"] or "") == "agent_run.completed"
    ) == 1, "恢复收口必须恰好写一次终态事件"
    assert len(_wakes_for_attempt(tmp_path, task.id, attempt_id)) == 1, "恢复后必须恰好通知一次"
    assert runtime_closeout.pending_closeout(manager.load(task.id)) is None


# LLM: 缺口 2 续——重复恢复必须幂等：不重复终态事件、不重复父级唤醒。
# 函数用途: 连续多次恢复后断言事实不重复。
def test_repeated_closeout_recovery_is_idempotent(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents import runner_completion_wake
    from agent_py_agent.agent.subagents.services import runtime_closeout

    manager, store, task, attempt_id, agent_run_id, params, _json = _closeout_fixture(
        tmp_path, owner="tui-test/closeout-idempotent"
    )
    # 真实形态：先让收口提交、通知在"中断"中丢失，留下真实落盘的 runner_result 与 WAL。
    def _explode(*_args, **_kwargs):
        raise RuntimeError("interrupted before notify")

    original = runner_completion_wake.notify_parent_on_runner_result
    runner_completion_wake.notify_parent_on_runner_result = _explode
    try:
        manager.runner_result.record_runner_result(params)
    finally:
        runner_completion_wake.notify_parent_on_runner_result = original
    assert runtime_closeout.pending_closeout(manager.load(task.id)) is not None
    summaries = [runtime_closeout.recover_pending_closeouts(manager) for _ in range(3)]
    assert summaries[0]["runtime_closeouts_recovered"] == 1, summaries
    assert all(item["runtime_closeouts_recovered"] == 0 for item in summaries[1:]), summaries
    events = manager.runtime_db.events_for_attempt(attempt_id, limit=100)
    assert sum(
        1 for item in events if str(item["event_type"] or "") == "agent_run.completed"
    ) == 1, "重复恢复不得重复收口"
    assert len(_wakes_for_attempt(tmp_path, task.id, attempt_id)) == 1, "重复恢复不得重复通知"
    assert runtime_closeout.pending_closeout(manager.load(task.id)) is None


# LLM: 缺口 2 的终态一致性：同一 exact current attempt 的一致终态可重入完成交付，
# 冲突终态仍拒——修复前 run=failed + attempt=done + incoming=failed 会被误判为冲突。
# 函数用途: 验证终态一致性规则不再拒绝可重入的一致终态。
def test_consistent_terminal_result_is_reentrant_but_conflict_is_rejected(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.runner_result_admission import (
        _managed_runtime_result_conflict,
    )

    manager, store, task, attempt_id, agent_run_id, params, _json = _closeout_fixture(
        tmp_path, owner="tui-test/closeout-consistent"
    )
    # 先正常收口：run=failed，而 attempt 因 settle_agent_run 只改未 ended 的 attempt 仍为 done。
    manager.runner_result.record_runner_result(params)
    run_row = manager.runtime_db.agent_run_for_run_id(task.id)
    assert str(run_row["status"] or "") == "failed"
    facts = manager.runtime_db.runner_result_commit_authority(
        run_id=task.id, attempt_id=attempt_id
    )
    assert facts is not None
    assert str(facts["run_status"]) == "failed"
    assert str(facts["attempt_status"]) == "done", (
        "真实事故形态：settle_agent_run 不改已 ended 的 attempt，run 已 failed 而 attempt 仍 done"
    )
    assert bool(facts["is_current"]) is True

    # 一致终态必须可重入（修复前这里会被判成 conflicting terminal fact）。
    assert _managed_runtime_result_conflict(
        manager.runtime_db, manager.load(task.id), params
    ) == "", "同一 exact current attempt 的一致终态必须可重入完成交付"

    # 冲突终态仍拒：run=failed 收到 DONE。
    conflicting = type(params)(**{**vars(params), "status": "DONE", "turn_end_reason": "completed"})
    assert _managed_runtime_result_conflict(
        manager.runtime_db, manager.load(task.id), conflicting
    ) != "", "与权威终态冲突的结论必须仍被拒绝"


# LLM: 被拒事实（换代/冲突）重试永远不会成功：诊断只写一次后必须清账，否则每个 reconcile
# 周期都会重复写 closeout_blocked，且该 owner 会因这条永久事实被反复当成硬事实扫描。
# 函数用途: 验证被拒事实只诊断一次、不无界重试、不产生事件洪泛。
def test_rejected_closeout_fact_is_cleared_once_not_retried(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.services import runtime_closeout

    manager, store, task, attempt_id, agent_run_id, params, _json = _closeout_fixture(
        tmp_path, owner="tui-test/closeout-rejected"
    )
    # 先让收口提交并完成交付，再把 WAL 事实人为恢复出来但换成"已换代"的 attempt：
    # 这样恢复链一定会拿到 stale_attempt 这个被拒结论。
    manager.runner_result.record_runner_result(params)
    task_loaded = manager.load(task.id)
    superseded = type(params)(**{**vars(params), "attempt_id": "attempt-superseded-not-current"})
    assert runtime_closeout.record_pending_closeout(
        manager, task_loaded, superseded, make_rejected_runner_result(
            task_loaded, False, False, params.message
        ),
        {"state": "pending", "target_run_status": "failed", "agent_run_id": agent_run_id,
         "attempt_id": "attempt-superseded-not-current"},
    )
    fact = runtime_closeout.pending_closeout(manager.load(task.id))
    assert fact is not None
    assert str(fact["attempt_id"]) == "attempt-superseded-not-current"

    first = runtime_closeout.recover_pending_closeouts(manager)
    assert first["runtime_closeouts_rejected"] == 1, first
    assert runtime_closeout.pending_closeout(manager.load(task.id)) is None, (
        "被拒事实必须清账，不能每个周期重复重试"
    )

    # 后续周期：不再重试、不再写诊断事件。
    events_before = manager.runtime_db.events_for_attempt(attempt_id, limit=200)
    blocked_before = sum(
        1 for item in events_before if str(item["event_type"] or "") == "closeout_blocked"
    )
    for _ in range(3):
        again = runtime_closeout.recover_pending_closeouts(manager)
        assert again["runtime_closeouts_rejected"] == 0, again
        assert again["runtime_closeouts_pending"] == 0, again
    events_after = manager.runtime_db.events_for_attempt(attempt_id, limit=200)
    blocked_after = sum(
        1 for item in events_after if str(item["event_type"] or "") == "closeout_blocked"
    )
    assert blocked_after == blocked_before, "被拒事实不得每周期重复写诊断事件"
    # 权威终态与既有唤醒不受影响。
    assert str(manager.runtime_db.agent_run_for_run_id(task.id)["status"] or "") == "failed"
    assert len(_wakes_for_attempt(tmp_path, task.id, attempt_id)) == 1


# LLM: 监督者复现的缺口：WAL 保存失败时 record_pending_closeout 返回 False，旧实现只把
# wal_active 置 False，随后仍 settle + deliver —— 一旦 deliver 失败，父级永远收不到通知，
# 且没有任何持久化重试或诊断。本用例用"零文件/零 LLM 控制流重放"固定正确形态：
# 事实没能持久化时不得继续不可恢复的后续动作，必须在另一个存储域留下可达恢复事实。
# 注入点精确：只在 canonical task 已经带上 WAL 属性的那次 manager.save 上失败
# （前一步的结果保存必须真的成功，不能用整个 task 文件不可写代替）。
# 函数用途: 验证 WAL 保存失败后停止收口/通知、留下可达事实，且恢复后收口与通知不丢不重。
def test_closeout_fact_save_failure_stops_and_stays_recoverable(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.services import runtime_closeout

    manager, store, task, attempt_id, agent_run_id, params, _json = _closeout_fixture(
        tmp_path, owner="tui-test/closeout-wal-save-fail"
    )
    original_save = manager.save
    seen = {"wal_failures": 0, "normal_saves": 0}

    def flaky_save(saved_task, **kwargs):
        attrs = getattr(saved_task, "attributes", {}) or {}
        if isinstance(attrs, dict) and "runtime_closeout_pending" in attrs:
            seen["wal_failures"] += 1
            raise OSError("injected WAL save failure")
        seen["normal_saves"] += 1
        return original_save(saved_task, **kwargs)

    manager.save = flaky_save
    try:
        result = manager.runner_result.record_runner_result(params)
    finally:
        manager.save = original_save
    assert result.status == "FAILED"
    # 结果本身已经保存成功（注入只打在 WAL 那一次保存上）。
    assert seen["normal_saves"] >= 1, "结果保存必须发生在注入之前且成功"
    assert seen["wal_failures"] >= 1, "必须真的注入到 WAL 那次保存"
    assert Path(str(getattr(manager.load(task.id), "runner_result_file", "") or "")).exists()

    # 事实没落成 → 不得继续收口与通知（否则会留下无法恢复的窗口）。
    run_row = manager.runtime_db.agent_run_for_run_id(task.id)
    assert str(run_row["status"] or "") == "created", (
        "WAL 未持久化时不得继续收口（避免不可恢复的后续动作）"
    )
    assert _wakes_for_attempt(tmp_path, task.id, attempt_id) == [], "不得在无恢复依据时通知父级"
    assert runtime_closeout.pending_closeout(manager.load(task.id)) is None
    # 但恢复依据必须在另一个存储域里可达。
    events = manager.runtime_db.events_for_attempt(attempt_id, limit=100)
    unpersisted = [e for e in events if str(e["event_type"] or "") == "closeout_unpersisted"]
    assert unpersisted, "WAL 写不进去时必须在 runtime 事件账本留下可达恢复事实"
    payload = unpersisted[0]["payload"]
    assert payload["run_id"] == task.id and payload["attempt_id"] == attempt_id
    assert str(payload.get("agent_run_id") or "") == agent_run_id

    # 存储恢复可写后：恢复链把事件还原成 WAL，再补收口与补通知，恰好一次。
    summary = runtime_closeout.recover_pending_closeouts(manager)
    assert summary["runtime_closeouts_restored"] == 1, summary
    assert summary["runtime_closeouts_recovered"] == 1, summary
    run_after = manager.runtime_db.agent_run_for_run_id(task.id)
    assert str(run_after["status"] or "") == "failed", dict(run_after)
    events_after = manager.runtime_db.events_for_attempt(attempt_id, limit=200)
    assert sum(
        1 for e in events_after if str(e["event_type"] or "") == "agent_run.completed"
    ) == 1, "恢复收口必须恰好写一次终态事件"
    assert len(_wakes_for_attempt(tmp_path, task.id, attempt_id)) == 1, "恢复后必须恰好通知一次"
    assert runtime_closeout.pending_closeout(manager.load(task.id)) is None

    # 重复恢复仍然幂等。
    again = runtime_closeout.recover_pending_closeouts(manager)
    assert again["runtime_closeouts_recovered"] == 0, again
    assert len(_wakes_for_attempt(tmp_path, task.id, attempt_id)) == 1


# LLM: clear_closeout / mark_closeout_delivered 失败时绝不能报 advanced 成功，也不能让一次成功
# 的父级通知因为清账失败被重发（"不重"）。函数用途: 固定清账失败时的诚实回报与去重边界。
def test_closeout_cleanup_failure_is_not_reported_as_success(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.services import runtime_closeout

    manager, store, task, attempt_id, agent_run_id, params, _json = _closeout_fixture(
        tmp_path, owner="tui-test/closeout-cleanup-fail"
    )
    # 先正常收口一次（产生一条真实唤醒），再人为恢复出"已提交、待交付"的事实。
    manager.runner_result.record_runner_result(params)
    assert len(_wakes_for_attempt(tmp_path, task.id, attempt_id)) == 1
    task_loaded = manager.load(task.id)
    assert runtime_closeout.record_pending_closeout(
        manager, task_loaded, params,
        make_rejected_runner_result(task_loaded, False, False, params.message),
        {"state": "already_consistent", "target_run_status": "failed",
         "agent_run_id": agent_run_id, "attempt_id": attempt_id},
    )
    original_save = manager.save

    def failing_save(saved_task, **kwargs):
        raise OSError("injected save failure during cleanup")

    manager.save = failing_save
    try:
        outcome = runtime_closeout.advance_pending_closeout(
            manager, manager.load(task.id), runtime_closeout.pending_closeout(manager.load(task.id))
        )
    finally:
        manager.save = original_save
    assert outcome.get("advanced") is not True, (
        "清账/交付标记写不进去时报 advanced 成功会让恢复链误判已完成"
    )
    # 通知不重：唤醒仍然是 1 条（回执判定 already_delivered），且事实仍在等待下一轮。
    assert len(_wakes_for_attempt(tmp_path, task.id, attempt_id)) == 1

    # 存储恢复后，恢复链清账且不再重发。
    summary = runtime_closeout.recover_pending_closeouts(manager)
    assert summary["runtime_closeouts_recovered"] == 1, summary
    assert runtime_closeout.pending_closeout(manager.load(task.id)) is None
    assert len(_wakes_for_attempt(tmp_path, task.id, attempt_id)) == 1, "已交付的通知不得重发"


# LLM: 初次 runner 交付与恢复交付都必须先持久标记 delivered 再清 WAL；单次标记失败时，
#   原 pending WAL 必须留给恢复扫描，已发出的 exact attempt wake 不能重发。
# 函数用途: 验证首次交付的已通知标记落盘失败不会把唯一恢复事实清掉。
def test_initial_delivery_marker_failure_keeps_pending_closeout(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.services import runtime_closeout

    manager, _store, task, attempt_id, _agent_run_id, params, _json = _closeout_fixture(
        tmp_path, owner="tui-test/initial-delivery-marker-fail"
    )
    original_save = manager.save
    failures = 0

    def fail_first_delivered_marker(saved_task, **kwargs):
        nonlocal failures
        fact = (getattr(saved_task, "attributes", {}) or {}).get("runtime_closeout_pending")
        if isinstance(fact, dict) and fact.get("delivery") == "delivered" and failures == 0:
            failures += 1
            raise OSError("injected first delivered marker failure")
        return original_save(saved_task, **kwargs)

    manager.save = fail_first_delivered_marker
    try:
        result = manager.runner_result.record_runner_result(params)
    finally:
        manager.save = original_save
    assert result.status == "FAILED" and failures == 1
    assert len(_wakes_for_attempt(tmp_path, task.id, attempt_id)) == 1
    fact = runtime_closeout.pending_closeout(manager.load(task.id))
    assert fact is not None and fact["delivery"] == "pending"

    summary = runtime_closeout.recover_pending_closeouts(manager)
    assert summary["runtime_closeouts_recovered"] == 1
    assert runtime_closeout.pending_closeout(manager.load(task.id)) is None
    assert len(_wakes_for_attempt(tmp_path, task.id, attempt_id)) == 1


# LLM: 使用真实队列及原子替换故障，验证已发布信号而回执未保存时恢复仍认同一 attempt。
# 函数用途: 覆盖 pending 和已被父级消费两种半写窗口，不能把恢复重发当成新通知。
@pytest.mark.parametrize("handled_before_receipt", [False, True])
def test_closeout_wake_receipt_half_write_does_not_duplicate(
    tmp_path: Path, monkeypatch, handled_before_receipt: bool,
) -> None:
    from agent_py_agent.agent.common import json_io
    from agent_py_agent.agent.subagents.services import runtime_closeout

    manager, store, task, attempt_id, _, params, _json = _closeout_fixture(
        tmp_path, owner="tui-test/wake-receipt-half-write"
    )
    thread = store.tasks.thread_for(task.id)
    key = f"subagent-finished:{task.id}:FAILED:{attempt_id}"
    receipt_path = store.storage.wake_dedupe_path(thread.thread_id, key)
    original_replace = json_io._replace_with_retry
    injected = False

    def fail_receipt_once(source, destination):
        nonlocal injected
        # v2 先冻结 prepared，再发布配对；只在发布完成标记替换时注入同一半写事实。
        payload = _json.loads(source.read_text(encoding="utf-8")) if destination == receipt_path else {}
        if destination == receipt_path and payload.get("phase") == "published" and not injected:
            injected = True
            published = _wakes_for_attempt(tmp_path, task.id, attempt_id)
            assert len(published) == 1
            if handled_before_receipt:
                signal = store.wakes.pending()[0]
                assert store.wakes.mark_handled(signal.wake_signal_id) is not None
            raise OSError("injected wake receipt replacement failure")
        return original_replace(source, destination)

    monkeypatch.setattr(json_io, "_replace_with_retry", fail_receipt_once)
    manager.runner_result.record_runner_result(params)
    assert injected
    assert len(_wakes_for_attempt(tmp_path, task.id, attempt_id)) == 1
    assert runtime_closeout.pending_closeout(manager.load(task.id)) is not None

    summary = runtime_closeout.recover_pending_closeouts(manager)
    assert summary["runtime_closeouts_recovered"] == 1
    assert runtime_closeout.pending_closeout(manager.load(task.id)) is None
    assert len(_wakes_for_attempt(tmp_path, task.id, attempt_id)) == 1


# LLM: 真机缺口（104 轮注入复现）：run 状态未知/脏时，旧守卫把它当"权威冲突"直接丢弃 runner
# 的最终结论 —— 没有 runner_result、没有待重试事实、没有父级通知，子代理永久 RUNNING。
# 未知状态不是权威终态事实：结论必须照常落账并留下可诊断的待重试事实（不猜成功、不覆盖未知行），
# 记录被修复后再补收口与通知。函数用途: 固定未知状态下的可诊断、可恢复语义。
def test_unknown_run_status_keeps_conclusion_diagnosable_not_silently_dropped(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.services import runtime_closeout

    manager, store, task, attempt_id, agent_run_id, params, _json = _closeout_fixture(
        tmp_path, owner="tui-test/unknown-run-status"
    )
    # 复现现场：执行中把权威 run 状态改成未知值（仅这条新 run）。
    with manager.runtime_db.transaction() as conn:
        conn.execute("UPDATE agent_runs SET status='quarantined-test' WHERE agent_run_id=?",
                     (agent_run_id,))

    result = manager.runner_result.record_runner_result(params)
    assert result.status == "FAILED"
    # 结论不再被静默丢弃：结果文件、任务终态、可诊断事实都在。
    reloaded = manager.load(task.id)
    assert Path(str(getattr(reloaded, "runner_result_file", "") or "")).exists(), (
        "未知 run 状态不得让 runner 结论被静默丢弃"
    )
    assert str(reloaded.status or "").upper() == "FAILED", "子代理不得停留在永久 RUNNING"
    fact = runtime_closeout.pending_closeout(reloaded)
    assert fact is not None, "必须留下可诊断的待重试收口事实"
    assert str(fact["closeout_state"]) == runtime_closeout.CLOSEOUT_UNKNOWN_STATUS, fact["closeout_state"]
    # 不猜成功、不冒充完成：未知 run 状态不得被改写，也不得通知父级。
    assert str(manager.runtime_db.agent_run_for_run_id(task.id)["status"] or "") == "quarantined-test"
    assert _wakes_for_attempt(tmp_path, task.id, attempt_id) == [], (
        "状态未知时不得把结论当成功通知父级"
    )
    events = manager.runtime_db.events_for_attempt(attempt_id, limit=100)
    assert not any(str(e["event_type"] or "") == "agent_run.completed" for e in events)

    # 记录被修复 → 恢复链补收口与补通知，恰好一次。
    with manager.runtime_db.transaction() as conn:
        conn.execute("UPDATE agent_runs SET status='created' WHERE agent_run_id=?", (agent_run_id,))
    summary = runtime_closeout.recover_pending_closeouts(manager)
    assert summary["runtime_closeouts_recovered"] == 1, summary
    assert str(manager.runtime_db.agent_run_for_run_id(task.id)["status"] or "") == "failed"
    events_after = manager.runtime_db.events_for_attempt(attempt_id, limit=200)
    assert sum(
        1 for e in events_after if str(e["event_type"] or "") == "agent_run.completed"
    ) == 1
    assert len(_wakes_for_attempt(tmp_path, task.id, attempt_id)) == 1
    assert runtime_closeout.pending_closeout(manager.load(task.id)) is None
