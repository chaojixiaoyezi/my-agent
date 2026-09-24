from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.background import dispatch as background_dispatch
from agent_py_agent.agent.agent_core.orchestration.dispatch.capability_auto_sweep import (
    _reconcile_conversation_parent_lifecycle,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.conversation_lifecycle_gate import (
    conversation_lifecycle_decisions,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchParams
from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.common.audit_activation import (
    AUDIT_ATTR,
    AUDIT_SOURCE_ID_ATTR,
    AUDIT_SOURCE_OWNER_HOME_ATTR,
    AUDIT_SOURCE_WATCH_ID_ATTR,
    AUDIT_SOURCE_WORKER_ATTR,
    AUDIT_SOURCE_WORKER_KEY_ATTR,
    audit_source_worker_key,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_worker import _owner_pool
from agent_py_agent.agent.ingestion.source_worker import settle_audit_source_worker
from agent_py_agent.agent.ingestion.watch_state import (
    load_state,
    new_state,
    persist_state,
    state_dir,
)
from agent_py_agent.agent.owner_scoped_pool import shared_active_owner_registry
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli import gateway_loops


def _runner_session(*, heartbeat_at: float) -> dict[str, object]:
    return {
        "schema_version": "runner_session_pool.v1",
        "session_id": "runsess-before-restart",
        "run_id": "run-before-restart",
        "worker_pid": os.getpid(),
        "worker_id": "subagent-worker:run-before-restart",
        "status": "running",
        "heartbeat_at": heartbeat_at,
        "interval_seconds": 5.0,
        "started_at": heartbeat_at - 10.0,
        "ended_at": 0.0,
        "process_epoch": "old-gateway-generation",
        "in_process": True,
    }


def _context(base_agent: SimpleAgent, tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(agent=base_agent, config_path=tmp_path / "agent_config.yaml")


def _scoped_restart_fixture(tmp_path: Path):
    config = AgentConfig(
        enable_tools=False,
        memory_path="memory.jsonl",
        gateway_per_user_owner_scoping=True,
        my_agent_home=str(tmp_path / "home"),
        orphan_supervision_interval_seconds=60,
    )
    base_agent = SimpleAgent(config, tmp_path)
    owner = OwnerIdentity.provider_user("feishu", "u-restart")
    scoped = _owner_pool(base_agent).get(owner)
    return base_agent, owner, scoped


def _running_restart_task(scoped, *, heartbeat_at: float):
    task = scoped.subagents.create_run(
        goal="continue after gateway restart",
        thought="restart recovery",
        plan=["continue"],
        depth=1,
    )
    task.status = "RUNNING"
    task.attributes = {
        **dict(task.attributes or {}),
        "runner_session": _runner_session(heartbeat_at=heartbeat_at),
        "background_start": {
            "launch_id": "launch-before-restart",
            "status": "running",
            "updated_at": time.time(),
        },
    }
    scoped.subagents.save(task)
    return task


def _bind_conversation_task(
    scoped,
    task,
    *,
    parent_status: str = "active",
    run_status: str = "active",
):
    store = scoped.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "u-restart",
            "channel": "feishu",
            "channel_conversation_id": f"chat-{task.id}",
            "channel_user_id": "u-restart",
        }
    )
    parent_task_id = f"parent-{task.id}"
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": parent_task_id,
            "goal": "parent task",
            "status": parent_status,
        }
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": task.id,
            "goal": task.goal,
            "status": run_status,
        }
    )
    task = scoped.subagents.load(task.id)
    task.attributes = {
        **dict(task.attributes or {}),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
    }
    scoped.subagents.save(task)
    return thread, parent_task_id


def _capture_background_starts(monkeypatch) -> list[list[str]]:
    starts: list[list[str]] = []

    def _capture_start(_agent, tasks, _params):
        starts.append([str(item.id) for item in tasks])
        return {"status": "started", "run_ids": starts[-1]}

    monkeypatch.setattr(background_dispatch, "auto_start_tasks", _capture_start)
    return starts


