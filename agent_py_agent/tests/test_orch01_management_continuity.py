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
    # 测试宿主登记真实主根执行，再创建下级；TaskLink的parent/root字段本身不证明运行库归属。
    repository = agent.subagents.runtime_db
    task = repository.create_task(owner_id=agent.home_paths.owner_id, thread_id=thread.thread_id,
                                  conversation_task_id=parent)
    task_run = repository.create_task_run(task_id=task["task_id"])
    repository.create_agent_run(task_run_id=task_run["task_run_id"], run_id=parent, role="main")
    child = agent.subagents.create_run(goal="子任务", thought="", plan=["做"], parent_id=parent, root_id=parent,
                                      depth=1, allowed_tools=["read_file", "manage_handoff_board"])
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
    assert pending[0].sender == other_thread.thread_id
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


# ---------- PRO-MSG-031(2)：execute_scoped 边界缺请求方身份时失败关闭（REQUESTER_IDENTITY_UNPROVEN） ----------


# LLM: 快照只采集 guidance、wake、会话任务与子代理目录的非锁文件原始字节及结构化状态，不触发任何修复或副作用。
# 函数用途: 采集当前 owner 下消息箱、唤醒队列、子代理任务与能力申请的精确字节快照，验证拒绝路径零改动。
def _control_plane_fact_bytes(case: Case, *extra_run_ids: str) -> dict[str, object]:
    owner_home = Path(case.agent.home_paths.owner_home_dir)
    tracked_files: dict[str, bytes] = {}
    for path in sorted(owner_home.rglob("*")):
        if not path.is_file():
            continue
        if path.name.endswith((".lock", ".guard")) or "sqlite" in path.name or path.name.endswith(".db"):
            continue
        rel = path.relative_to(owner_home).as_posix()
        if "/conversations/" in f"/{rel}" or "/subagents/" in f"/{rel}" or rel.startswith("agents/"):
            tracked_files[rel] = path.read_bytes()
    run_ids = (case.child_id, *extra_run_ids)
    return {
        "files": tracked_files,
        "child_guidance": [
            (rid, [entry.to_dict() for entry in case.agent.conversation_store.guidance.pending("agent_run", rid)])
            for rid in run_ids
        ],
        "parent_guidance": [
            entry.to_dict()
            for entry in case.agent.conversation_store.guidance.pending("task", case.parent_task_id)
        ],
        "wakes": [wake.to_dict() for wake in _thread_wakes(case)],
    }


# LLM: 统一核对注册表执行结果或直接 execute_scoped 结果的三要素：TOOL_PERMISSION_DENIED、not_started 与 REQUESTER_IDENTITY_UNPROVEN。
# 函数用途: 断言一次未证明请求方身份的调用被精确失败关闭并携带结构化原因码。
def _assert_unproven_requester_denied(result: object) -> None:
    assert result.ok is False, getattr(result, "output", "")
    assert result.error_code == "TOOL_PERMISSION_DENIED"
    assert result.effect_outcome == "not_started"
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["reason"] == "REQUESTER_IDENTITY_UNPROVEN"
    assert payload["details"]["reason"] == "REQUESTER_IDENTITY_UNPROVEN"
    assert payload["details"]["error"]["reason"] == "REQUESTER_IDENTITY_UNPROVEN"
    metadata = getattr(result, "metadata", None)
    if isinstance(metadata, dict):
        envelope_details = metadata["handler_details"]["details"]
        assert envelope_details["reason"] == "REQUESTER_IDENTITY_UNPROVEN"
        assert envelope_details["error"]["reason"] == "REQUESTER_IDENTITY_UNPROVEN"
    else:
        envelope_details = result.result_envelope["details"]
        assert envelope_details["reason"] == "REQUESTER_IDENTITY_UNPROVEN"
        assert envelope_details["error"]["reason"] == "REQUESTER_IDENTITY_UNPROVEN"


# LLM: 为指定子代理打开一条目录或工具能力申请，供父级或协调子代理正向与负向测试复用。
# 函数用途: 在任意指定子代理 run 上追加一条 OPEN 状态的能力申请。
def _open_request_for_run(agent: SimpleAgent, run_id: str, request_id: str = "req-1", *, directory_only: bool = True) -> None:
    from agent_py_agent.agent.subagents.model_capabilities import CapabilityRequest

    task = agent.subagents.load(run_id)
    scope = (
        {"path_scope": [str(Path(task.task_workspace_dir) / "out")]}
        if directory_only
        else {"requested_tools": ["run_command"]}
    )
    task.capability_requests = [
        *task.capability_requests,
        CapabilityRequest(
            id=request_id,
            from_run_id=run_id,
            problem="需要写输出",
            needed_capability="write_output",
            status="OPEN",
            created_at=time.time(),
            **scope,
        ),
    ]
    agent.subagents.save(task)


