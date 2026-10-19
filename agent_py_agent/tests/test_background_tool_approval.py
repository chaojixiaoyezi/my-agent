from __future__ import annotations

import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalDecision,
    build_tool_approval_request,
)
from agent_py_agent.agent.conversation.agent_activity import BackgroundMainActivitySink
from agent_py_agent.agent.conversation.agent_control import (
    AgentControlError,
    read_agent_view,
    resolve_agent_permission,
    send_agent_guidance,
    stop_agent,
)
from agent_py_agent.agent.conversation.agent_tool_approval import list_pending_agent_tool_approvals
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.http_handlers import read_gateway_client_notices
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall


def test_child_session_cache_requires_new_decision_after_attempt_changes(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import agent_tool_approval as bridge
    from agent_py_agent.agent.conversation.background_transcript import BackgroundTranscriptSink
    from agent_py_agent.tests.test_gateway_agent_control_service import (
        _bound_agent_tree,
        _child_approval_request,
    )

    agent, _, child = _bound_agent_tree(tmp_path)
    request = _child_approval_request(child.id)
    sink = BackgroundTranscriptSink(agent, thread_id=child.agent_thread_id, task_id=child.id,
                                    event_writer=lambda *_args, **_kwargs: None)
    sink.approval_interactive = True
    published = []
    monkeypatch.setattr(bridge, "publish_agent_tool_approval", lambda *_args, **_kwargs: published.append(child.id) or object())
    monkeypatch.setattr(bridge, "wait_for_agent_tool_approval", lambda *_args, **_kwargs:
        ToolApprovalDecision(request.permission_id, "approved_session" if len(published) == 1 else "denied"))
    assert sink.request_permission(request.to_dict())["decision"] == "approved_session"
    assert sink.request_permission(request.to_dict())["decision"] == "approved"
    repo = agent.subagents.runtime_db
    row = repo.agent_run_for_run_id(child.id)
    assert repo.settle_agent_attempt(agent_run_id=row["agent_run_id"], attempt_id=child.runner_active_attempt_id)["settled"]
    agent.subagents.lifecycle.prepare_runner_attempt(child.id)
    assert sink.request_permission(request.to_dict())["decision"] == "denied"
    assert len(published) == 2


# LLM: 夹具使用真实 owner store、任务关联和 claim；只替代模型调用，不伪造审批结果或权限。
# 函数用途: 建立一个可交互后台主代理，并在测试退出时取消尚未结束的等待线程。
@pytest.fixture
def background(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    from agent_py_agent.agent.user_space.approval_mode import execute_approval_mode_operation
    execute_approval_mode_operation(agent.home_paths, "set", "ask")
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "local-agent", "channel": "chat", "owner_id": agent.home_paths.owner_id,
        "channel_conversation_id": "session-main", "channel_user_id": "local-agent",
    })
    task_id = "root-work"
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id, "goal": "执行既有工作", "status": "active"})
    store.tasks.select_workspace_task({"thread_id": thread.thread_id, "task_id": task_id})
    claim = store.claims.acquire({"thread_id": thread.thread_id, "task_id": task_id, "reason": "thread_goal_continue"})
    request = build_tool_approval_request(
        ToolCall(call_id="call-main", tool_name="controlled_exec", arguments={"apply": True, "command": ["python3", "-V"]},
                 source_protocol="native", schema_hash="sha256:test-schema", run_id=task_id, turn_id="main-turn", attempt_id="main-attempt"),
        request_id="main-request", round_number=1, call_index=1, description="运行当前任务命令",
    )
    ctx = SimpleNamespace(agent=agent, store=store, thread=thread, task_id=task_id, claim=claim, request=request,
                          scope=GatewayControlScope("local-agent", "chat", "session-main"),
                          sink=BackgroundMainActivitySink(agent, thread_id=thread.thread_id, task_id=task_id),
                          token=SimpleNamespace(cancelled=False), response={}, waiter=None)
    # 显式交互只用于原审批桥测试；产品后台默认仍禁止发布人工确认。
    ctx.sink._transcript.approval_interactive = True
    yield ctx
    ctx.token.cancelled = True
    if ctx.waiter is not None:
        ctx.waiter.join(timeout=2)
        assert not ctx.waiter.is_alive()


# LLM: 使用生产后台 control、core binder 和原 claim publisher；真实 RuntimeDB 决定运行代次，不从测试 JSON 或展示 sink 授权。
# 函数用途: 为需要会话复用的后台主代理测试建立完整原宿主来源，普通单次审批夹具保持缺参反例。
def _bind_background_authority(ctx):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.agent_core.runtime_mixin import _bind_main_agent_authority
    from agent_py_agent.agent.conversation.background_execution import _BackgroundRunControl
    from agent_py_agent.agent.conversation.claim_execution_binding import (
        ClaimExecutionBinding,
        bind_claim_execution,
    )

    control = _BackgroundRunControl("main-source", None)
    with bind_claim_execution(ClaimExecutionBinding(ctx.store.claims, ctx.thread.thread_id, ctx.claim["claim_id"], "")):
        params = _bind_main_agent_authority(ctx.agent, RunParams(request_id="main-source", run_id="main-core",
            task_id=ctx.task_id, task_attributes={"conversation_thread_id": ctx.thread.thread_id,
                "conversation_task_id": ctx.task_id}, conversation_task_binding_callback=control))
    ctx.agent._current_run_params = params
    return params