def test_reconciler_reclaims_after_heartbeat_stales_without_another_model_tick(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    shared_active_owner_registry(base_agent).record(owner)
    starts = _capture_background_starts(monkeypatch)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)
    reconciler = gateway_loops._GatewayOrphanReconciler(_context(base_agent, tmp_path))

    first = reconciler.tick()

    assert scoped.subagents.load(task.id).status == "RUNNING"
    assert not any(int(report.get("running_reclaimed") or 0) for report in first)

    stale = scoped.subagents.load(task.id)
    stale.attributes["runner_session"] = _runner_session(heartbeat_at=time.time() - 120.0)
    scoped.subagents.save(stale)

    second = reconciler.tick()

    recovered = scoped.subagents.load(task.id)
    assert recovered.status == "PENDING"
    assert recovered.attributes["background_start"]["status"] == "reclaimed"
    assert starts == [[task.id]]
    assert any(int(report.get("running_reclaimed") or 0) == 1 for report in second)


# 替身 owner 池：按 owner 身份原样返回实例；巡检以 touch=False 租用并 pin，不刷新空闲时间。
class _PassThroughOwnerPool:
    def get(self, owner, *, touch: bool = True):
        return owner

    @contextmanager
    def pin(self, agent, *, touch: bool = True):
        yield agent


def test_reconciler_stuck_owner_does_not_delay_base_or_other_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, _owner, _scoped = _scoped_restart_fixture(tmp_path)
    slow_owner = OwnerIdentity.provider_user("feishu", "u-slow")
    fast_owner = OwnerIdentity.provider_user("feishu", "u-fast")
    registry = shared_active_owner_registry(base_agent)
    registry.record(slow_owner)
    registry.record(fast_owner)
    monkeypatch.setattr(
        gateway_loops,
        "_gateway_agent_from_context",
        lambda _context: base_agent,
    )
    reconciler = gateway_loops._GatewayOrphanReconciler(
        _context(base_agent, tmp_path)
    )
    reconciler.interval = 0.05
    monkeypatch.setattr(reconciler, "_seed_owner_registry", lambda: None)
    monkeypatch.setattr(
        reconciler,
        "_ensure_owner_pool",
        lambda: _PassThroughOwnerPool(),
    )
    slow_entered = threading.Event()
    release_slow = threading.Event()
    labels: list[str] = []

    def _sweep(_agent, label: str) -> dict[str, object]:
        labels.append(label)
        if label.endswith("/u-slow"):
            slow_entered.set()
            release_slow.wait(timeout=2.0)
        return {"owner": label}

    monkeypatch.setattr(reconciler, "_sweep", _sweep)
    stop_event = threading.Event()
    thread = threading.Thread(target=reconciler.run, args=(stop_event,))
    thread.start()
    try:
        assert slow_entered.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while (
            labels.count("base") < 2
            or not any(label.endswith("/u-fast") for label in labels)
        ) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert labels.count("base") >= 2
        assert any(label.endswith("/u-fast") for label in labels)
        assert sum(label.endswith("/u-slow") for label in labels) == 1
    finally:
        stop_event.set()
        release_slow.set()
        thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_reconciler_bounds_queue_and_prioritizes_recent_owners(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, _owner, _scoped = _scoped_restart_fixture(tmp_path)
    registry = shared_active_owner_registry(base_agent)
    for index in range(6):
        registry.record(OwnerIdentity.provider_user("feishu", f"u-{index}"))
    monkeypatch.setattr(
        gateway_loops,
        "_gateway_agent_from_context",
        lambda _context: base_agent,
    )
    reconciler = gateway_loops._GatewayOrphanReconciler(
        _context(base_agent, tmp_path)
    )
    reconciler._owner_worker_limit = 2
    reconciler._next_discovery_at = float("inf")
    submitted: list[str] = []

    class _PendingFuture:
        def done(self) -> bool:
            return False

    class _Executor:
        def submit(self, _callable, _owner, label):
            submitted.append(label)
            return _PendingFuture()

    reconciler._owner_executor = _Executor()

    reconciler._submit_owner_sweeps(10.0)

    assert submitted == ["feishu/user/u-5", "feishu/user/u-4"]
    assert len(reconciler._owner_inflight) == 2


def test_reconciler_does_not_sweep_memory_curator_only_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, _owner, _scoped = _scoped_restart_fixture(tmp_path)
    hard_owner = OwnerIdentity.provider_user("feishu", "u-running")
    soft_owner = OwnerIdentity.provider_user("feishu", "u-memory-only")
    registry = shared_active_owner_registry(base_agent)
    registry.record(hard_owner, hard=True)
    registry.record(soft_owner, hard=False)
    monkeypatch.setattr(
        gateway_loops,
        "_gateway_agent_from_context",
        lambda _context: base_agent,
    )
    reconciler = gateway_loops._GatewayOrphanReconciler(
        _context(base_agent, tmp_path)
    )
    monkeypatch.setattr(reconciler, "_seed_owner_registry", lambda: None)
    monkeypatch.setattr(
        reconciler,
        "_ensure_owner_pool",
        lambda: _PassThroughOwnerPool(),
    )
    labels: list[str] = []

    def _sweep(_agent, label: str) -> dict[str, object]:
        labels.append(label)
        return {"owner": label}

    monkeypatch.setattr(reconciler, "_sweep", _sweep)

    reports = reconciler.tick()

    assert labels == ["base", "feishu/user/u-running"]
    assert [report["owner"] for report in reports] == labels


def test_reconciler_does_not_replay_completed_runner_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time() - 120.0)
    completed = scoped.subagents.load(task.id)
    completed.attributes["runner_session"] = {
        **dict(completed.attributes["runner_session"]),
        "status": "completed",
        "ended_at": time.time() - 110.0,
    }
    scoped.subagents.save(completed)
    shared_active_owner_registry(base_agent).record(owner)
    starts = _capture_background_starts(monkeypatch)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)

    reports = gateway_loops._GatewayOrphanReconciler(_context(base_agent, tmp_path)).tick()

    recovered = scoped.subagents.load(task.id)
    assert recovered.status == "RUNNING"
    assert starts == []
    assert not any(int(report.get("running_reclaimed") or 0) for report in reports)
    assert not any(int(report.get("orphans_revived") or 0) for report in reports)