# LLM: 沿真实子代理 runner 启动路径（reuse_pending=True）激活指定子代理的 generation-1 执行轮，供注册表 MANAGED 权威门核验。
# 函数用途: 激活已创建子代理的 pending attempt 并返回 running 态的 attempt_id。
def _activate_subagent_attempt(agent: SimpleAgent, subagent_run_id: str) -> str:
    run_row = agent.subagents.runtime_db.agent_run_for_run_id(subagent_run_id)
    assert run_row is not None
    attempt_row = agent.subagents.runtime_db.create_attempt(
        str(run_row["agent_run_id"]),
        reuse_pending=True,
    )
    return str(attempt_row["attempt_id"])


# LLM: 在真实裸工作线程上经 canonical registry.execute_tool 调用三项控制工具；即便调用账带已登记 attempt 也不得从参数/工作区推断请求方身份，且直接宿主 execute() 语义不变。
# 函数用途: 验证裸线程经注册表执行 send_guidance、resolve_capability_requests、cancel_subagents 时全部前置失败关闭且持久化事实逐字节不变。
def test_031_2_bare_thread_canonical_executor_rejects_all_three_tools_with_byte_identical_facts(case):
    from agent_py_agent.tests._tool_runtime_harness import (
         _authority_attempt_id,
        execute_registry_test_call,
    )

    _open_request(case, "req-bare", directory_only=True)
    # 先在主线程预先登记运行库 attempt，避免登记本身改动被测前后快照；裸线程不设任何 _current_run_params 或 runner 上下文。
    attempt = _authority_attempt_id(case.agent, case.parent_task_id)
    before = _control_plane_fact_bytes(case)
    outcomes: dict[str, object] = {}

    # LLM: 裸线程入口不设置 _current_run_params 与 subagent 上下文，模拟缺少宿主线程身份的注册表调用。
    # 函数用途: 在无线程身份的工作线程上依次经注册表调用三项控制工具。
    def bare_worker() -> None:
        common = {"run_id": case.parent_task_id, "attempt_id": attempt}
        outcomes["guide"] = execute_registry_test_call(
            case.agent.tools,
            "send_guidance",
            {"target": case.child_id, "message": "裸线程消息"},
            call_id="bare-guide",
            operation_id="op-bare-guide",
            **common,
        )
        outcomes["resolve"] = execute_registry_test_call(
            case.agent.tools,
            "resolve_capability_requests",
            {"run_id": case.child_id, "request_id": "req-bare", "decision": "grant", "reason": "裸线程批准"},
            call_id="bare-resolve",
            operation_id="op-bare-resolve",
            **common,
        )
        outcomes["cancel"] = execute_registry_test_call(
            case.agent.tools,
            "cancel_subagents",
            {"run_id": case.child_id},
            call_id="bare-cancel",
            operation_id="op-bare-cancel",
            **common,
        )

    worker = threading.Thread(target=bare_worker)
    worker.start()
    worker.join(30)
    assert not worker.is_alive()

    for key in ("guide", "resolve", "cancel"):
        _assert_unproven_requester_denied(outcomes[key])
    after = _control_plane_fact_bytes(case)
    assert after == before
    assert _request_status(case, "req-bare") == "OPEN"
    assert case.agent.subagents.load(case.child_id).status != TaskStatus.CANCELLED.value