# LLM: 接收能力通过真实 notices 入口续租；等待观察 canonical pending，不以 UI 文案代替发布成功。
# 函数用途: 在测试线程用原prompt scope挂宿主已绑定参数，返回TUI精确请求，不伪造实际运行身份。
def _pending_request(ctx):
    assert callable(getattr(ctx.sink, "request_permission", None))
    read_gateway_client_notices(ctx.agent, scope=ctx.scope, after=0, interactive_approvals=True)
    params = getattr(ctx.agent, "_current_run_params", None)
    ctx.waiter = threading.Thread(target=lambda: _wait_background_approval(ctx, params))
    ctx.waiter.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        rows = list_pending_agent_tool_approvals(ctx.agent, root_task_id=ctx.task_id)
        if rows:
            return rows
        time.sleep(.01)
    raise AssertionError("后台审批未发布到会话账本")


# LLM: 只用于测试线程，参数必须来自调用方当前已绑定宿主；生产仍在自己的原模型/工具线程读取权限，不增加跨线程授权入口。
# 函数用途: 通过原current_prompt_scope保留真实运行来源，审批结束后恢复测试线程临时状态。
def _wait_background_approval(ctx, params):
    from agent_py_agent.agent.agent_core.runtime_mixin import current_prompt_scope

    with current_prompt_scope(ctx.agent, "离线审批回归", params=params):
        ctx.response.update(ctx.sink.request_permission(ctx.request.to_dict(), cancellation_token=ctx.token))


@pytest.mark.parametrize("decision", ["approved", "denied"])
def test_background_main_tui_decides_exact_call(background, decision):
    ctx = background
    rows = _pending_request(ctx)
    assert rows[0]["run_id"] == ctx.task_id
    assert rows[0]["agent_kind"] == "main"
    assert rows[0]["request"] == ctx.request.to_dict()
    assert ctx.response == {}
    result = resolve_agent_permission(ctx.agent, scope=ctx.scope, run_id=ctx.task_id, request=ctx.request.to_dict(),
                                      decision=ToolApprovalDecision(ctx.request.permission_id, decision).to_dict())
    ctx.waiter.join(timeout=2)
    assert result["ok"] is True
    assert ctx.response["decision"] == decision
    assert list_pending_agent_tool_approvals(ctx.agent, root_task_id=ctx.task_id) == []


def test_paused_goal_does_not_cancel_current_approval(background):
    ctx = background
    goal = ctx.store.goals.create({"thread_id": ctx.thread.thread_id, "task_id": ctx.task_id, "objective": "执行既有工作"})
    _pending_request(ctx)
    ctx.store.goals.update({"thread_id": ctx.thread.thread_id, "goal_id": goal.goal_id, "status": "paused"})
    assert list_pending_agent_tool_approvals(ctx.agent, root_task_id=ctx.task_id)
    resolve_agent_permission(ctx.agent, scope=ctx.scope, run_id=ctx.task_id, request=ctx.request.to_dict(),
                             decision=ToolApprovalDecision(ctx.request.permission_id, "approved").to_dict())
    ctx.waiter.join(timeout=2)
    assert ctx.response["decision"] == "approved"


def test_other_conversation_cannot_approve_background_call(background):
    ctx = background
    _pending_request(ctx)
    ctx.store.threads.get_or_create({"canonical_user_id": "local-agent", "channel": "chat", "owner_id": ctx.agent.home_paths.owner_id,
                                    "channel_conversation_id": "session-other", "channel_user_id": "local-agent"})
    with pytest.raises(AgentControlError):
        resolve_agent_permission(ctx.agent, scope=GatewayControlScope("local-agent", "chat", "session-other"),
                                 run_id=ctx.task_id, request=ctx.request.to_dict(),
                                 decision=ToolApprovalDecision(ctx.request.permission_id, "approved").to_dict())
    assert ctx.response == {}


def test_new_claim_cannot_accept_old_background_approval(background):
    ctx = background
    _pending_request(ctx)
    ctx.store.claims.finish({"thread_id": ctx.thread.thread_id, "claim_id": ctx.claim["claim_id"],
                            "task_id": ctx.task_id, "status": "cancelled"})
    successor = ctx.store.claims.acquire({"thread_id": ctx.thread.thread_id, "task_id": ctx.task_id, "reason": "thread_goal_continue"})
    assert successor["claim_id"] != ctx.claim["claim_id"]
    with pytest.raises(AgentControlError):
        resolve_agent_permission(ctx.agent, scope=ctx.scope, run_id=ctx.task_id, request=ctx.request.to_dict(),
                                 decision=ToolApprovalDecision(ctx.request.permission_id, "approved").to_dict())
    ctx.waiter.join(timeout=2)
    assert ctx.response["decision"] == "unavailable"
    assert list_pending_agent_tool_approvals(ctx.agent, root_task_id=ctx.task_id) == []