def test_reconciler_cold_start_discovers_owner_projection_and_reclaims(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = AgentConfig(
        enable_tools=False,
        memory_path="memory.jsonl",
        gateway_per_user_owner_scoping=True,
        my_agent_home=str(tmp_path / "home"),
        orphan_supervision_interval_seconds=60,
    )
    owner = OwnerIdentity.provider_user("feishu", "u-cold-restart")
    before_restart = SimpleAgent(config, tmp_path)
    old_scoped = _owner_pool(before_restart).get(owner)
    task = _running_restart_task(old_scoped, heartbeat_at=time.time() - 120.0)
    owner_projection = old_scoped.home_paths.owner_agents_dir / task.id / "state.json"
    assert owner_projection.is_file()

    after_restart = SimpleAgent(config, tmp_path)
    starts = _capture_background_starts(monkeypatch)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: after_restart)
    reconciler = gateway_loops._GatewayOrphanReconciler(_context(after_restart, tmp_path))

    reports = reconciler.tick()

    recovered_scoped = _owner_pool(after_restart).get(owner)
    recovered = recovered_scoped.subagents.load(task.id)
    assert recovered.status == "PENDING"
    assert recovered.attributes["background_start"]["status"] == "reclaimed"
    assert starts == [[task.id]]
    assert any(report.get("owner") == "feishu/user/u-cold-restart" for report in reports)
    assert any(int(report.get("running_reclaimed") or 0) == 1 for report in reports)


def test_reconciler_recovers_scoped_run_only_while_parent_and_run_are_active(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time() - 120.0)
    _bind_conversation_task(scoped, task)
    shared_active_owner_registry(base_agent).record(owner)
    starts = _capture_background_starts(monkeypatch)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)

    reports = gateway_loops._GatewayOrphanReconciler(_context(base_agent, tmp_path)).tick()

    assert scoped.subagents.load(task.id).status == "PENDING"
    assert starts == [[task.id]]
    assert any(int(report.get("running_reclaimed") or 0) == 1 for report in reports)


