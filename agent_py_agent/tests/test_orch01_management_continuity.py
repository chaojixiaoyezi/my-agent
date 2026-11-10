"""ORCH-01：同一项已授权长期工作的定时检查沿原父任务管理直属下级；无权方只能转交请求。

全部使用真实 SimpleAgent(echo)、ConversationStore、SubAgentManager/runtime.db 与 SchedulerRepository/SchedulerService：
定时片经原 enqueue_ready_runs → claim_wake → mark_run_running 推进，RunParams 由 conversation.runtime._run_params
从真实 BackgroundRunRequest 构造；会话执行租约用原 claims.acquire + bind_claim_execution 绑定（与后台 claim 循环同一对象）。
"""

from __future__ import annotations

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
    assert payload["details"]["scope_warnings"] == ["SCHEDULED_CONTINUATION_RUN_MISMATCH"]
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
    assert payload["details"]["route"]["reason"] == "AUTHORIZED_PARENT_UNAVAILABLE"
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