# LLM: 缺失 ToolInvocationContext 或缺少真实执行编号（run_id/task_id/request_id 全空）时，execute_scoped 同样拒绝；直接调用 execute() 保持原宿主行为不变。
# 函数用途: 验证缺上下文或空执行编号在 execute_scoped 边界失败关闭，同时不拓宽或破坏直接 execute() 宿主入口。
def test_031_2_missing_context_and_empty_exec_id_fail_closed_while_direct_execute_stays_intact(case):
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling.models import ToolInvocationContext

    _open_request(case, "req-ctx", directory_only=True)
    _foreground(case.agent, case.thread, case.parent_task_id)
    before = _control_plane_fact_bytes(case)

    # 即使线程上有合法前台身份，缺失 invocation_context 也不能静默当成受信任宿主。
    missing_guide = SendGuidanceTool(case.agent).execute_scoped(
        {"target": case.child_id, "message": "缺上下文"}, None,  # type: ignore[arg-type]
    )
    missing_resolve = ResolveCapabilityRequestsTool(case.agent).execute_scoped(
        {"run_id": case.child_id, "request_id": "req-ctx", "decision": "deny", "reason": "缺上下文"},
        None,  # type: ignore[arg-type]
    )
    missing_cancel = CancelSubagentsTool(case.agent).execute_scoped(
        {"run_id": case.child_id}, None,  # type: ignore[arg-type]
    )
    for item in (missing_guide, missing_resolve, missing_cancel):
        _assert_unproven_requester_denied(item)
    assert _control_plane_fact_bytes(case) == before

    # 只有提名属性、没有任何执行编号（run_id/task_id/request_id 全空）也不能通过 execute_scoped。
    snapshot = case.agent.tools.runtime_snapshot(run_id="snap-only")
    ctx = ToolInvocationContext(runtime_snapshot=snapshot)
    case.agent._current_run_params = SimpleNamespace(
        run_id="",
        task_id="",
        request_id="   ",
        task_attributes={"conversation_task_id": case.parent_task_id, "conversation_thread_id": case.thread.thread_id},
    )
    empty_guide = SendGuidanceTool(case.agent).execute_scoped({"target": case.child_id, "message": "空执行编号"}, ctx)
    empty_resolve = ResolveCapabilityRequestsTool(case.agent).execute_scoped(
        {"run_id": case.child_id, "request_id": "req-ctx", "decision": "grant", "reason": "空执行编号"}, ctx,
    )
    empty_cancel = CancelSubagentsTool(case.agent).execute_scoped({"run_id": case.child_id}, ctx)
    for item in (empty_guide, empty_resolve, empty_cancel):
        _assert_unproven_requester_denied(item)
    assert _control_plane_fact_bytes(case) == before

    # 直接宿主入口 execute() 语义不变：清除 _current_run_params 后直接调 execute() 仍沿原有宿主空门工作。
    del case.agent._current_run_params
    direct = SendGuidanceTool(case.agent).execute({"target": case.child_id, "message": "宿主直接调用"})
    assert direct.ok is True
    assert _child_messages(case) == ["宿主直接调用"]