def test_reconciler_cancels_old_run_when_parent_conversation_completed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time() - 120.0)
    thread, parent_task_id = _bind_conversation_task(scoped, task)
    scoped.conversation_store.tasks.update_status(
        {"task_id": parent_task_id, "status": "completed", "expected_status": "active"}
    )
    shared_active_owner_registry(base_agent).record(owner)
    starts = _capture_background_starts(monkeypatch)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)

    reports = gateway_loops._GatewayOrphanReconciler(_context(base_agent, tmp_path)).tick()

    assert scoped.subagents.load(task.id).status == "CANCELLED"
    assert starts == []
    links = {link.task_id: link for link in scoped.conversation_store.tasks.list(thread.thread_id)}
    assert links[parent_task_id].status == "completed"
    assert links[task.id].status == "cancelled"
    assert any(int(report.get("parent_closed_cancelled") or 0) == 1 for report in reports)


@pytest.mark.parametrize(
    "terminal_status", ["FAILED", "BLOCKED", "TIMEOUT", "CHANNEL_ERROR", "DONE"]
)
def test_parent_cleanup_preserves_existing_child_terminal_result(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    _thread, _parent_task_id = _bind_conversation_task(scoped, task, run_status="failed")
    ended = scoped.subagents.load(task.id)
    ended.status = terminal_status
    ended.failure_type = "runner_error" if terminal_status == "FAILED" else ""
    ended.runner_last_error = "typed Compact failure" if terminal_status == "FAILED" else ""
    scoped.subagents.save(ended)

    decision = conversation_lifecycle_decisions(scoped, [ended])[task.id]
    assert decision.should_cancel
    assert decision.reason == "run_link_closed"

    summary = _reconcile_conversation_parent_lifecycle(scoped)

    preserved = scoped.subagents.load(task.id)
    assert preserved.status == terminal_status
    assert preserved.failure_type == ended.failure_type
    assert preserved.runner_last_error == ended.runner_last_error
    assert "cancel_subagents" not in preserved.attributes
    assert summary["parent_closed_cancelled"] == 0


def test_reconciler_cancels_old_run_when_parent_conversation_interrupted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time() - 120.0)
    _thread, parent_task_id = _bind_conversation_task(scoped, task)
    scoped.conversation_store.tasks.update_status(
        {"task_id": parent_task_id, "status": "interrupted", "expected_status": "active"}
    )
    shared_active_owner_registry(base_agent).record(owner)
    starts = _capture_background_starts(monkeypatch)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)

    reports = gateway_loops._GatewayOrphanReconciler(_context(base_agent, tmp_path)).tick()

    assert scoped.subagents.load(task.id).status == "CANCELLED"
    assert starts == []
    assert any(int(report.get("parent_closed_cancelled") or 0) == 1 for report in reports)


def test_reconciler_holds_scoped_run_when_parent_link_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time() - 120.0)
    thread = scoped.conversation_store.threads.get_or_create(
        {
            "canonical_user_id": "u-restart",
            "channel": "feishu",
            "channel_conversation_id": f"chat-{task.id}",
            "channel_user_id": "u-restart",
        }
    )
    stale = scoped.subagents.load(task.id)
    stale.attributes = {
        **dict(stale.attributes or {}),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": "missing-parent",
    }
    scoped.subagents.save(stale)
    shared_active_owner_registry(base_agent).record(owner)
    starts = _capture_background_starts(monkeypatch)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)

    reports = gateway_loops._GatewayOrphanReconciler(_context(base_agent, tmp_path)).tick()

    assert scoped.subagents.load(task.id).status == "RUNNING"
    assert starts == []
    assert any(int(report.get("parent_recovery_held") or 0) == 1 for report in reports)


def test_reconciler_holds_scoped_run_when_parent_link_is_corrupt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time() - 120.0)
    _thread, parent_task_id = _bind_conversation_task(scoped, task)
    (scoped.conversation_store.storage.tasks_dir / f"{parent_task_id}.json").write_text(
        "{broken",
        encoding="utf-8",
    )
    shared_active_owner_registry(base_agent).record(owner)
    starts = _capture_background_starts(monkeypatch)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)

    reports = gateway_loops._GatewayOrphanReconciler(_context(base_agent, tmp_path)).tick()

    assert scoped.subagents.load(task.id).status == "RUNNING"
    assert starts == []
    assert any(int(report.get("parent_recovery_held") or 0) == 1 for report in reports)


