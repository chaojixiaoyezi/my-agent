from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.background import dispatch as background_dispatch
from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchParams
from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_worker import _owner_pool
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
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "u-restart",
            "channel": "feishu",
            "channel_conversation_id": f"chat-{task.id}",
            "channel_user_id": "u-restart",
        }
    )
    parent_task_id = f"parent-{task.id}"
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": parent_task_id,
            "goal": "parent task",
            "status": parent_status,
        }
    )
    store.bind_task(
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
    scoped.conversation_store.update_task_status(
        {"task_id": parent_task_id, "status": "completed", "expected_status": "active"}
    )
    shared_active_owner_registry(base_agent).record(owner)
    starts = _capture_background_starts(monkeypatch)
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)

    reports = gateway_loops._GatewayOrphanReconciler(_context(base_agent, tmp_path)).tick()

    assert scoped.subagents.load(task.id).status == "CANCELLED"
    assert starts == []
    links = {link.task_id: link for link in scoped.conversation_store.task_links(thread.thread_id)}
    assert links[parent_task_id].status == "completed"
    assert links[task.id].status == "cancelled"
    assert any(int(report.get("parent_closed_cancelled") or 0) == 1 for report in reports)


def test_reconciler_cancels_old_run_when_parent_conversation_interrupted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_agent, owner, scoped = _scoped_restart_fixture(tmp_path)
    task = _running_restart_task(scoped, heartbeat_at=time.time() - 120.0)
    _thread, parent_task_id = _bind_conversation_task(scoped, task)
    scoped.conversation_store.update_task_status(
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
    thread = scoped.conversation_store.get_or_create_thread(
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
    (scoped.conversation_store.tasks_dir / f"{parent_task_id}.json").write_text(
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
    scoped.conversation_store.update_task_status(
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