def test_background_approval_without_consumer_fails_closed(background):
    ctx = background
    assert callable(getattr(ctx.sink, "request_permission", None))
    assert ctx.sink.request_permission(ctx.request.to_dict())["decision"] == "unavailable"
    assert list_pending_agent_tool_approvals(ctx.agent, root_task_id=ctx.task_id) == []


def test_interrupted_background_approval_is_cancelled(background):
    ctx = background
    _pending_request(ctx)
    ctx.token.cancelled = True
    ctx.waiter.join(timeout=2)
    assert ctx.response["decision"] == "cancelled"
    assert list_pending_agent_tool_approvals(ctx.agent, root_task_id=ctx.task_id) == []


def test_background_approval_rejects_modified_request(background):
    ctx = background
    _pending_request(ctx)
    changed = ctx.request.to_dict()
    changed["description"] = "另一条请求"
    with pytest.raises(AgentControlError):
        resolve_agent_permission(ctx.agent, scope=ctx.scope, run_id=ctx.task_id, request=changed,
                                 decision=ToolApprovalDecision(ctx.request.permission_id, "approved").to_dict())
    assert ctx.response == {}
    assert list_pending_agent_tool_approvals(ctx.agent, root_task_id=ctx.task_id)[0]["request"] == ctx.request.to_dict()


@pytest.mark.parametrize("operation, extra", [
    (read_agent_view, {}),
    (send_agent_guidance, {"message": "继续工作", "message_id": "message-control"}),
    (stop_agent, {"operation_id": "stop-control"}),
])
def test_main_approval_does_not_open_child_control_endpoints(background, operation, extra):
    ctx = background
    with pytest.raises(AgentControlError):
        operation(ctx.agent, scope=ctx.scope, run_id=ctx.task_id, **extra)
    claim, error = ctx.store.claims.load_report(ctx.thread.thread_id)
    assert error is None
    assert claim["claim_id"] == ctx.claim["claim_id"]
    assert claim["status"] == "running"


def test_background_approval_after_claim_end_never_publishes(background):
    ctx = background
    ctx.store.claims.finish({"thread_id": ctx.thread.thread_id, "claim_id": ctx.claim["claim_id"],
                            "task_id": ctx.task_id, "status": "completed"})
    assert ctx.sink.request_permission(ctx.request.to_dict())["decision"] == "unavailable"
    assert list_pending_agent_tool_approvals(ctx.agent, root_task_id=ctx.task_id) == []


@pytest.mark.parametrize("error", [OSError("unreadable"), ValueError("invalid state")])
def test_canonical_child_read_error_cannot_fall_back_to_main_claim(background, monkeypatch, error):
    ctx = background

    def unreadable(_run_id):
        raise error

    monkeypatch.setattr(ctx.agent.subagents, "load", unreadable)
    assert ctx.sink.request_permission(ctx.request.to_dict())["decision"] == "unavailable"
    assert list_pending_agent_tool_approvals(ctx.agent, root_task_id=ctx.task_id) == []


def test_main_session_approval_cannot_cross_claim_or_cancellation(background):
    ctx = background
    _bind_background_authority(ctx)
    _pending_request(ctx)
    resolve_agent_permission(ctx.agent, scope=ctx.scope, run_id=ctx.task_id, request=ctx.request.to_dict(),
                             decision=ToolApprovalDecision(ctx.request.permission_id, "approved_session").to_dict())
    ctx.waiter.join(timeout=2)
    assert ctx.response["decision"] == "approved_session"
    assert ctx.sink.request_permission(ctx.request.to_dict())["decision"] == "approved"
    ctx.token.cancelled = True
    assert ctx.sink.request_permission(ctx.request.to_dict(), cancellation_token=ctx.token)["decision"] == "cancelled"
    ctx.token.cancelled = False
    ctx.store.claims.finish({"thread_id": ctx.thread.thread_id, "claim_id": ctx.claim["claim_id"],
                            "task_id": ctx.task_id, "status": "completed"})
    ctx.store.claims.acquire({"thread_id": ctx.thread.thread_id, "task_id": ctx.task_id, "reason": "thread_goal_continue"})
    ctx.response = {}
    _pending_request(ctx)
    assert ctx.response == {}
    resolve_agent_permission(ctx.agent, scope=ctx.scope, run_id=ctx.task_id, request=ctx.request.to_dict(),
                             decision=ToolApprovalDecision(ctx.request.permission_id, "denied").to_dict())
    ctx.waiter.join(timeout=2)
    assert ctx.response["decision"] == "denied"