def test_auto_start_and_dispatch_both_reject_run_under_completed_parent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time() - 120.0)
    _thread, parent_task_id = _bind_conversation_task(scoped, task)
    scoped.conversation_store.tasks.update_status(
        {"task_id": parent_task_id, "status": "completed", "expected_status": "active"}
    )
    pending = scoped.subagents.load(task.id)
    pending.status = "PENDING"
    pending.attributes.pop("background_start", None)
    pending.attributes.pop("runner_session", None)
    scoped.subagents.save(pending)
    monkeypatch.setattr(
        background_dispatch,
        "_start_background_dispatch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not start")),
    )

    auto_start = background_dispatch.auto_start_tasks(scoped, [pending], {})
    report = scoped.dispatch_subagents(
        CapabilityRouter(config=CapabilityConfig(), tool_specs=scoped.tools.specs()),
        CapabilityConfig(),
        params=DispatchParams(
            apply=True,
            start_runners=True,
            include_run_ids=[task.id],
        ),
    )

    assert auto_start["status"] == "blocked"
    assert auto_start["conversation_gate"][0]["reason"] == "parent_link_closed"
    assert any(record.action == "conversation_lifecycle_blocked" for record in report.records)
    assert scoped.subagents.load(task.id).status == "PENDING"


def test_live_claim_keeps_child_allowed_after_parent_projection_completed(tmp_path: Path) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, parent_task_id = _bind_conversation_task(scoped, task)
    scoped.conversation_store.tasks.update_status(
        {"task_id": parent_task_id, "status": "completed", "expected_status": "active"}
    )
    claim = scoped.conversation_store.claims.acquire(
        {
            "thread_id": thread.thread_id,
            "task_id": parent_task_id,
            "reason": "user_guidance",
            "lease_seconds": 300,
        }
    )
    assert claim is not None

    current = scoped.subagents.load(task.id)
    decision = conversation_lifecycle_decisions(scoped, [current])[current.id]

    assert decision.allowed is True
    assert decision.reason == "parent_execution_active"
    assert decision.parent_status == "completed"
    assert decision.parent_allows_children is True


