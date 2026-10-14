"""私人审批运输替身 + 原等待记录/签名/租约/密码库；不伪造批准，也不打开 TUI 消费者。"""
from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.contracts.tool_approval import build_tool_approval_request
from agent_py_agent.agent.conversation.agent_tool_approval import (
    _approval_path,
    list_pending_agent_tool_approvals,
)
from agent_py_agent.agent.conversation.background_transcript import BackgroundTranscriptSink
from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    handle_private_interaction,
    resolve_private_interaction,
)
from agent_py_agent.agent.session_lock.service import UnlockService
from agent_py_agent.agent.session_lock.store import SessionLockStore
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.tests.test_background_tool_approval import background

_FIXTURES = (background,)


# LLM: 替换只有原生运输，真实审批请求/签名/归属/等待全走生产路径；不启动 TUI consumer。
# 函数用途: 挂起后台主代理的真实审批并等待已持久化私人卡回执。
def _im_pending(ctx, *, actor="ou_admin", bind_admin=True):
    ctx.agent.config.feishu_session_lock_enabled = False
    if bind_admin:
        bind_admin_channel_identity(ctx.agent.home_paths.root, "feishu", actor)
    sent = []

    def send(target, card, **kwargs):
        sent.append((target, card))
        return {"message_id": "om_approval", "chat_id": "oc_private"}

    adapter = SimpleNamespace(send_control_card=send)
    ctx.agent.channel_registry.register_adapter("feishu", adapter,
        capabilities=ChannelCapabilities(proactive=True, control_cards=True))
    ctx.waiter = threading.Thread(target=lambda: ctx.response.update(
        ctx.sink.request_permission(ctx.request.to_dict(), cancellation_token=ctx.token)))
    ctx.waiter.start()
    path = _approval_path(ctx.agent, tool_approval_scope(ctx.agent, ctx.task_id), ctx.request.permission_id)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        row = read_json_object_report(path, context="test.approval").payload or {}
        if (row.get("im_control") or {}).get("delivery_status") == "sent":
            return {"reference": row["im_control"]["reference"], "decision": "approved",
                    "message_id": "om_approval", "chat_id": "oc_private"}, sent
        time.sleep(0.01)
    pytest.fail("原审批没有已发出的私人卡回执")


def test_private_card_keeps_original_waiter_without_tui_and_resolves_same_call(background):
    ctx = background
    body, sent = _im_pending(ctx)
    time.sleep(1.6)  # 已超过原 1.5 秒发现窗，没有任何 TUI consumer。
    assert ctx.waiter.is_alive() and not ctx.response
    assert len(sent) == 1 and sent[0][0] == "ou_admin"
    result = resolve_private_interaction(ctx.agent, body, channel="feishu", actor_id="ou_admin")
    assert result["ok"] and result["permission_id"] == ctx.request.permission_id
    ctx.waiter.join(2)
    assert ctx.response["decision"] == "approved"
    assert not list_pending_agent_tool_approvals(ctx.agent, root_task_id=ctx.task_id)


@pytest.mark.parametrize("depth", [1, 2])
def test_child_and_grandchild_private_approval_returns_to_exact_attempt(background, depth):
    ctx = background
    root_id = ctx.task_id
    parent_id = root_id
    for _ in range(depth):
        child = ctx.agent.subagents.create_run(goal="合成子任务", root_id=root_id, parent_id=parent_id)
        child = ctx.agent.subagents.lifecycle.prepare_runner_attempt(child.id)
        parent_id = child.id
    ctx.task_id = child.id
    ctx.request = build_tool_approval_request(ToolCall(call_id="child-private-call", tool_name="controlled_exec",
        arguments={"apply": True, "command": ["python3", "-V"]}, source_protocol="native", schema_hash="sha256:child-test",
        run_id=child.id, turn_id="child-turn", attempt_id=child.runner_active_attempt_id),
        request_id="child-private-request", round_number=1, call_index=1, description="子代理合成命令")
    ctx.sink = BackgroundTranscriptSink(ctx.agent, thread_id=child.agent_thread_id, task_id=child.id,
        request_id="child-private-request", event_writer=lambda *_args, **_kwargs: None)
    body, _ = _im_pending(ctx)
    assert resolve_private_interaction(ctx.agent, body, channel="feishu", actor_id="ou_admin")["run_id"] == child.id
    ctx.waiter.join(2)
    assert ctx.response["decision"] == "approved"
    assert not list_pending_agent_tool_approvals(ctx.agent, root_task_id=root_id)