# LLM: 覆盖前台父任务、真实定时延续与子代理 runner 三条正向控制路径，全部经 canonical registry.execute_tool 走通三项工具。
# 函数用途: 验证已证明身份的前台回合、定时延续回合和子代理工作线程经注册表执行三项工具均正常放行。
def test_031_2_registry_positive_controls_foreground_scheduled_and_subagent_runner(case):
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )
    from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call

    # 1) 前台父任务正向对照
    child_fg = case.agent.subagents.create_run(
        goal="前台子任务", thought="", plan=["做"], parent_id=case.parent_task_id, root_id=case.parent_task_id, depth=1,
    )
    _open_request_for_run(case.agent, child_fg.id, "req-fg", directory_only=True)
    _foreground(case.agent, case.thread, case.parent_task_id)
    fg_run = f"gwreq-{case.parent_task_id}"
    fg_guide = execute_registry_test_call(
        case.agent.tools, "send_guidance", {"target": child_fg.id, "message": "前台注册表消息"},
        run_id=fg_run, call_id="fg-guide", register_with=case.agent,
    )
    fg_resolve = execute_registry_test_call(
        case.agent.tools, "resolve_capability_requests",
        {"run_id": child_fg.id, "request_id": "req-fg", "decision": "grant", "reason": "前台批准目录"},
        run_id=fg_run, call_id="fg-resolve", register_with=case.agent,
    )
    fg_cancel = execute_registry_test_call(
        case.agent.tools, "cancel_subagents", {"run_id": child_fg.id},
        run_id=fg_run, call_id="fg-cancel", register_with=case.agent,
    )
    assert fg_guide.ok is True and json.loads(fg_guide.output)["scope_resolution"]["authority"] == "self"
    assert fg_resolve.ok is True, fg_resolve.output
    assert fg_cancel.ok is True, fg_cancel.output
    assert case.agent.subagents.load(child_fg.id).status == TaskStatus.CANCELLED.value

    # 2) 真实定时延续正向对照
    _open_request(case, "req-sched", directory_only=True)
    _start_scheduled_run(case)
    with _bound(case):
        sched_guide = execute_registry_test_call(
            case.agent.tools, "send_guidance", {"target": case.child_id, "message": "定时注册表消息"},
            run_id=case.run_id, call_id="sched-guide", register_with=case.agent,
        )
        sched_resolve = execute_registry_test_call(
            case.agent.tools, "resolve_capability_requests",
            {"run_id": case.child_id, "request_id": "req-sched", "decision": "deny", "reason": "定时拒绝申请"},
            run_id=case.run_id, call_id="sched-resolve", register_with=case.agent,
        )
        sched_cancel = execute_registry_test_call(
            case.agent.tools, "cancel_subagents", {"run_id": case.child_id},
            run_id=case.run_id, call_id="sched-cancel", register_with=case.agent,
        )
    assert sched_guide.ok is True
    assert json.loads(sched_guide.output)["scope_resolution"]["authority"] == AUTHORITY_SCHEDULED_CONTINUATION
    assert sched_resolve.ok is True, sched_resolve.output
    assert _request_status(case, "req-sched") == "CLOSED"
    assert sched_cancel.ok is True, sched_cancel.output
    assert case.agent.subagents.load(case.child_id).status == TaskStatus.CANCELLED.value

    # 3) 子代理 runner 工作线程正向对照
    coordinator, grandchild = _coordinator_child(case)
    coord_attempt = _activate_subagent_attempt(case.agent, coordinator.id)
    _open_request_for_run(case.agent, grandchild.id, "req-sub", directory_only=True)
    runner_outcomes: dict[str, object] = {}

    # LLM: 在独立工作线程进入真实 set_current_subagent_context 作用域并经注册表执行三项工具，退出时完整清理线程上下文。
    # 函数用途: 模拟协调子代理 runner 线程管理其直属孙代理。
    def runner_worker() -> None:
        previous = set_current_subagent_context(case.agent, run_id=coordinator.id, attempt_id=coord_attempt)
        try:
            runner_outcomes["guide"] = execute_registry_test_call(
                case.agent.tools, "send_guidance", {"target": grandchild.id, "message": "协调者补充"},
                run_id=coordinator.id, attempt_id=coord_attempt, call_id="sub-guide",
            )
            runner_outcomes["resolve"] = execute_registry_test_call(
                case.agent.tools, "resolve_capability_requests",
                {"run_id": grandchild.id, "request_id": "req-sub", "decision": "grant", "reason": "协调者批准目录"},
                run_id=coordinator.id, attempt_id=coord_attempt, call_id="sub-resolve",
            )
            runner_outcomes["cancel"] = execute_registry_test_call(
                case.agent.tools, "cancel_subagents", {"run_id": grandchild.id},
                run_id=coordinator.id, attempt_id=coord_attempt, call_id="sub-cancel",
            )
        finally:
            restore_current_subagent_context(case.agent, previous)

    thread = threading.Thread(target=runner_worker)
    thread.start()
    thread.join(30)
    assert not thread.is_alive()
    assert runner_outcomes["guide"].ok is True
    assert json.loads(runner_outcomes["guide"].output)["scope_resolution"]["principal_run_id"] == coordinator.id
    assert runner_outcomes["resolve"].ok is True, runner_outcomes["resolve"].output
    assert runner_outcomes["cancel"].ok is True, runner_outcomes["cancel"].output
    assert case.agent.subagents.load(grandchild.id).status == TaskStatus.CANCELLED.value