def test_explicit_user_stop_resume_reopens_only_the_same_conversation_run(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, _parent_task_id = _bind_conversation_task(scoped, task)
    stopped = scoped.subagents.load(task.id)
    stopped.status = "CANCELLED"
    stopped.failure_type = "cancelled"
    stopped.verification_status = "UNVERIFIED"
    stopped.attributes = {
        **dict(stopped.attributes or {}),
        "cancel_subagents": {
            "reason": "conversation_user_stop",
            "previous_status": "RUNNING",
            "previous_failure_type": "",
        },
    }
    scoped.subagents.save(stopped)
    cancelled_link = scoped.conversation_store.tasks.update_status(
        {
            "task_id": task.id,
            "status": "cancelled",
            "expected_status": "active",
        }
    )
    assert cancelled_link is not None

    closed = conversation_lifecycle_decisions(scoped, [stopped])[task.id]
    explicit_resume = conversation_lifecycle_decisions(
        scoped,
        [stopped],
        resume_run_ids={task.id},
    )[task.id]

    assert closed.allowed is False
    assert closed.reason == "run_link_closed"
    assert explicit_resume.allowed is True
    assert explicit_resume.reason == "user_stopped_same_run_resume"
    assert explicit_resume.thread_id == thread.thread_id

    prepared = scoped.subagents.lifecycle.prepare_runner_attempt(
        task.id,
        retry_reason="reason_code=conversation_user_stop; mode=same_run_resume",
    )
    links, errors = scoped.conversation_store.tasks.list_report(thread.thread_id)
    child_link = next(item for item in links if item.task_id == task.id)

    assert errors == []
    assert prepared.id == task.id
    assert prepared.status == "RUNNING"
    assert child_link.status == "active"
    assert [item.id for item in scoped.subagents.list_runs()].count(task.id) == 1


def test_active_audit_source_worker_survives_closed_foreground_run_link(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, parent_task_id = _bind_conversation_task(
        scoped,
        task,
        run_status="cancelled",
    )
    owner_home = Path(scoped.home_paths.owner_home_dir)
    state = new_state(
        owner_home,
        "http://audit-source.example/events",
        {"background_harvest": 0},
    )
    state.audit_guarantee = True
    state.audit_root_task_id = parent_task_id
    state.source_id = "audit-source"
    state.totals["spool_candidates"] = 1
    persist_state(state)
    current = scoped.subagents.load(task.id)
    current.status = "PENDING"
    current.attributes = {
        **dict(current.attributes or {}),
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: parent_task_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
            parent_task_id,
            state.watch_id,
        ),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
    }
    scoped.subagents.save(current)

    decision = conversation_lifecycle_decisions(scoped, [current])[current.id]

    assert decision.allowed is True
    assert decision.reason == "audit_source_watch_active"
    assert decision.parent_allows_children is True


def test_idle_audit_source_worker_waits_without_model_call_until_disk_backlog_advances(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, parent_task_id = _bind_conversation_task(
        scoped,
        task,
        run_status="cancelled",
    )
    owner_home = Path(scoped.home_paths.owner_home_dir)
    state = new_state(
        owner_home,
        "http://idle-audit-source.example/events",
        {"background_harvest": 0},
    )
    state.audit_guarantee = True
    state.audit_root_task_id = parent_task_id
    state.source_id = "idle-audit-source"
    persist_state(state)
    current = scoped.subagents.load(task.id)
    current.status = "PENDING"
    current.attributes = {
        **dict(current.attributes or {}),
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: parent_task_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
            parent_task_id,
            state.watch_id,
        ),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
    }
    scoped.subagents.save(current)

    idle = conversation_lifecycle_decisions(scoped, [current])[current.id]

    assert idle.allowed is False
    assert idle.action == "hold"
    assert idle.reason == "audit_source_waiting_for_records"

    # Simulate a harvester in another process committing one complete record.
    # The already-cached lifecycle state must refresh this monotonic counter
    # from disk before deciding whether to dispatch the model worker.
    state_path = state_dir(owner_home) / f"{state.watch_id}.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["totals"]["spool_candidates"] = 1
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    ready = conversation_lifecycle_decisions(scoped, [current])[current.id]

    assert ready.allowed is True
    assert ready.reason == "audit_source_watch_active"


def test_active_watch_cannot_revive_after_named_audit_parent_is_cancelled(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time() - 120.0)
    thread, parent_task_id = _bind_conversation_task(
        scoped,
        task,
        parent_status="cancelled",
    )
    owner_home = Path(scoped.home_paths.owner_home_dir)
    state = new_state(
        owner_home,
        "http://cancelled-audit-source.example/events",
        {"background_harvest": 0},
    )
    state.audit_guarantee = True
    state.audit_root_task_id = parent_task_id
    state.source_id = "cancelled-audit-source"
    persist_state(state)
    current = scoped.subagents.load(task.id)
    current.status = "PENDING"
    current.runner_active_attempt_id = ""
    current.attributes = {
        **dict(current.attributes or {}),
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: parent_task_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
            parent_task_id,
            state.watch_id,
        ),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
    }
    scoped.subagents.save(current)

    decision = conversation_lifecycle_decisions(scoped, [current])[current.id]
    summary = _reconcile_conversation_parent_lifecycle(scoped)

    assert decision.allowed is False
    assert decision.should_cancel is True
    assert decision.reason == "parent_link_closed"
    assert summary["parent_closed_cancelled"] == 1
    assert scoped.subagents.load(task.id).status == "CANCELLED"
    closed_state = load_state(owner_home, state.watch_id)
    assert closed_state is not None
    assert closed_state.closed is True
    assert closed_state.close_reason == "audit_parent_inactive"


def test_closed_audit_source_worker_is_cancelled_not_completed(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, parent_task_id = _bind_conversation_task(scoped, task)
    owner_home = Path(scoped.home_paths.owner_home_dir)
    state = new_state(
        owner_home,
        "http://audit-source.example/events",
        {"background_harvest": 0},
    )
    state.audit_guarantee = True
    state.audit_root_task_id = parent_task_id
    state.source_id = "audit-source"
    state.closed = True
    persist_state(state)
    current = scoped.subagents.load(task.id)
    current.status = "PENDING"
    current.attributes = {
        **dict(current.attributes or {}),
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: parent_task_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
            parent_task_id,
            state.watch_id,
        ),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
    }
    scoped.subagents.save(current)

    decision = conversation_lifecycle_decisions(scoped, [current])[current.id]

    assert decision.allowed is False
    assert decision.should_cancel is True
    assert decision.should_complete is False
    assert decision.reason == "audit_source_watch_closed"