@pytest.mark.parametrize("change", ["actor", "message", "chat", "reference"])
def test_other_actor_forwarded_card_and_tampered_reference_cannot_approve(background, change):
    ctx = background
    body, _ = _im_pending(ctx)
    actor = "ou_admin"
    if change == "actor":
        actor = "ou_other"
    elif change == "reference":
        body["reference"] = body["reference"][:-1] + ("0" if body["reference"][-1] != "0" else "1")
    else:
        body["message_id" if change == "message" else "chat_id"] = "forwarded"
    with pytest.raises(ValueError):
        resolve_private_interaction(ctx.agent, body, channel="feishu", actor_id=actor)
    assert ctx.response == {} and ctx.waiter.is_alive()


def test_new_claim_rejects_old_private_card_without_approving_next_run(background):
    ctx = background
    body, _ = _im_pending(ctx)
    ctx.store.claims.finish({"thread_id": ctx.thread.thread_id, "claim_id": ctx.claim["claim_id"],
                            "task_id": ctx.task_id, "status": "cancelled"})
    ctx.store.claims.acquire({"thread_id": ctx.thread.thread_id, "task_id": ctx.task_id, "reason": "thread_goal_continue"})
    with pytest.raises(ValueError):
        resolve_private_interaction(ctx.agent, body, channel="feishu", actor_id="ou_admin")
    ctx.waiter.join(2)
    assert ctx.response["decision"] == "unavailable"


def test_password_setup_and_idle_lock_block_decision_until_original_unlock(background):
    ctx = background
    body, _ = _im_pending(ctx)
    ctx.agent.config.feishu_session_lock_enabled = True
    ctx.agent.config.feishu_personal_idle_lock_seconds = 60
    result = resolve_private_interaction(ctx.agent, body, channel="feishu", actor_id="ou_admin")
    assert result["error_code"] == "IM_PASSWORD_SETUP_REQUIRED" and ctx.response == {}
    store = SessionLockStore(Path(ctx.agent.home_paths.root) / "session_lock" / "private_chat_locks.db")
    service = UnlockService(store, idle_limit_seconds=60)
    assert service.set_password("ou_admin", "Synthetic-Test-Password-123")[0]
    store.record_activity("ou_admin", now=time.time() - 100)
    assert resolve_private_interaction(ctx.agent, body, channel="feishu", actor_id="ou_admin")["error_code"] == "IM_UNLOCK_REQUIRED"
    assert not service.unlock("ou_admin", "wrong-test-password")
    assert ctx.response == {}
    assert service.unlock("ou_admin", "Synthetic-Test-Password-123")
    assert resolve_private_interaction(ctx.agent, body, channel="feishu", actor_id="ou_admin")["ok"]
    ctx.waiter.join(2)
    assert ctx.response["decision"] == "approved"


def test_selection_change_is_not_a_change_of_execution_authority(background):
    ctx = background
    body, _ = _im_pending(ctx)
    ctx.store.tasks.bind({"thread_id": ctx.thread.thread_id, "task_id": "other-visible-work", "goal": "其它工作", "status": "active"})
    ctx.store.tasks.select_workspace_task({"thread_id": ctx.thread.thread_id, "task_id": "other-visible-work"})
    assert resolve_private_interaction(ctx.agent, body, channel="feishu", actor_id="ou_admin")["ok"]
    ctx.waiter.join(2)
    assert ctx.response["decision"] == "approved"


