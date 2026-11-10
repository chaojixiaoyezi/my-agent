"""ORCH-01：同一项已授权长期工作的定时检查沿原父任务管理直属下级；无权方只能转交请求。

全部使用真实 SimpleAgent(echo)、ConversationStore、SubAgentManager/runtime.db 与 SchedulerRepository/SchedulerService：
定时片经原 enqueue_ready_runs → claim_wake → mark_run_running 推进，RunParams 由 conversation.runtime._run_params
从真实 BackgroundRunRequest 构造；会话执行租约用原 claims.acquire + bind_claim_execution 绑定（与后台 claim 循环同一对象）。
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.orchestration.management_continuity import (
    AUTHORITY_SCHEDULED_CONTINUATION,
    ROUTE_ORIGIN_KIND,
    management_principals,
)
from agent_py_agent.agent.agent_core.orchestration.tools.cancel import CancelSubagentsTool
from agent_py_agent.agent.agent_core.orchestration.tools.capability import (
    ResolveCapabilityRequestsTool,
)
from agent_py_agent.agent.agent_core.runtime.guidance import (
    _pending_guidance_for_current_agent,
    _render_guidance_user_input,
)
from agent_py_agent.agent.agent_core.runtime.guidance_tool import SendGuidanceTool
from agent_py_agent.agent.conversation.claim_execution_binding import (
    ClaimExecutionBinding,
    bind_claim_execution,
)
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
from agent_py_agent.agent.scheduler.service import _SCHEDULER_WAKE_REASON
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.models import TaskStatus


@dataclass
class Case:
    agent: SimpleAgent
    config: AgentConfig
    workspace: Path
    thread: object
    parent_task_id: str
    child_id: str
    job: dict
    run_id: str = ""
    claim: dict | None = None


def _agent(config: AgentConfig, workspace: Path) -> SimpleAgent:
    return SimpleAgent(config, workspace)


def _thread(agent: SimpleAgent, conversation: str) -> object:
    return agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "u", "channel": "local", "channel_conversation_id": conversation,
        "channel_user_id": "u", "owner_id": agent.home_paths.owner_id,
        "owner_home": str(agent.home_paths.owner_home_dir),
    })


@pytest.fixture
def case(tmp_path) -> Case:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), enable_subagents=True)
    agent = _agent(config, workspace)
    thread = _thread(agent, "long-work")
    parent = agent.conversation_store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-T", "goal": "长期工作", "status": "active"}
    ).task_id
    child = agent.subagents.create_run(goal="子任务", thought="", plan=["做"], parent_id=parent, root_id=parent, depth=1)
    job = agent.scheduler_repository.create_job(SchedulerJobCreateRequest(
        name="定时检查", prompt="检查直属下级进展", thread_id=thread.thread_id, source_task_id=parent,
        schedule={"kind": "every", "every_seconds": 600, "timezone": "UTC", "anchor_at": 1000},
        creator={"local": True}, now=1000,
    ))[0]
    return Case(agent, config, workspace, thread, parent, child.id, job)


# 函数用途: 沿原定时派发、领取与运行标记推进一次 run，并按原后台 claim 形状取得会话执行租约。
def _start_scheduled_run(case: Case) -> None:
    agent, repo = case.agent, case.agent.scheduler_repository
    run_id = repo.reserve_manual_run(case.job["job_id"])["run_id"]
    wake_ids = agent.scheduler_service.enqueue_ready_runs(now=time.time() + 3600)
    signal = agent.conversation_store.wakes.pending_one(wake_ids[0])
    claimed = agent.scheduler_service.claim_wake(signal)
    assert claimed.status == "claimed"
    repo.mark_run_running(run_id, claimed.claim.claim_id)
    case.run_id = run_id
    case.claim = agent.conversation_store.claims.acquire(
        {"thread_id": case.thread.thread_id, "task_id": run_id, "reason": _SCHEDULER_WAKE_REASON}
    )
    case.agent._current_run_params = _scheduled_params(case, agent, signal)


def _scheduled_params(case: Case, agent: SimpleAgent, signal: object | None = None) -> object:
    if signal is None:
        signal = agent.conversation_store.wakes.raise_signal({
            "thread_id": case.thread.thread_id, "root_task_id": case.run_id, "reason": _SCHEDULER_WAKE_REASON,
            "summary": "s", "dedupe_key": f"scheduler-run:{case.run_id}",
            "metadata": {"scheduler_job_id": case.job["job_id"], "scheduler_run_id": case.run_id},
        })
    request = BackgroundRunRequest(
        thread_id=case.thread.thread_id, task_id=case.run_id,
        reason=_SCHEDULER_WAKE_REASON, wake_signal=signal.to_dict(),
    )
    return _run_params(case.thread.thread_id, request, agent, thread=case.thread)


def _bound(case: Case, agent: SimpleAgent | None = None):
    agent = agent or case.agent
    return bind_claim_execution(ClaimExecutionBinding(
        agent.conversation_store.claims, case.thread.thread_id, case.claim["claim_id"], case.claim["claim_scope_id"],
    ))


def _guide(agent: SimpleAgent, target: str, message: str, operation: str = "") -> tuple[object, dict]:
    params = {"target": target, "message": message}
    if operation:
        params["__operation_id"] = operation
    result = SendGuidanceTool(agent).execute(params)
    return result, json.loads(result.output)


def _child_messages(case: Case, agent: SimpleAgent | None = None) -> list[str]:
    agent = agent or case.agent
    return [item.message for item in agent.conversation_store.guidance.pending("agent_run", case.child_id)]


def _thread_wakes(case: Case) -> list[object]:
    return [w for w in case.agent.conversation_store.wakes.pending(limit=1000) if w.thread_id == case.thread.thread_id]


def _foreground(agent: SimpleAgent, thread: object, task_id: str) -> None:
    from types import SimpleNamespace

    agent._current_run_params = SimpleNamespace(
        run_id=f"gwreq-{task_id}", task_id=f"gwreq-{task_id}", request_id=f"gwreq-{task_id}",
        task_attributes={"conversation_task_id": task_id, "conversation_thread_id": thread.thread_id},
    )


def test_foreground_parent_and_its_scheduled_followup_both_guide_the_child(case):
    _foreground(case.agent, case.thread, case.parent_task_id)
    result, payload = _guide(case.agent, case.child_id, "前台补充")
    assert result.ok is True and payload["scope_resolution"]["authority"] == "self"

    _start_scheduled_run(case)
    with _bound(case):
        result, payload = _guide(case.agent, case.child_id, "定时检查补充")

    assert result.ok is True, payload
    resolution = payload["scope_resolution"]
    assert resolution["authority"] == AUTHORITY_SCHEDULED_CONTINUATION
    assert resolution["principal_run_id"] == case.parent_task_id
    assert resolution["scheduler_run_id"] == case.run_id
    assert _child_messages(case) == ["前台补充", "定时检查补充"]
    entry = case.agent.conversation_store.guidance.pending("agent_run", case.child_id)[-1]
    assert entry.sender == case.parent_task_id
    assert entry.metadata["management_authority"]["scheduler_run_id"] == case.run_id


def test_scheduled_followup_without_host_claim_binding_is_denied(case):
    _start_scheduled_run(case)
    result, payload = _guide(case.agent, case.child_id, "伪造的定时补充")

    assert result.ok is False and result.error_code == "TOOL_PERMISSION_DENIED"
    assert result.effect_outcome == "not_started"
    assert payload["details"]["scope_warnings"] == ["SCHEDULED_CONTINUATION_CLAIM_UNBOUND"]
    assert _child_messages(case) == []


def test_forged_scheduler_attributes_on_another_turn_grant_nothing(case):
    _start_scheduled_run(case)
    params = case.agent._current_run_params
    params.task_attributes = {**params.task_attributes, "conversation_task_id": "gwreq-other"}
    with _bound(case):
        result, payload = _guide(case.agent, case.child_id, "冒充定时片")

    assert result.ok is False
    assert payload["details"]["scope_warnings"] == [
        "SELF_TASK_IDENTITY_CONFLICT", "SCHEDULED_CONTINUATION_RUN_MISMATCH",
    ]
    assert _child_messages(case) == []


def test_stale_scheduler_run_from_a_previous_execution_grants_nothing(case):
    _start_scheduled_run(case)
    stale_run, stale_claim = case.run_id, case.claim
    claim = case.agent.scheduler_repository.get_active_run(stale_run)
    case.agent.scheduler_repository.finish_run(stale_run, claim["claim_id"], _done_finish())
    with _bound(case):
        result, payload = _guide(case.agent, case.child_id, "旧定时片")

    assert stale_claim is case.claim
    assert result.ok is False
    assert payload["details"]["scope_warnings"] == ["SCHEDULED_CONTINUATION_REVOKED"]
    assert _child_messages(case) == []


def _done_finish():
    from agent_py_agent.agent.scheduler.repository import SchedulerRunFinish

    return SchedulerRunFinish(status="done")


def test_paused_job_revokes_and_resume_restores_without_cached_grant(case):
    repo = case.agent.scheduler_repository
    _start_scheduled_run(case)
    paused = repo.pause_job(case.job["job_id"], expected_version=int(repo.get_job(case.job["job_id"])["version"]))
    with _bound(case):
        denied, payload = _guide(case.agent, case.child_id, "暂停后")
    repo.resume_job(case.job["job_id"], expected_version=int(paused["version"]))
    with _bound(case):
        allowed, _ = _guide(case.agent, case.child_id, "恢复后")

    assert denied.ok is False
    assert payload["details"]["scope_warnings"] == ["SCHEDULED_CONTINUATION_REVOKED"]
    assert allowed.ok is True
    assert _child_messages(case) == ["恢复后"]


def test_cancelled_parent_task_revokes_scheduled_management(case):
    _start_scheduled_run(case)
    case.agent.conversation_store.tasks.update_status(
        {"task_id": case.parent_task_id, "status": "cancelled", "expected_status": "active", "now": time.time()}
    )
    with _bound(case):
        result, payload = _guide(case.agent, case.child_id, "父任务已取消", operation="op-cancelled")

    assert result.ok is False and result.error_code == "TOOL_PERMISSION_DENIED"
    assert payload["details"]["scope_warnings"] == ["SCHEDULED_CONTINUATION_REVOKED"]
    # 原父任务已撤销：既不能代管，也没有可转交的父级邮箱。
    assert payload["details"]["route"]["reason"] == "AUTHORIZED_PARENT_NOT_RECEIVING"
    assert _child_messages(case) == []
    assert case.agent.conversation_store.guidance.pending("task", case.parent_task_id) == []


def test_unrelated_same_owner_thread_is_denied_and_routed_as_a_request(case):
    other_thread = _thread(case.agent, "unrelated")
    other_task = case.agent.conversation_store.tasks.bind(
        {"thread_id": other_thread.thread_id, "task_id": "task-U", "goal": "另一件事", "status": "active"}
    ).task_id
    _foreground(case.agent, other_thread, other_task)

    first, payload = _guide(case.agent, case.child_id, "请让它改用新数据源", operation="op-1")
    replay, replay_payload = _guide(case.agent, case.child_id, "请让它改用新数据源", operation="op-1")

    assert first.ok is True and replay.ok is True
    assert payload["delivery"] == "routed_to_authorized_parent"
    assert payload["delivered_to_target"] is False and payload["consumed"] is False
    route = payload["route"]
    assert route["parent_run_id"] == case.parent_task_id and route["target_type"] == "task"
    assert replay_payload["route"]["route_id"] == route["route_id"]
    assert _child_messages(case) == []
    pending = case.agent.conversation_store.guidance.pending("task", case.parent_task_id)
    assert len(pending) == 1
    metadata = pending[0].metadata
    assert metadata["origin_kind"] == ROUTE_ORIGIN_KIND
    assert metadata["requester_run_id"] == other_task
    assert metadata["requester_thread_id"] == other_thread.thread_id
    assert metadata["child_run_id"] == case.child_id
    # 父级看到的是宿主事件而不是用户原话，也不能当作新的用户指令。
    rendered = _render_guidance_user_input(pending)
    assert "不是当前用户的原话" in rendered and "不要直接执行" in rendered
    wakes = [w for w in _thread_wakes(case)
             if (w.metadata or {}).get("route_dedupe_key") == route["dedupe_key"]]
    assert len(wakes) == 1 and wakes[0].root_task_id == case.parent_task_id

    # 转交不回授权限：同一请求方仍不能停止该孩子，且拒绝不产生任何副作用。
    cancel = CancelSubagentsTool(case.agent).execute({"run_id": case.child_id})
    assert cancel.ok is False and cancel.error_code == "TOOL_PERMISSION_DENIED"
    assert case.parent_task_id in cancel.output
    assert case.agent.subagents.load(case.child_id).status != TaskStatus.CANCELLED.value


def test_routed_request_is_read_by_the_parents_own_background_turn_and_receipt_is_honest(case):
    other_thread = _thread(case.agent, "unrelated")
    _foreground(case.agent, other_thread, "task-U")
    _result, payload = _guide(case.agent, case.child_id, "转交内容", operation="op-route")
    route = payload["route"]
    wake = next(w for w in _thread_wakes(case)
                if (w.metadata or {}).get("route_dedupe_key") == route["dedupe_key"])

    request = BackgroundRunRequest(
        thread_id=wake.thread_id, task_id=wake.root_task_id, reason=wake.reason, wake_signal=wake.to_dict(),
    )
    parent_params = _run_params(wake.thread_id, request, case.agent, thread=case.thread)
    entries, task_id, _warning = _pending_guidance_for_current_agent(
        case.agent.conversation_store, parent_params, limit=20,
    )
    assert task_id == case.parent_task_id
    assert [entry.guidance_id for entry in entries] == [route["route_id"]]

    case.agent.conversation_store.guidance.mark_status(route["dedupe_key"], "rejected")
    _foreground(case.agent, other_thread, "task-U")
    _replay, replay_payload = _guide(case.agent, case.child_id, "转交内容", operation="op-route")
    assert replay_payload["route"]["receipt_status"] == "rejected"
    assert replay_payload["route"]["parent_consumed"] is False


def test_route_requires_host_operation_id_and_never_widens_on_missing_identity(case):
    other_thread = _thread(case.agent, "unrelated")
    _foreground(case.agent, other_thread, "task-U")
    result, payload = _guide(case.agent, case.child_id, "没有宿主操作编号")

    assert result.ok is False and result.error_code == "TOOL_PERMISSION_DENIED"
    assert payload["details"]["authorized_parent_run_id"] == case.parent_task_id
    assert case.agent.conversation_store.guidance.pending("task", case.parent_task_id) == []


def test_other_owner_cannot_guide_or_route(case):
    from agent_py_agent.agent.owner_scoped_pool import _config_with_owner
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    other = _agent(_config_with_owner(case.config, OwnerIdentity.provider_user("feishu", "bob")), case.workspace)
    assert other.home_paths.owner_id != case.agent.home_paths.owner_id
    _foreground(other, _thread(other, "bob-chat"), "task-B")
    result, _payload = _guide(other, case.child_id, "跨 owner", operation="op-x")

    assert result.ok is False
    assert result.effect_outcome == "not_started"
    assert _child_messages(case) == []
    assert case.agent.conversation_store.guidance.pending("task", case.parent_task_id) == []


def test_restart_rebuilds_continuity_from_persisted_facts_only(case):
    _start_scheduled_run(case)
    restarted = _agent(case.config, case.workspace)
    restarted._current_run_params = _scheduled_params(case, restarted)
    with _bound(case, restarted):
        result, payload = _guide(restarted, case.child_id, "重启后定时检查")

    assert result.ok is True, payload
    assert payload["scope_resolution"]["principal_run_id"] == case.parent_task_id
    assert _child_messages(case, restarted) == ["重启后定时检查"]


def test_concurrent_scheduled_guidance_and_duplicate_routes(case):
    _start_scheduled_run(case)
    results: list[object] = []

    def worker(message: str) -> None:
        with _bound(case):
            results.append(_guide(case.agent, case.child_id, message)[0])

    threads = [threading.Thread(target=contextvars.copy_context().run, args=(worker, f"并发-{i}")) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(results) == 4 and all(item.ok for item in results)
    assert sorted(_child_messages(case)) == sorted(f"并发-{i}" for i in range(4))

    other_thread = _thread(case.agent, "unrelated")
    routes: list[str] = []

    def router() -> None:
        _foreground(case.agent, other_thread, "task-U")
        routes.append(_guide(case.agent, case.child_id, "同一请求", operation="op-dup")[1]["route"]["route_id"])

    # 同一 Agent 的 _current_run_params 是线程局部的宿主上下文；每个线程各自设置。
    route_threads = [threading.Thread(target=router) for _ in range(3)]
    for thread in route_threads:
        thread.start()
    for thread in route_threads:
        thread.join(30)
    assert len(set(routes)) == 1 and len(routes) == 3
    assert len(case.agent.conversation_store.guidance.pending("task", case.parent_task_id)) == 1


def test_scheduled_followup_can_cancel_and_resolve_for_the_parent(case):
    _start_scheduled_run(case)
    with _bound(case):
        principals, warnings = management_principals(case.agent)
        resolve = ResolveCapabilityRequestsTool(case.agent).execute(
            {"run_id": case.child_id, "decision": "deny", "reason": "按现有权限继续"}
        )
        cancel = CancelSubagentsTool(case.agent).execute({"run_id": case.child_id})

    assert [p.run_id for p in principals] == [case.run_id, case.parent_task_id] and warnings == ()
    # 没有未决申请：通过授权门后返回“无需裁决”，而不是权限拒绝。
    assert resolve.ok is True and "无权" not in resolve.output
    assert cancel.ok is True, cancel.output
    assert case.agent.subagents.load(case.child_id).status == TaskStatus.CANCELLED.value


def test_unrelated_thread_resolve_denial_names_parent_and_changes_nothing(case):
    _foreground(case.agent, _thread(case.agent, "unrelated"), "task-U")
    result = ResolveCapabilityRequestsTool(case.agent).execute(
        {"run_id": case.child_id, "decision": "grant", "reason": "越权批准"}
    )

    assert result.ok is False
    assert case.parent_task_id in result.output and "没有转交" in result.output
    assert case.agent.conversation_store.guidance.pending("task", case.parent_task_id) == []


# ---------- PRO-MSG-004 扩展：定时延续修改直属下级目标（goal_binding.goal_tool_target） ----------


def _child_goal(case: Case) -> object:
    child = case.agent.subagents.load(case.child_id)
    return case.agent.conversation_store.goals.create(
        {"thread_id": child.agent_thread_id, "task_id": child.id, "objective": "原子目标"}
    )


def _update_child_goal(case: Case, goal: object, objective: str) -> object:
    return case.agent.tools.tools["update_goal"].execute(
        {"target_run_id": case.child_id, "objective": objective, "expected_revision": goal.revision}
    )


def _goal_objective(case: Case, goal: object) -> str:
    return case.agent.conversation_store.goals.load(goal.thread_id).objective


def test_scheduled_followup_reads_and_revises_the_parents_child_goal(case):
    goal = _child_goal(case)
    _start_scheduled_run(case)
    with _bound(case):
        read = case.agent.tools.tools["get_goal"].execute({"target_run_id": case.child_id})
        updated = _update_child_goal(case, goal, "定时检查调整后的子目标")

    assert read.ok is True, read.output
    assert updated.ok is True, updated.output
    current = case.agent.conversation_store.goals.load(goal.thread_id)
    assert current.objective == "定时检查调整后的子目标"
    assert current.goal_id == goal.goal_id and current.revision > goal.revision


def test_goal_edit_without_claim_binding_or_with_forged_attributes_is_denied(case):
    goal = _child_goal(case)
    _start_scheduled_run(case)
    unbound = _update_child_goal(case, goal, "未绑定租约")
    params = case.agent._current_run_params
    params.task_attributes = {**params.task_attributes, "conversation_task_id": "gwreq-other"}
    with _bound(case):
        forged = _update_child_goal(case, goal, "伪造属性")

    for result in (unbound, forged):
        assert result.ok is False and result.error_code == "GOAL_STATE_CONFLICT"
        assert result.effect_outcome == "not_started"
    assert _goal_objective(case, goal) == "原子目标"
    assert _child_messages(case) == []


def test_goal_edit_from_stale_finished_run_is_denied(case):
    goal = _child_goal(case)
    _start_scheduled_run(case)
    claim = case.agent.scheduler_repository.get_active_run(case.run_id)
    case.agent.scheduler_repository.finish_run(case.run_id, claim["claim_id"], _done_finish())
    with _bound(case):
        stale = _update_child_goal(case, goal, "旧定时片")

    assert stale.ok is False and stale.error_code == "GOAL_STATE_CONFLICT"
    assert _goal_objective(case, goal) == "原子目标"


def test_goal_edit_after_parent_cancelled_is_denied(case):
    goal = _child_goal(case)
    _start_scheduled_run(case)
    case.agent.conversation_store.tasks.update_status(
        {"task_id": case.parent_task_id, "status": "cancelled", "expected_status": "active", "now": time.time()}
    )
    with _bound(case):
        revoked = _update_child_goal(case, goal, "父任务已取消")

    assert revoked.ok is False and revoked.error_code == "GOAL_STATE_CONFLICT"
    assert _goal_objective(case, goal) == "原子目标"


def test_unrelated_thread_cannot_edit_child_goal_and_nothing_is_routed(case):
    goal = _child_goal(case)
    other_thread = _thread(case.agent, "unrelated")
    _foreground(case.agent, other_thread, "task-U")
    result = _update_child_goal(case, goal, "越权改目标")

    assert result.ok is False and result.error_code == "GOAL_STATE_CONFLICT"
    assert _goal_objective(case, goal) == "原子目标"
    assert case.agent.conversation_store.guidance.pending("task", case.parent_task_id) == []
    assert _child_messages(case) == []


# ---------- PRO-MSG-005 复核：ORCH01-R1..R4 与证据缺口 ----------


def _open_request(case: Case, request_id: str = "req-1", *, directory_only: bool = False) -> None:
    from agent_py_agent.agent.subagents.model_capabilities import CapabilityRequest

    task = case.agent.subagents.load(case.child_id)
    # 目录-only 申请只要求孩子自己任务区内的写目录，不增加工具，留在父级既有上界内。
    scope = {"path_scope": [str(Path(task.task_workspace_dir) / "out")]} if directory_only else {
        "requested_tools": ["run_command"]}
    task.capability_requests = [*task.capability_requests, CapabilityRequest(
        id=request_id, from_run_id=case.child_id, problem="需要写输出", needed_capability="write_output",
        status="OPEN", created_at=time.time(), **scope,
    )]
    case.agent.subagents.save(task)


def _request_status(case: Case, request_id: str = "req-1") -> str:
    task = case.agent.subagents.load(case.child_id)
    return next(item.status for item in task.capability_requests if item.id == request_id)


def _limit_pairs(monkeypatch, limit: int) -> None:
    from agent_py_agent.agent.capability import runtime_config_reload
    from agent_py_agent.agent.capability.config import CapabilityConfig

    monkeypatch.setattr(runtime_config_reload, "capability_config_for_agent",
                        lambda _agent: CapabilityConfig(session_pair_hourly_limit=limit))


@pytest.mark.parametrize("drop_scheduler_attrs", [False, True])
def test_r1_forged_parent_identity_in_scheduled_run_mutates_nothing(case, drop_scheduler_attrs):
    goal = _child_goal(case)
    _open_request(case)
    _start_scheduled_run(case)
    params = case.agent._current_run_params
    attrs = {**params.task_attributes, "conversation_task_id": case.parent_task_id}
    if drop_scheduler_attrs:
        attrs = {k: v for k, v in attrs.items() if not k.startswith("scheduler_")}
    params.task_attributes = attrs
    with _bound(case):
        principals, warnings = management_principals(case.agent)
        guide, _ = _guide(case.agent, case.child_id, "冒充父任务")
        grant = ResolveCapabilityRequestsTool(case.agent).execute(
            {"run_id": case.child_id, "decision": "grant", "reason": "冒充批准"})
        deny = ResolveCapabilityRequestsTool(case.agent).execute(
            {"run_id": case.child_id, "decision": "deny", "reason": "冒充拒绝"})
        cancel = CancelSubagentsTool(case.agent).execute({"run_id": case.child_id})
        read = case.agent.tools.tools["get_goal"].execute({"target_run_id": case.child_id})
        edit = _update_child_goal(case, goal, "冒充改目标")

    assert [p.run_id for p in principals] == [case.run_id]
    assert "SELF_TASK_IDENTITY_CONFLICT" in warnings
    assert not any(item.ok for item in (guide, grant, deny, cancel, read, edit))
    assert _child_messages(case) == []
    assert _request_status(case) == "OPEN"
    assert case.agent.subagents.load(case.child_id).status != TaskStatus.CANCELLED.value
    assert _goal_objective(case, goal) == "原子目标"


def test_r1_foreground_task_on_another_thread_is_not_trusted(case):
    other_thread = _thread(case.agent, "unrelated")
    _foreground(case.agent, other_thread, case.parent_task_id)
    principals, warnings = management_principals(case.agent)
    result, _ = _guide(case.agent, case.child_id, "别的线程冒充")

    assert principals[0].run_id != case.parent_task_id and warnings == ("SELF_TASK_UNPROVEN",)
    assert result.ok is False and _child_messages(case) == []


def test_scheduled_continuation_grants_and_denies_a_real_open_request(case):
    _open_request(case, "req-grant", directory_only=True)
    _open_request(case, "req-deny")
    _start_scheduled_run(case)
    with _bound(case):
        grant = ResolveCapabilityRequestsTool(case.agent).execute(
            {"run_id": case.child_id, "request_id": "req-grant", "decision": "grant", "reason": "允许跑测试"})
        deny = ResolveCapabilityRequestsTool(case.agent).execute(
            {"run_id": case.child_id, "request_id": "req-deny", "decision": "deny", "reason": "按现有权限继续"})

    assert grant.ok is True, grant.output
    assert deny.ok is True, deny.output
    task = case.agent.subagents.load(case.child_id)
    assert _request_status(case, "req-grant") == "GRANTED"
    assert _request_status(case, "req-deny") == "CLOSED"
    assert [g.request_id for g in task.capability_grants] == ["req-grant"]
    continuation = json.loads(deny.output)["continuation"]
    assert continuation["status"] in {"queued", "not_queued", "already_running"}
    assert task.attributes["capability_resolution_wake"]["decision"] == "deny"


def _coordinator_child(case: Case) -> tuple[object, object]:
    coordinator = case.agent.subagents.create_run(
        goal="协调者", thought="", plan=["派"], parent_id=case.parent_task_id, root_id=case.parent_task_id, depth=1)
    grandchild = case.agent.subagents.create_run(
        goal="孙任务", thought="", plan=["做"], parent_id=coordinator.id, root_id=case.parent_task_id, depth=2)
    return coordinator, grandchild


def test_r2_r4_subagent_parent_route_uses_bounded_pairs_and_replays_first(case, monkeypatch):
    _limit_pairs(monkeypatch, 1)
    coordinator, grandchild = _coordinator_child(case)
    _foreground(case.agent, _thread(case.agent, "unrelated"), "task-U")
    first, payload = _guide(case.agent, grandchild.id, "第一条", operation="op-a")
    second, second_payload = _guide(case.agent, grandchild.id, "第二条", operation="op-b")
    replay, replay_payload = _guide(case.agent, grandchild.id, "第一条", operation="op-a")

    assert first.ok is True and payload["route"]["target_type"] == "agent_run"
    assert payload["route"]["parent_run_id"] == coordinator.id
    assert second.ok is False and second_payload["details"]["route"]["reason"] == "SESSION_TASK_RATE_LIMIT"
    assert replay.ok is True and replay_payload["route"]["route_id"] == payload["route"]["route_id"]
    assert replay_payload["route"]["replayed"] is True
    pending = case.agent.conversation_store.guidance.pending("agent_run", coordinator.id)
    assert [entry.guidance_id for entry in pending] == [payload["route"]["route_id"]]


def test_r2_concurrent_distinct_routes_respect_the_pair_limit(case, monkeypatch):
    _limit_pairs(monkeypatch, 1)
    other_thread = _thread(case.agent, "unrelated")
    outcomes: list[bool] = []

    def router(index: int) -> None:
        _foreground(case.agent, other_thread, "task-U")
        outcomes.append(_guide(case.agent, case.child_id, f"并发-{index}", operation=f"op-{index}")[0].ok)

    workers = [threading.Thread(target=router, args=(index,)) for index in range(5)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
    assert sorted(outcomes) == [False, False, False, False, True]
    assert len(case.agent.conversation_store.guidance.pending("task", case.parent_task_id)) == 1


def test_r2_route_without_provable_sender_thread_is_refused(case):
    from types import SimpleNamespace

    case.agent._current_run_params = SimpleNamespace(
        run_id="gwreq-x", task_id="gwreq-x", request_id="gwreq-x", task_attributes={"conversation_task_id": "task-U"})
    result, payload = _guide(case.agent, case.child_id, "没有线程", operation="op-x")

    assert result.ok is False and payload["details"]["route"]["reason"] == "ROUTE_SENDER_THREAD_UNPROVABLE"
    assert case.agent.conversation_store.guidance.pending("task", case.parent_task_id) == []


def _end_parent(case: Case, status: str = "done") -> None:
    case.agent.conversation_store.tasks.update_status(
        {"task_id": case.parent_task_id, "status": status, "expected_status": "active", "now": time.time()})


def test_r3_ended_parent_is_not_routed_and_accepted_route_reports_honestly(case):
    other_thread = _thread(case.agent, "unrelated")
    _foreground(case.agent, other_thread, "task-U")
    accepted, payload = _guide(case.agent, case.child_id, "父级结束前", operation="op-before")
    _end_parent(case)
    refused, refused_payload = _guide(case.agent, case.child_id, "父级结束后", operation="op-after")
    replay, replay_payload = _guide(case.agent, case.child_id, "父级结束前", operation="op-before")

    assert accepted.ok is True and payload["route"]["parent_receiving"] is True
    assert refused.ok is False
    assert refused_payload["details"]["route"]["reason"] == "AUTHORIZED_PARENT_NOT_RECEIVING"
    assert replay.ok is True and replay_payload["route"]["receipt_status"] == "pending"
    assert replay_payload["route"]["parent_receiving"] is False and replay_payload["route"]["parent_consumed"] is False
    assert len(case.agent.conversation_store.guidance.pending("task", case.parent_task_id)) == 1


def _background_scheduler(agent: SimpleAgent, run_once):
    from agent_py_agent.agent.conversation import runtime as runtime_module

    runtime = runtime_module.BackgroundMainAgentRuntime(agent=agent, store=agent.conversation_store)
    runtime.run_once = run_once
    scheduler = runtime_module.BackgroundMainAgentScheduler({"runtime": runtime, "store": agent.conversation_store})
    return runtime_module._background_claim_dependencies(scheduler)


def test_r3_real_claim_path_does_not_run_an_ended_parent_for_its_route(case):
    from agent_py_agent.agent.conversation import background_claim as claim_module

    other_thread = _thread(case.agent, "unrelated")
    _foreground(case.agent, other_thread, "task-U")
    _ok, payload = _guide(case.agent, case.child_id, "等父级处理", operation="op-claim")
    _end_parent(case)
    wake = next(w for w in _thread_wakes(case) if (w.metadata or {}).get("route_dedupe_key") == payload["route"]["dedupe_key"])
    ran: list[object] = []
    report = claim_module.run_claimed(_background_scheduler(case.agent, ran.append), {
        "thread_id": wake.thread_id, "task_id": wake.root_task_id, "reason": wake.reason,
        "wake_signal": wake.to_dict(), "now": time.time(),
    })

    # ORCH01-R7：先单独断言从未执行，再断言租约报告状态，避免运算符优先级让非空 ran 也能通过。
    assert ran == []
    assert report is None or getattr(report, "status", "") != "finished"
    receipt = case.agent.conversation_store.guidance.receipt(payload["route"]["dedupe_key"])
    assert receipt.status == "pending"


def test_scheduled_guidance_through_the_real_claim_runner(case):
    from agent_py_agent.agent.conversation import background_claim as claim_module

    _start_scheduled_run(case)
    case.agent.conversation_store.claims.finish(
        {"thread_id": case.thread.thread_id, "claim_id": case.claim["claim_id"], "status": "finished"})
    results: list[object] = []

    def run_once(request):
        case.agent._current_run_params = _scheduled_params(case, case.agent)
        results.append(_guide(case.agent, case.child_id, "真实租约循环里的补充")[0])

    claim_module.run_claimed(_background_scheduler(case.agent, run_once), {
        "thread_id": case.thread.thread_id, "task_id": case.run_id, "reason": _SCHEDULER_WAKE_REASON,
        "now": time.time(),
    })

    assert len(results) == 1 and results[0].ok is True, results
    assert _child_messages(case) == ["真实租约循环里的补充"]


def test_route_through_the_real_tool_executor_injects_and_protects_operation_id(case):
    from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call

    other_thread = _thread(case.agent, "unrelated")
    _foreground(case.agent, other_thread, "task-U")
    attempt = case.agent.subagents.runtime_db.record_run_creation(
        owner_id=case.agent.home_paths.owner_id, goal="unrelated main turn", conversation_task_id="task-U",
        thread_id=other_thread.thread_id, run_id="gwreq-task-U", role="assistant",
    )["attempt_id"]
    call = {"run_id": "gwreq-task-U", "attempt_id": attempt}
    first = execute_registry_test_call(case.agent.tools, "send_guidance",
                                       {"target": case.child_id, "message": "经执行器"}, operation_id="op-exec", **call)
    again = execute_registry_test_call(case.agent.tools, "send_guidance", {"target": case.child_id, "message": "经执行器"},
                                       operation_id="op-exec", call_id="test-call-2", **call)
    forged = execute_registry_test_call(case.agent.tools, "send_guidance",
                                        {"target": case.child_id, "message": "x", "__operation_id": "forged"}, **call)

    assert first.ok is True, first.output
    assert json.loads(first.output)["route"]["route_id"] == json.loads(again.output)["route"]["route_id"]
    assert forged.ok is False and forged.error_code == "TOOL_INTERNAL_PARAMETER_FORBIDDEN"
    assert len(case.agent.conversation_store.guidance.pending("task", case.parent_task_id)) == 1


def test_route_receipt_and_wake_survive_restart(case):
    _foreground(case.agent, _thread(case.agent, "unrelated"), "task-U")
    _ok, payload = _guide(case.agent, case.child_id, "重启前", operation="op-restart")
    restarted = _agent(case.config, case.workspace)
    _foreground(restarted, _thread(restarted, "unrelated"), "task-U")
    replay, replay_payload = _guide(restarted, case.child_id, "重启前", operation="op-restart")

    assert replay.ok is True and replay_payload["route"]["route_id"] == payload["route"]["route_id"]
    wakes = [w for w in restarted.conversation_store.wakes.pending(limit=1000)
             if (w.metadata or {}).get("route_dedupe_key") == payload["route"]["dedupe_key"]]
    assert len(wakes) == 1
    assert len(restarted.conversation_store.guidance.pending("task", case.parent_task_id)) == 1


# ---------- PRO-MSG-008：ORCH01-R1 返回型读错误、R5 目标提前返回、R6 唤醒补发 ----------


def _forge_parent_on_scheduled_run(case: Case, monkeypatch) -> None:
    """定时 run S 把提名改成真实父任务 T、去掉调度属性，并让 claim 与 S 的 TaskLink 读口返回错误（不抛异常）。"""
    _start_scheduled_run(case)
    params = case.agent._current_run_params
    params.task_attributes = {k: v for k, v in {**params.task_attributes, "conversation_task_id": case.parent_task_id}.items()
                              if not k.startswith("scheduler_")}
    store = case.agent.conversation_store
    monkeypatch.setattr(store.claims, "load_report", lambda *_a, **_k: ({}, {"error_type": "DataCorruptionError"}))
    original = store.tasks.load_report

    def task_report(task_id):
        if task_id == case.run_id:
            return None, {"error_type": "DataCorruptionError"}
        return original(task_id)

    monkeypatch.setattr(store.tasks, "load_report", task_report)


def test_r1_returned_read_errors_fail_closed_for_every_control(case, monkeypatch):
    goal = _child_goal(case)
    _open_request(case)
    _forge_parent_on_scheduled_run(case, monkeypatch)
    with _bound(case):
        principals, warnings = management_principals(case.agent)
        guide, _ = _guide(case.agent, case.child_id, "读错误冒充")
        grant = ResolveCapabilityRequestsTool(case.agent).execute(
            {"run_id": case.child_id, "decision": "grant", "reason": "冒充批准"})
        deny = ResolveCapabilityRequestsTool(case.agent).execute(
            {"run_id": case.child_id, "decision": "deny", "reason": "冒充拒绝"})
        cancel = CancelSubagentsTool(case.agent).execute({"run_id": case.child_id})
        read = case.agent.tools.tools["get_goal"].execute({"target_run_id": case.child_id})
        edit = _update_child_goal(case, goal, "冒充改目标")

    assert case.parent_task_id not in [p.run_id for p in principals] and "SELF_TASK_UNPROVEN" in warnings
    assert not any(item.ok for item in (guide, grant, deny, cancel, read, edit))
    assert _child_messages(case) == [] and _request_status(case) == "OPEN"
    assert case.agent.subagents.load(case.child_id).status != TaskStatus.CANCELLED.value
    assert _goal_objective(case, goal) == "原子目标"


@pytest.mark.parametrize("read_errors", [False, True])
def test_r5_forged_parent_cannot_read_or_edit_the_parents_own_goal(case, monkeypatch, read_errors):
    parent_goal = case.agent.conversation_store.goals.create(
        {"thread_id": case.thread.thread_id, "task_id": case.parent_task_id, "objective": "父目标"})
    if read_errors:
        _forge_parent_on_scheduled_run(case, monkeypatch)
    else:
        _start_scheduled_run(case)
        params = case.agent._current_run_params
        params.task_attributes = {**params.task_attributes, "conversation_task_id": case.parent_task_id}
    with _bound(case):
        read = case.agent.tools.tools["get_goal"].execute({})
        edit = case.agent.tools.tools["update_goal"].execute(
            {"objective": "冒充改父目标", "expected_revision": parent_goal.revision})
        self_target = case.agent.tools.tools["get_goal"].execute({"target_run_id": case.parent_task_id})

    assert read.ok is False and edit.ok is False and self_target.ok is False
    assert read.error_code == edit.error_code == "GOAL_STATE_CONFLICT"
    assert case.agent.conversation_store.goals.load(case.thread.thread_id).objective == "父目标"


def test_r5_real_foreground_parent_still_reads_its_own_goal(case):
    case.agent.conversation_store.goals.create(
        {"thread_id": case.thread.thread_id, "task_id": case.parent_task_id, "objective": "父目标"})
    _foreground(case.agent, case.thread, case.parent_task_id)
    read = case.agent.tools.tools["get_goal"].execute({})

    assert read.ok is True and "父目标" in read.output


# ---------- PRO-MSG-016：create_goal 先证明自身身份，再写目标/任务登记/attrs ----------


def _create_goal_writes(case: Case) -> dict[str, object]:
    params = case.agent._current_run_params
    return {
        "goals": [(g.task_id, g.objective) for g in case.agent.conversation_store.goals.list(case.thread.thread_id)],
        "registered": case.agent.local_store.task_registry.lookup_task(case.parent_task_id),
        "attr_goal": dict(getattr(params, "task_attributes", None) or {}).get("thread_goal_id"),
    }


@pytest.mark.parametrize("error_source", ["none", "claim", "task_link", "both"])
def test_create_goal_with_forged_parent_nomination_writes_nothing(case, monkeypatch, error_source):
    if error_source == "both":
        # 提名改成 T、去掉调度属性，且租约与 S 的 TaskLink 读口都返回错误。
        _forge_parent_on_scheduled_run(case, monkeypatch)
    else:
        _start_scheduled_run(case)
    params = case.agent._current_run_params
    params.task_attributes = {k: v for k, v in {**params.task_attributes, "conversation_task_id": case.parent_task_id}.items()
                              if not k.startswith("scheduler_")}
    store = case.agent.conversation_store
    scope = _bound(case)
    if error_source == "claim":
        params.task_id = params.run_id = "gwreq-forged"
        monkeypatch.setattr(store.claims, "load_report", lambda *_a, **_k: ({}, {"error_type": "DataCorruptionError"}))
    elif error_source == "task_link":
        original = store.tasks.load_report
        monkeypatch.setattr(store.tasks, "load_report", lambda task_id: (None, {"error_type": "DataCorruptionError"})
                            if task_id == case.run_id else original(task_id))
        scope = contextlib.nullcontext()
    before = _create_goal_writes(case)
    with scope:
        result = case.agent.tools.tools["create_goal"].execute({"objective": "冒充给父任务建目标"})

    assert result.ok is False and result.error_code == "GOAL_STATE_CONFLICT"
    assert result.effect_outcome == "not_started"
    assert _create_goal_writes(case) == before and before["goals"] == []


def test_create_goal_positive_control_scheduled_run_creates_its_own_goal(case):
    _start_scheduled_run(case)
    with _bound(case):
        result = case.agent.tools.tools["create_goal"].execute({"objective": "定时自身目标"})

    assert result.ok is True, result.output
    assert [(g.task_id, g.objective) for g in case.agent.conversation_store.goals.list(case.thread.thread_id)] == [
        (case.run_id, "定时自身目标")]


def test_create_goal_positive_control_real_foreground_parent(case):
    _foreground(case.agent, case.thread, case.parent_task_id)
    result = case.agent.tools.tools["create_goal"].execute({"objective": "父任务自己的目标"})

    assert result.ok is True, result.output
    assert [(g.task_id, g.objective) for g in case.agent.conversation_store.goals.list(case.thread.thread_id)] == [
        (case.parent_task_id, "父任务自己的目标")]
    assert case.agent._current_run_params.task_attributes.get("thread_goal_id")
    assert case.agent.local_store.task_registry.lookup_task(case.parent_task_id) is not None


def test_r6_wake_failure_after_append_is_repaired_once_by_replay(case, monkeypatch):
    _limit_pairs(monkeypatch, 1)
    _foreground(case.agent, _thread(case.agent, "unrelated"), "task-U")
    wakes = case.agent.conversation_store.wakes
    original = wakes.raise_signal
    calls = {"n": 0}

    def failing_once(request, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("wake store unavailable")
        return original(request, **kwargs)

    monkeypatch.setattr(wakes, "raise_signal", failing_once)
    first, first_payload = _guide(case.agent, case.child_id, "要父级处理", operation="op-wake")
    key = "orchestration-route:"
    pending_before = [w for w in _thread_wakes(case) if (w.metadata or {}).get("origin_kind") == ROUTE_ORIGIN_KIND]
    results: list[tuple[object, dict]] = []
    barrier = threading.Barrier(2)

    unrelated = _thread(case.agent, "unrelated")

    def replay() -> None:
        # 运行参数按线程隔离：每个工作线程都要带上同一个无关线程身份，否则就成了宿主系统身份。
        _foreground(case.agent, unrelated, "task-U")
        barrier.wait()
        results.append(_guide(case.agent, case.child_id, "要父级处理", operation="op-wake"))

    threads = [threading.Thread(target=replay) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    route_wakes = [w for w in _thread_wakes(case) if (w.metadata or {}).get("origin_kind") == ROUTE_ORIGIN_KIND]
    other_op, other_payload = _guide(case.agent, case.child_id, "另一条", operation="op-other")

    assert first.ok is False and first.error_code == "TOOL_EXECUTION_FAILED" and first.effect_outcome == "unknown"
    assert pending_before == []
    assert len(results) == 2
    for result, payload in results:
        assert result.ok is True and payload.get("route", {}).get("replayed") is True, payload
    assert len(route_wakes) == 1 and route_wakes[0].metadata["route_dedupe_key"].startswith(key)
    assert len(case.agent.conversation_store.guidance.pending("task", case.parent_task_id)) == 1
    # 补发不计配额：上限 1 已被首次写入占用，新操作被限流，重放没有被限流。
    assert other_op.ok is False and other_payload["details"]["route"]["reason"] == "SESSION_TASK_RATE_LIMIT"

    wakes.mark_handled(route_wakes[0].wake_signal_id)
    again, _ = _guide(case.agent, case.child_id, "要父级处理", operation="op-wake")
    assert again.ok is True
    assert [w for w in _thread_wakes(case) if (w.metadata or {}).get("origin_kind") == ROUTE_ORIGIN_KIND] == []


@pytest.mark.parametrize("error_source", ["claim", "task_link"])
def test_r1_each_unreadable_execution_fact_alone_fails_closed(case, monkeypatch, error_source):
    """只有一个执行事实能揭示真实执行 S，且它返回读错误时，自身身份也不能回落成提名的父任务 T。"""
    _open_request(case)
    _start_scheduled_run(case)
    params = case.agent._current_run_params
    params.task_attributes = {k: v for k, v in {**params.task_attributes, "conversation_task_id": case.parent_task_id}.items()
                              if not k.startswith("scheduler_")}
    store = case.agent.conversation_store
    if error_source == "claim":
        # RunParams 不再指向任何 TaskLink，只剩租约能证明 S；租约读口返回错误。
        params.task_id = params.run_id = "gwreq-forged"
        monkeypatch.setattr(store.claims, "load_report", lambda *_a, **_k: ({}, {"error_type": "DataCorruptionError"}))
        scope = _bound(case)
    else:
        # 不绑定租约，只剩 RunParams 指向的 S TaskLink；它的读口返回错误。
        original = store.tasks.load_report
        monkeypatch.setattr(store.tasks, "load_report", lambda task_id: (None, {"error_type": "DataCorruptionError"})
                            if task_id == case.run_id else original(task_id))
        scope = contextlib.nullcontext()
    with scope:
        principals, warnings = management_principals(case.agent)
        guide, _ = _guide(case.agent, case.child_id, "单一读错误冒充")
        deny = ResolveCapabilityRequestsTool(case.agent).execute(
            {"run_id": case.child_id, "decision": "deny", "reason": "冒充拒绝"})

    assert case.parent_task_id not in [p.run_id for p in principals] and "SELF_TASK_UNPROVEN" in warnings
    assert guide.ok is False and deny.ok is False
    assert _child_messages(case) == [] and _request_status(case) == "OPEN"