def test_naturally_settled_audit_source_worker_projects_done_verified(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, parent_task_id = _bind_conversation_task(scoped, task)
    owner_home = Path(scoped.home_paths.owner_home_dir)
    state = new_state(
        owner_home,
        "http://audit-source.example/events",
        {"background_harvest": 0},
    )
    state.audit_guarantee = True
    state.audit_root_task_id = parent_task_id
    state.source_id = "audit-source"
    state.watch_window_seconds = 1
    state.opened_at = time.time() - 10
    state.window_finalized_at = time.time() - 1
    state.totals["spool_candidates"] = 0
    persist_state(state)
    current = scoped.subagents.load(task.id)
    current.status = "PENDING"
    current.runner_active_attempt_id = ""
    current.attributes = {
        **dict(current.attributes or {}),
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: parent_task_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
            parent_task_id,
            state.watch_id,
        ),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
    }
    scoped.subagents.save(current)

    decision = conversation_lifecycle_decisions(
        scoped,
        [scoped.subagents.load(task.id)],
    )[task.id]
    summary = _reconcile_conversation_parent_lifecycle(scoped)

    completed = scoped.subagents.load(task.id)
    links = {
        link.task_id: link
        for link in scoped.conversation_store.tasks.list(thread.thread_id)
    }
    assert decision.allowed is False
    assert decision.should_cancel is False
    assert decision.should_complete is True
    assert decision.reason == "audit_source_watch_complete"
    assert completed.status == "DONE"
    assert completed.verification_status == "VERIFIED"
    assert completed.attributes["audit_source_terminal"]["reason"] == (
        "watch_window_settled"
    )
    assert links[task.id].status == "completed"
    assert summary["source_workers_completed"] == 1
    assert summary["parent_closed_cancelled"] == 0


def test_final_boundary_event_projects_settled_source_worker_without_status(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, parent_task_id = _bind_conversation_task(scoped, task)
    owner_home = Path(scoped.home_paths.owner_home_dir)
    state = new_state(
        owner_home,
        "http://audit-source.example/events",
        {"background_harvest": 0},
    )
    state.audit_guarantee = True
    state.audit_root_task_id = parent_task_id
    state.source_id = "audit-source"
    state.watch_window_seconds = 1
    state.opened_at = time.time() - 10
    state.window_finalized_at = time.time() - 1
    state.totals["spool_candidates"] = 0
    persist_state(state)
    current = scoped.subagents.load(task.id)
    current.status = "PENDING"
    current.runner_active_attempt_id = ""
    current.attributes = {
        **dict(current.attributes or {}),
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: parent_task_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
            parent_task_id,
            state.watch_id,
        ),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
    }
    scoped.subagents.save(current)

    assert settle_audit_source_worker(scoped, state) is True

    completed = scoped.subagents.load(task.id)
    assert completed.status == "DONE"
    assert completed.verification_status == "VERIFIED"
    assert completed.attributes["audit_source_terminal"]["reason"] == (
        "watch_window_settled"
    )


def test_settled_source_worker_repairs_legacy_false_cancellation(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, parent_task_id = _bind_conversation_task(scoped, task)
    owner_home = Path(scoped.home_paths.owner_home_dir)
    state = new_state(
        owner_home,
        "http://audit-source.example/events",
        {"background_harvest": 0},
    )
    state.audit_guarantee = True
    state.audit_root_task_id = parent_task_id
    state.source_id = "audit-source"
    state.watch_window_seconds = 1
    state.opened_at = time.time() - 10
    state.window_finalized_at = time.time() - 1
    state.totals["spool_candidates"] = 0
    persist_state(state)
    current = scoped.subagents.load(task.id)
    current.status = "CANCELLED"
    current.failure_type = "cancelled"
    current.runner_active_attempt_id = ""
    current.attributes = {
        **dict(current.attributes or {}),
        AUDIT_ATTR: True,
        CONVERSATION_REQUEST_ID_ATTR: parent_task_id,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: state.source_id,
        AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
            parent_task_id,
            state.watch_id,
        ),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
        "cancel_subagents": {
            "reason": "conversation_lifecycle:audit_source_watch_complete",
            "previous_status": "RUNNING",
        },
    }
    scoped.subagents.save(current)
    assert scoped.conversation_store.tasks.update_status(
        {
            "task_id": task.id,
            "status": "cancelled",
            "expected_status": "active",
        }
    ) is not None

    summary = _reconcile_conversation_parent_lifecycle(scoped)

    repaired = scoped.subagents.load(task.id)
    links = {
        link.task_id: link
        for link in scoped.conversation_store.tasks.list(thread.thread_id)
    }
    assert repaired.status == "DONE"
    assert repaired.verification_status == "VERIFIED"
    assert "cancel_subagents" not in repaired.attributes
    assert repaired.attributes["audit_source_terminal"]["previous_status"] == (
        "CANCELLED"
    )
    assert links[task.id].status == "completed"
    assert summary["source_workers_completed"] == 1


