from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.dispatch import capability_auto_sweep
from agent_py_agent.agent.agent_core.runner.dispatch import _is_dispatch_runner_candidate
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
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


def _make_child(manager: SubAgentManager, *, status: str, session: dict | None = None, attempts: int = 0):
    task = manager.create_run(goal=f"liveness {status}", thought="liveness", plan=["run"], depth=1)
    task.status = status
    task.runner_attempts = attempts
    if status == "RUNNING":
        task.runner_active_attempt_id = "attempt-live-1"
    attrs = dict(task.attributes or {})
    if session is not None:
        attrs["runner_session"] = session
    task.attributes = attrs
    manager.save(task)
    return task


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


def test_stalled_redispatch_width_is_dynamic(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    for _ in range(4):
        _make_child(
            manager,
            status="PENDING",
            session={**_session(age_seconds=120.0), "worker_pid": 999999999},
        )

    assert capability_auto_sweep._stalled_redispatch_width(_agent(tmp_path, manager)) == 4