# LLM: 伪造提名父任务或兄弟子代理冒充父级经注册表调用三项工具时，由既有权威身份门与直属下级门拒绝，且不改动目标持久化事实。
# 函数用途: 验证注册表入口下伪造/错配身份的负向控制与持久化事实逐字节不变。
def test_031_2_registry_forged_and_mismatched_identities_deny_without_mutating_facts(case):
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )
    from agent_py_agent.tests._tool_runtime_harness import (
        _authority_attempt_id,
        execute_registry_test_call,
    )

    sibling = case.agent.subagents.create_run(
        goal="兄弟代理", thought="", plan=["看"], parent_id=case.parent_task_id, root_id=case.parent_task_id, depth=1,
    )
    _open_request(case, "req-forged", directory_only=True)
    _start_scheduled_run(case)
    # 先预建两个调用方的运行库 attempt，随后采集精确控制面快照。
    sched_attempt = _authority_attempt_id(case.agent, case.run_id)
    sibling_attempt = _activate_subagent_attempt(case.agent, sibling.id)
    params = case.agent._current_run_params
    # 伪造 conversation_task_id 为父任务且清空线程证明（使转交也因无发送线程证明而拒绝），核验零写盘。
    params.task_attributes = {"conversation_task_id": case.parent_task_id}
    before = _control_plane_fact_bytes(case, sibling.id)

    with _bound(case):
        forged_guide = execute_registry_test_call(
            case.agent.tools, "send_guidance", {"target": case.child_id, "message": "伪造补充"},
            run_id=case.run_id, attempt_id=sched_attempt, call_id="forged-guide", operation_id="op-forged-guide",
        )
        forged_resolve = execute_registry_test_call(
            case.agent.tools, "resolve_capability_requests",
            {"run_id": case.child_id, "request_id": "req-forged", "decision": "grant", "reason": "伪造批准"},
            run_id=case.run_id, attempt_id=sched_attempt, call_id="forged-resolve",
        )
        forged_cancel = execute_registry_test_call(
            case.agent.tools, "cancel_subagents", {"run_id": case.child_id},
            run_id=case.run_id, attempt_id=sched_attempt, call_id="forged-cancel",
        )

    sibling_outcomes: dict[str, object] = {}

    # LLM: 兄弟子代理没有 agent_thread 绑定或不是目标的直接父级，不能直接控制目标子代理。
    # 函数用途: 在兄弟子代理线程上尝试越权引导（无 operation_id 不转交）、裁决和取消目标子代理。
    def sibling_worker() -> None:
        previous = set_current_subagent_context(case.agent, run_id=sibling.id, attempt_id=sibling_attempt)
        try:
            # 传空 operation_id? Registry 会自动注入 __operation_id；为测纯拒绝零写盘，验证兄弟对裁决与取消的拒绝，以及缺线程证明的引导拒绝。
            sibling_outcomes["guide"] = execute_registry_test_call(
                case.agent.tools, "send_guidance", {"target": case.child_id, "message": "兄弟越权引导"},
                run_id=sibling.id, attempt_id=sibling_attempt, call_id="sib-guide",
            )
            sibling_outcomes["resolve"] = execute_registry_test_call(
                case.agent.tools, "resolve_capability_requests",
                {"run_id": case.child_id, "request_id": "req-forged", "decision": "deny", "reason": "兄弟越权拒绝"},
                run_id=sibling.id, attempt_id=sibling_attempt, call_id="sib-resolve",
            )
            sibling_outcomes["cancel"] = execute_registry_test_call(
                case.agent.tools, "cancel_subagents", {"run_id": case.child_id},
                run_id=sibling.id, attempt_id=sibling_attempt, call_id="sib-cancel",
            )
        finally:
            restore_current_subagent_context(case.agent, previous)

    thread = threading.Thread(target=sibling_worker)
    thread.start()
    thread.join(30)
    assert not thread.is_alive()

    for denied in (
        forged_guide,
        forged_resolve,
        forged_cancel,
        sibling_outcomes["guide"],
        sibling_outcomes["resolve"],
        sibling_outcomes["cancel"],
    ):
        assert denied.ok is False, denied.output
        assert denied.error_code != "TOOL_AUTHORITY_CONTEXT_MISSING", denied.output
        assert denied.handler_executed is True
    assert _control_plane_fact_bytes(case, sibling.id) == before
    assert _request_status(case, "req-forged") == "OPEN"
    assert case.agent.subagents.load(case.child_id).status != TaskStatus.CANCELLED.value