def test_manual_cancel_stays_closed_even_when_named_as_a_resume_run(
    tmp_path: Path,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    _thread, _parent_task_id = _bind_conversation_task(scoped, task)
    cancelled = scoped.subagents.load(task.id)
    cancelled.status = "CANCELLED"
    cancelled.failure_type = "cancelled"
    cancelled.attributes = {
        **dict(cancelled.attributes or {}),
        "cancel_subagents": {
            "reason": "administrator_cancel",
            "previous_status": "RUNNING",
        },
    }
    scoped.subagents.save(cancelled)
    assert scoped.conversation_store.tasks.update_status(
        {
            "task_id": task.id,
            "status": "cancelled",
            "expected_status": "active",
        }
    ) is not None

    decision = conversation_lifecycle_decisions(
        scoped,
        [cancelled],
        resume_run_ids={task.id},
    )[task.id]

    assert decision.allowed is False
    assert decision.reason == "run_link_closed"


def test_completed_parent_children_share_one_execution_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    first = _running_restart_task(scoped, heartbeat_at=time.time())
    thread, parent_task_id = _bind_conversation_task(scoped, first)
    second = _running_restart_task(scoped, heartbeat_at=time.time())
    scoped.conversation_store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": second.id,
            "goal": second.goal,
            "status": "active",
        }
    )
    second = scoped.subagents.load(second.id)
    second.attributes = {
        **dict(second.attributes or {}),
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": parent_task_id,
    }
    scoped.subagents.save(second)
    scoped.conversation_store.tasks.update_status(
        {"task_id": parent_task_id, "status": "completed", "expected_status": "active"}
    )
    assert scoped.conversation_store.claims.acquire(
        {
            "thread_id": thread.thread_id,
            "task_id": parent_task_id,
            "reason": "user_guidance",
            "lease_seconds": 300,
        }
    ) is not None

    calls = {"policies": 0, "claim": 0}
    original_policies = scoped.conversation_store.progress.list_report
    original_claim = scoped.conversation_store.claims.load_report

    def policies(*args, **kwargs):
        calls["policies"] += 1
        return original_policies(*args, **kwargs)

    def claim(*args, **kwargs):
        calls["claim"] += 1
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(scoped.conversation_store.progress, 'list_report', policies)
    monkeypatch.setattr(scoped.conversation_store.claims, "load_report", claim)

    current = [scoped.subagents.load(first.id), scoped.subagents.load(second.id)]
    decisions = conversation_lifecycle_decisions(scoped, current)

    assert all(decisions[item.id].reason == "parent_execution_active" for item in current)
    assert calls == {"policies": 1, "claim": 1}


def test_unreadable_execution_state_holds_child_under_completed_parent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _base_agent, _owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time())
    _thread, parent_task_id = _bind_conversation_task(scoped, task)
    scoped.conversation_store.tasks.update_status(
        {"task_id": parent_task_id, "status": "completed", "expected_status": "active"}
    )
    monkeypatch.setattr(
        scoped.conversation_store.claims,
        "load_report",
        lambda _thread_id: ({"task_id": parent_task_id, "expires_at": "invalid"}, None),
    )

    current = scoped.subagents.load(task.id)
    decision = conversation_lifecycle_decisions(scoped, [current])[current.id]

    assert decision.allowed is False
    assert decision.should_cancel is False
    assert decision.reason == "parent_execution_state_unavailable"