def test_group_background_approval_goes_to_frozen_creator_not_latest_speaker(tmp_path):
    from agent_py_agent.agent.conversation.agent_activity import BackgroundMainActivitySink
    from agent_py_agent.agent.conversation.execution_threads import ensure_execution_thread
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
        my_agent_owner_provider="feishu", my_agent_owner_kind="group", my_agent_owner_id="oc_group"), tmp_path)
    store = agent.conversation_store
    source = store.threads.get_or_create({"channel": "feishu", "channel_user_id": "ou_creator",
        "channel_conversation_id": "oc_group", "canonical_user_id": "ou_creator", "owner_id": agent.home_paths.owner_id,
        "owner_home": str(agent.home_paths.owner_home_dir)})
    work = ensure_execution_thread(agent, source.thread_id, "group-work", kind="delegated")
    store.threads.update_atomic(source.thread_id, lambda thread: replace(thread,
        channel_bindings=tuple(replace(binding, channel_user_id="ou_other") for binding in thread.channel_bindings)))
    task_id = "group-root-work"
    store.tasks.bind({"thread_id": work.thread_id, "task_id": task_id, "goal": "合成群任务", "status": "active"})
    store.claims.acquire({"thread_id": work.thread_id, "task_id": task_id, "reason": "thread_goal_continue"})
    request = build_tool_approval_request(ToolCall(call_id="group-call", tool_name="run_command", arguments={"command": "true"},
        source_protocol="native", schema_hash="sha256:group-test", run_id=task_id, turn_id="group-turn", attempt_id="group-attempt"),
        request_id="group-request", round_number=1, call_index=1, description="合成群工作")
    ctx = SimpleNamespace(agent=agent, store=store, task_id=task_id, request=request, response={},
        token=SimpleNamespace(cancelled=False), waiter=None,
        sink=BackgroundMainActivitySink(agent, thread_id=work.thread_id, task_id=task_id))
    try:
        body, sent = _im_pending(ctx, actor="ou_creator", bind_admin=False)
        assert sent[0][0] == "ou_creator"
        with pytest.raises(ValueError):
            resolve_private_interaction(agent, body, channel="feishu", actor_id="ou_other")
        assert resolve_private_interaction(agent, body, channel="feishu", actor_id="ou_creator")["ok"]
        ctx.waiter.join(2)
        assert ctx.response["decision"] == "approved"
    finally:
        ctx.token.cancelled = True
        if ctx.waiter is not None:
            ctx.waiter.join(2)


def test_card_endpoint_requires_real_host_credential_even_with_legacy_loopback_trust():
    middleware = AuthMiddleware(None, auth_token="synthetic-host-credential")
    responses = []
    handler = SimpleNamespace(_auth_middleware=middleware, headers={"X-User-Id": "ou_admin", "X-Channel": "feishu"},
        _send_json=lambda status, payload: responses.append((status, payload)),
        _read_json=lambda: pytest.fail("无凭据时不应读取按钮引用或解析 owner"))
    handle_private_interaction(handler, None)
    assert responses == [(403, {"ok": False, "error_code": "LOCAL_CREDENTIAL_REQUIRED"})]


def test_native_callback_posts_through_real_http_route_to_original_waiter(background):
    from agent_py_agent.agent.adapter.feishu import FeishuAdapter
    from agent_py_agent.agent.adapter.private_controls import tool_approval_card_action
    from agent_py_agent.agent.auth.manager import AuthManager
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths

    ctx = background
    body, _ = _im_pending(ctx)
    middleware = AuthMiddleware(AuthManager())
    server = GatewayHTTPServer(0, gateway_paths(ctx.agent),
        params=GatewayHTTPServerParams(agent=ctx.agent, auth_middleware=middleware))
    try:
        server.start()
        port = server.server.server_address[1]
        adapter = FeishuAdapter({"my_agent_home": str(ctx.agent.home_paths.root), "gateway_port": port,
                                 "feishu_session_lock_enabled": False})
        result = tool_approval_card_action(adapter, {"operator_open_id": "ou_admin", "message_id": "om_approval",
            "chat_id": "oc_private", "value": {"kind": "my_agent_tool_approval", "reference": body["reference"], "decision": "approved"}})
        assert result["toast"]["type"] == "success", result
        ctx.waiter.join(2)
        assert ctx.response["decision"] == "approved"
    finally:
        server.stop()