def test_routed_native_request_passes_the_actual_parent_instruction_admission_and_claim(case):
    """队列可读还不够：真实来源分类与精确turn认领不能误拒合法的run/thread分列。"""
    from agent_py_agent.agent.agent_core.runtime.guidance import _claim_guidance_for_active_turn

    thread = _thread(case.agent, "native-requester")
    task = case.agent.conversation_store.tasks.bind({
        "thread_id": thread.thread_id, "task_id": "task-U", "status": "active",
    })
    _foreground(case.agent, thread, task.task_id)
    result, payload = _guide(case.agent, case.child_id, "safe forwarded request", operation="native-claim-op")
    assert result.ok and payload["delivered_to_target"] is False
    route = payload["route"]
    entries = case.agent.conversation_store.guidance.pending("task", case.parent_task_id)
    claimed = _claim_guidance_for_active_turn(case.agent.conversation_store, entries,
        "parent-real-turn", "parent-real-attempt", owner_id=case.agent.home_paths.owner_id)
    assert [entry.guidance_id for entry in claimed] == [route["route_id"]]
    receipt = case.agent.conversation_store.guidance.receipt(route["dedupe_key"])
    assert receipt.status == "reserved"
    assert claimed[0].sender == thread.thread_id
    assert claimed[0].metadata["requester_run_id"] == task.task_id
    assert claimed[0].metadata["origin_thread_id"] == thread.thread_id
    assert _child_messages(case) == []
    assert claimed[0].metadata["expected_turn_id"] == "parent-real-turn"

    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.runtime.loop_support import _completed_turn_native_messages
    from agent_py_agent.agent.backends.tool_ir import AssistantTurn, UserTurn
    from agent_py_agent.agent.runtime_errors import DataCorruptionError
    from agent_py_agent.agent.tooling.runtime_contracts import (
        ProviderToolCapability,
        ToolCall,
        ToolContentBlock,
        ToolProtocolSnapshot,
        ToolResult,
    )

    call = ToolCall(call_id="prior-call", tool_name="read_file", arguments={"path": "data.txt"},
                    source_protocol="native", schema_hash="sha256:fixture-schema", run_id=case.parent_task_id,
                    turn_id="parent-real-turn", attempt_id="parent-real-attempt")
    history = [AssistantTurn(tool_calls=[call]),
               ToolResult("prior-call", "read_file", "succeeded",
                          content_blocks=(ToolContentBlock("text", "prior receipt retained"),)),
               UserTurn(claimed[0].message, input_ids=(claimed[0].guidance_id,))]
    params = SimpleNamespace(request_id="parent-real-turn", attempt_id="parent-real-attempt",
        run_id=case.parent_task_id, task_id=case.parent_task_id, tool_ir_history=history, turn_trigger=None,
        live_archive_state={"_guidance_ack_entries": {claimed[0].guidance_id: claimed[0]}},
        tool_protocol_snapshot=ToolProtocolSnapshot(case.parent_task_id, "native", ProviderToolCapability(
            provider="test", endpoint="local://test", model="test", stream=False, native_supported=True,
            evidence="canonical_fixture")))
    partial = json.dumps(_completed_turn_native_messages(params, None, agent=case.agent))
    assert "safe forwarded request" not in partial
    assert "prior-call" in partial and "prior receipt retained" in partial
    assert history[-1].text == claimed[0].message  # 私有待发送IR不被归档过滤改写。

    submissions = case.agent.conversation_store.guidance.submissions
    for wrong_turn, wrong_attempt in (("foreign-turn", "parent-real-attempt"),
                                      ("parent-real-turn", "foreign-attempt")):
        with pytest.raises(DataCorruptionError, match="reservation mismatch"):
            submissions.mark_submitted(wrong_turn, claimed, attempt_id=wrong_attempt,
                                       provider_call_id="not-authorized-call")
    assert case.agent.conversation_store.guidance.receipt(route["dedupe_key"]).status == "reserved"
    submitted = submissions.mark_submitted("parent-real-turn", claimed, attempt_id="parent-real-attempt",
                                           provider_call_id="original-provider-call")
    assert submitted == (claimed[0].guidance_id,)
    assert "safe forwarded request" in json.dumps(_completed_turn_native_messages(params, None, agent=case.agent))
    acknowledgements = case.agent.conversation_store.guidance.acknowledgements
    with pytest.raises(DataCorruptionError, match="turn mismatch"):
        acknowledgements.consume_submitted("foreign-turn", claimed, provider_call_id="original-provider-call")
    consumed = acknowledgements.consume_submitted("parent-real-turn", claimed,
        provider_call_id="original-provider-call", consuming_task_id=case.parent_task_id)
    assert consumed == submitted
    assert case.agent.conversation_store.guidance.receipt(route["dedupe_key"]).status == "consumed"
    assert "safe forwarded request" in json.dumps(_completed_turn_native_messages(params, None, agent=case.agent))
    params.attempt_id = "another-execution"
    assert "safe forwarded request" not in json.dumps(_completed_turn_native_messages(params, None, agent=case.agent))
