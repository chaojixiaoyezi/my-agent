from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.guidance import (
    acknowledge_injected_turn_input,
    inject_pending_guidance,
    mark_injected_turn_input_submitted,
)
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.common.audit_activation import (
    AUDIT_SOURCE_ID_ATTR,
    AUDIT_SOURCE_OWNER_HOME_ATTR,
    AUDIT_SOURCE_WATCH_ID_ATTR,
    AUDIT_SOURCE_WORKER_ATTR,
    AUDIT_SOURCE_WORKER_KEY_ATTR,
    audit_source_worker_key,
)
from agent_py_agent.agent.concurrency.interrupt import (
    is_interrupted,
    register_interrupt_callback,
    register_interruptible,
)
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_REQUEST_ID_ATTR,
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
)
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.conversation.runtime import (
    BackgroundRunRequest,
    _background_delivery_decision,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    execute_gateway_conversation_control,
    steer_active_conversation_if_running,
)
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.io import write_json_file
from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path, gateway_paths
from agent_py_agent.agent.gateway_parts.request_execution import (
    _GatewayActiveTurnTransition,
    _GatewayAskRunContext,
    _GatewayConversationContext,
    _GatewayTaskBindingWriter,
    _handle_gateway_request,
    _persist_gateway_assistant_result,
)
from agent_py_agent.agent.gateway_parts.request_worker import _resolve_request_agent
from agent_py_agent.agent.ingestion import source_worker
from agent_py_agent.agent.ingestion.watch_state import (
    load_state,
    new_state,
    persist_state,
    state_dir,
    watch_id_for,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.models import TaskStatus
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


def _request(request_id: str, *, user: str = "u-1", conversation_id: str = "c-1") -> dict:
    return {
        "id": request_id,
        "request_id": request_id,
        "kind": "ask",
        "goal": "整理今天的资料",
        "created_at": time.time() - 30,
        "lease_started_at": time.time() - 20,
        "status": "processing",
        "user_id": user,
        "metadata": {"user_id": user, "channel": "feishu"},
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": conversation_id,
            "channel_user_id": user,
            "canonical_user_id": user,
        },
    }


def _scope(user: str = "u-1", conversation_id: str = "c-1") -> GatewayControlScope:
    return GatewayControlScope(user, "feishu", conversation_id)


def _command(text: str):
    command = parse_conversation_control(text)
    assert command is not None
    return command


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _bind_durable_task(
    agent: SimpleAgent,
    task_id: str,
    *,
    user: str = "u-1",
    conversation_id: str = "c-1",
    goal: str = "整理持久后台资料",
    task_path: str = "",
):
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": user,
            "channel": "feishu",
            "channel_conversation_id": conversation_id,
            "channel_user_id": user,
            "now": time.time() - 40,
        }
    )
    link = agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": task_id,
            "goal": goal,
            "status": "active",
            "task_path": task_path,
            "now": time.time() - 30,
        }
    )
    return thread, link


def test_btw_targets_only_current_request(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    write_json_file(paths.processing / "req-2.json", _request("req-2", user="u-2"))

    scope = GatewayControlScope(
        "u-1",
        "feishu",
        "c-1",
        metadata={"message_id": "om-btw-1"},
    )
    result = execute_gateway_conversation_control(agent, paths, _command("/btw 先核对来源"), scope)

    assert result.ok is True
    assert result.request_id == "req-1"
    assert agent.conversation_store.pending_guidance("request", "req-1")[0].message == "先核对来源"
    assert agent.conversation_store.pending_guidance("request", "req-2") == []


def test_active_turn_guidance_replay_is_persistent_and_exactly_once(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "req-idempotent.json"
    write_json_file(request_path, _request("req-idempotent"))
    scope = GatewayControlScope(
        "u-1",
        "feishu",
        "c-1",
        metadata={
            "message_id": "channel-steer-idempotent",
            "expected_turn_id": "req-idempotent",
        },
    )

    first = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 保持同一回合并只保存一次"),
        scope,
    )
    request_path.unlink()
    paths.terminal.mkdir(parents=True, exist_ok=True)
    write_json_file(
        paths.terminal / "req-idempotent.json",
        {**_request("req-idempotent"), "status": "done", "turn_phase": "closed"},
    )
    replay_after_turn_disappeared = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 保持同一回合并只保存一次"),
        scope,
    )

    thread = agent.conversation_store.resolve_thread(
        channel="feishu",
        channel_conversation_id="c-1",
        channel_user_id="u-1",
    )
    assert thread is not None
    rows = agent.conversation_store.recent_guidance("request", "req-idempotent", limit=0)
    assert first.delivery_status == "unknown"
    assert replay_after_turn_disappeared.delivery_status == "rejected"
    assert [row.message for row in rows] == ["保持同一回合并只保存一次"]
    assert len({row.guidance_id for row in rows}) == 1


def test_active_turn_guidance_rejects_reused_message_id_with_different_input(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-conflict.json", _request("req-conflict"))
    scope = GatewayControlScope(
        "u-1",
        "feishu",
        "c-1",
        metadata={
            "message_id": "channel-steer-conflict",
            "expected_turn_id": "req-conflict",
        },
    )

    accepted = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 第一条内容"),
        scope,
    )
    conflict = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 被错误复用 ID 的另一条内容"),
        scope,
    )

    rows = agent.conversation_store.recent_guidance("request", "req-conflict", limit=0)
    assert accepted.delivery_status == "unknown"
    assert conflict.ok is False
    assert conflict.delivery_status == "rejected"
    assert [row.message for row in rows] == ["第一条内容"]


def test_active_turn_pending_receipt_is_unknown_until_exact_turn_ends(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "req-pending.json"
    write_json_file(request_path, _request("req-pending"))
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    message_id = "channel-steer-pending"
    scope = GatewayControlScope(
        "u-1",
        "feishu",
        "c-1",
        metadata={
            "message_id": message_id,
            "expected_turn_id": "req-pending",
        },
    )
    dedupe_key = control_service.active_turn_guidance_dedupe_key(scope)
    entry = agent.conversation_store.append_guidance_once(
        {
            "target_type": "request",
            "target_id": "req-pending",
            "message": "等待原请求写完回执",
            "sender": "u-1",
            "priority": "high",
            "delivery": "current_request",
            "metadata": {
                "kind": "active_turn_user_input",
                "record_in_transcript": True,
                "thread_id": thread.thread_id,
                "channel": "feishu",
                "conversation_id": "c-1",
                "channel_message_id": message_id,
                "expected_turn_id": "req-pending",
            },
        },
        dedupe_key=dedupe_key,
    )
    while_active = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 等待原请求写完回执"),
        scope,
    )
    request_path.unlink()
    paths.terminal.mkdir(parents=True, exist_ok=True)
    write_json_file(
        paths.terminal / "req-pending.json",
        {**_request("req-pending"), "status": "done", "turn_phase": "closed"},
    )
    after_turn = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 等待原请求写完回执"),
        scope,
    )

    assert while_active.delivery_status == "unknown"
    assert after_turn.delivery_status == "rejected"
    receipt = agent.conversation_store.guidance_once_receipt(dedupe_key)
    assert receipt is not None and receipt.status == "rejected"
    assert agent.conversation_store.pending_guidance("request", "req-pending") == []
    assert entry.guidance_id == receipt.entry.guidance_id


def test_pending_receipt_uses_its_exact_live_turn_when_global_projection_is_ambiguous(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-exact-a.json", _request("req-exact-a"))
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    scope = GatewayControlScope(
        "u-1",
        "feishu",
        "c-1",
        metadata={"message_id": "msg-exact-a", "expected_turn_id": "req-exact-a"},
    )
    dedupe_key = control_service.active_turn_guidance_dedupe_key(scope)
    agent.conversation_store.append_guidance_once(
        {
            "target_type": "request",
            "target_id": "req-exact-a",
            "message": "只能留在 A",
            "sender": "u-1",
            "metadata": {
                "kind": "active_turn_user_input",
                "thread_id": thread.thread_id,
                "channel": "feishu",
                "conversation_id": "c-1",
                "channel_message_id": "msg-exact-a",
                "expected_turn_id": "req-exact-a",
            },
        },
        dedupe_key=dedupe_key,
    )
    # A recovery conflict makes the global active projection ambiguous, but the receipt already
    # owns exact turn A and must not be rejected or rebound to B.
    write_json_file(paths.processing / "req-exact-b.json", _request("req-exact-b"))

    replay = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 只能留在 A"),
        scope,
    )

    receipt = agent.conversation_store.guidance_once_receipt(dedupe_key)
    assert replay.delivery_status == "unknown"
    assert replay.request_id == "req-exact-a"
    assert receipt is not None and receipt.status == "pending"


def test_ordinary_input_steers_only_the_matching_active_conversation(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    write_json_file(paths.processing / "req-2.json", _request("req-2", user="u-2"))

    result = steer_active_conversation_if_running(
        agent,
        paths,
        message="顺便回答一句，原任务继续",
        scope=GatewayControlScope(
            "u-1",
            "feishu",
            "c-1",
            metadata={"message_id": "om-ordinary-1"},
        ),
    )
    missing = steer_active_conversation_if_running(
        agent,
        paths,
        message="另一个会话的消息",
        scope=GatewayControlScope("u-1", "feishu", "c-missing"),
    )

    assert result is not None and result.ok is True
    assert result.request_id == "req-1"
    pending = agent.conversation_store.pending_guidance("request", "req-1")
    assert [item.message for item in pending] == ["顺便回答一句，原任务继续"]
    assert pending[0].metadata["channel_message_id"] == "om-ordinary-1"
    assert agent.conversation_store.pending_guidance("request", "req-2") == []
    assert missing is None

    for path in paths.processing.glob("*.json"):
        path.unlink()
    _bind_durable_task(agent, "task-background-only")
    no_live_turn = steer_active_conversation_if_running(
        agent,
        paths,
        message="普通聊天仍要有自己的回复",
        scope=_scope(),
    )
    assert no_live_turn is None


def test_ordinary_input_follows_only_the_task_linked_to_the_live_turn(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path / "linked",
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    thread, _link = _bind_durable_task(agent, "task-linked")
    payload = _request("req-live")
    payload["conversation_runtime"] = {
        "thread_id": thread.thread_id,
        "task_id": "task-linked",
        "task_path": "",
    }
    write_json_file(paths.processing / "req-live.json", payload)

    linked = steer_active_conversation_if_running(
        agent,
        paths,
        message="把新要求应用到当前工作",
        scope=_scope(),
    )

    assert linked is not None and linked.ok is True
    assert linked.request_id == "req-live"
    assert [
        item.message for item in agent.conversation_store.pending_guidance("task", "task-linked")
    ] == ["把新要求应用到当前工作"]
    assert agent.conversation_store.pending_guidance("request", "req-live") == []

    other_agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path / "unrelated",
    )
    other_paths = gateway_paths(other_agent)
    other_paths.processing.mkdir(parents=True, exist_ok=True)
    _bind_durable_task(other_agent, "task-unrelated")
    write_json_file(other_paths.processing / "req-chat.json", _request("req-chat"))

    unlinked = steer_active_conversation_if_running(
        other_agent,
        other_paths,
        message="这是当前聊天的新消息",
        scope=_scope(),
    )

    assert unlinked is not None and unlinked.ok is True
    assert unlinked.request_id == "req-chat"
    assert [
        item.message
        for item in other_agent.conversation_store.pending_guidance("request", "req-chat")
    ] == ["这是当前聊天的新消息"]
    assert other_agent.conversation_store.pending_guidance("task", "task-unrelated") == []


def test_ordinary_input_guidance_stays_inside_the_authenticated_owner(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            gateway_per_user_owner_scoping=True,
        ),
        tmp_path / "base",
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    alice_request = _request("req-alice", user="alice", conversation_id="chat-alice")
    bob_request = _request("req-bob", user="bob", conversation_id="chat-bob")
    write_json_file(paths.processing / "req-alice.json", alice_request)
    write_json_file(paths.processing / "req-bob.json", bob_request)

    result = steer_active_conversation_if_running(
        agent,
        paths,
        message="只属于 Alice 的新消息",
        scope=GatewayControlScope("alice", "feishu", "chat-alice"),
    )
    alice = _resolve_request_agent(agent, alice_request)
    bob = _resolve_request_agent(agent, bob_request)

    assert result is not None and result.ok is True
    assert [
        item.message for item in alice.conversation_store.pending_guidance("request", "req-alice")
    ] == ["只属于 Alice 的新消息"]
    assert bob.conversation_store.pending_guidance("request", "req-alice") == []
    assert agent.conversation_store.pending_guidance("request", "req-alice") == []


def test_gateway_cli_status_uses_the_same_owner_scope_as_gateway_ask(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            gateway_per_user_owner_scoping=True,
        ),
        tmp_path / "base",
    )
    paths = gateway_paths(agent)
    payload = {
        "user_id": "alice",
        "metadata": {"user_id": "alice", "channel": "gateway-cli"},
        "conversation": {
            "channel": "gateway-cli",
            "channel_conversation_id": "default",
            "channel_user_id": "alice",
            "canonical_user_id": "alice",
        },
    }
    owner_agent = _resolve_request_agent(agent, payload)
    thread = owner_agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "alice",
            "channel": "gateway-cli",
            "channel_conversation_id": "default",
            "channel_user_id": "alice",
        }
    )
    owner_agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-alice",
            "goal": "持续检查",
            "status": "active",
            "work_kind": "audit",
            "work_name": "Alice巡检",
            "duration_seconds": 3600,
            "cancellation_scope": "detached",
        }
    )

    status = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/status"),
        GatewayControlScope("alice", "gateway-cli", "default"),
    )
    cleared = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit Alice巡检 clear"),
        GatewayControlScope("alice", "gateway-cli", "default"),
    )
    link = next(
        item
        for item in agent.conversation_store.task_links(thread.thread_id)
        if item.task_id == "audit-alice"
    )

    assert status.ok is True
    assert "Alice巡检" in status.message
    assert cleared.ok is True
    assert link.status == "cancelled"
    assert owner_agent is agent
    assert (
        agent.conversation_store.resolve_thread(
            channel="gateway-cli",
            channel_conversation_id="default",
            channel_user_id="alice",
        )
        is not None
    )


def test_btw_becomes_one_thread_user_message_after_model_accepts_it(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    scope = GatewayControlScope(
        "u-1",
        "feishu",
        "c-1",
        metadata={"message_id": "om-btw-accepted"},
    )
    result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 改为先验证数据库迁移"),
        scope,
    )
    params = RunParams(request_id="req-1", run_id="req-1", task_id="req-1")
    params.tool_protocol_snapshot = make_test_protocol_snapshot(
        run_id="req-1",
        source_protocol="native",
    )
    params.live_archive_state = {}
    params.tool_context = []
    params.runtime_injections = []
    params.active_turn_user_inputs = []

    assert result.ok is True
    assert inject_pending_guidance(agent, params) is True
    assert mark_injected_turn_input_submitted(
        agent,
        params,
        provider_call_id="provider-call-btw",
    ) == 1
    assert acknowledge_injected_turn_input(agent, params) == 1
    assert acknowledge_injected_turn_input(agent, params) == 0

    thread = agent.conversation_store.resolve_thread(
        channel="feishu",
        channel_conversation_id="c-1",
        channel_user_id="u-1",
    )
    rows = agent.conversation_store.recent_messages(thread.thread_id, limit=0)
    assert [(row.role, row.content) for row in rows] == [("user", "改为先验证数据库迁移")]
    assert rows[0].channel_message_id == "om-btw-accepted"


def test_provider_ack_consumes_btw_after_exact_turn_enters_closing(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_id = "req-stop-after-provider"
    attempt_id = "attempt-stop-after-provider"
    request_path = paths.processing / f"{request_id}.json"
    request = _request(request_id)
    request["execution_attempt_id"] = attempt_id
    request["turn_phase"] = "open"
    write_json_file(request_path, request)
    result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 先核对已进入模型的这句"),
        GatewayControlScope(
            "u-1",
            "feishu",
            "c-1",
            metadata={"message_id": "om-btw-stop-race"},
        ),
    )
    params = RunParams(
        request_id=request_id,
        run_id=request_id,
        task_id=request_id,
        attempt_id=attempt_id,
    )
    params.tool_protocol_snapshot = make_test_protocol_snapshot(
        run_id=request_id,
        source_protocol="native",
    )
    params.live_archive_state = {}
    params.tool_context = []
    params.runtime_injections = []
    params.active_turn_user_inputs = []
    params.active_turn_transition_callback = _GatewayActiveTurnTransition(
        request_path,
        request_id,
        attempt_id,
    )

    assert result.ok is True
    assert inject_pending_guidance(agent, params) is True
    assert mark_injected_turn_input_submitted(
        agent,
        params,
        provider_call_id="provider-call-before-stop",
    ) == 1
    closing = dict(request)
    closing.update({"turn_phase": "closing", "cancel_requested": True})
    write_json_file(request_path, closing)

    assert acknowledge_injected_turn_input(agent, params) == 1
    assert agent.conversation_store.pending_guidance("request", request_id) == []


def test_goal_lifecycle_is_persistent_and_conversation_scoped(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)

    created = execute_gateway_conversation_control(
        agent, paths, _command("/goal 连续整理七天资料"), _scope()
    )
    viewed = execute_gateway_conversation_control(agent, paths, _command("/goal"), _scope())
    other_user = execute_gateway_conversation_control(
        agent, paths, _command("/goal"), _scope(user="u-2", conversation_id="c-2")
    )

    assert created.ok is True
    assert created.request_id.startswith("goal-task-")
    assert "连续整理七天资料" in viewed.message
    assert "运行中" in viewed.message
    assert other_user.message == "当前没有持续目标。"
    thread = agent.conversation_store.resolve_thread(
        channel="feishu", channel_conversation_id="c-1", channel_user_id="u-1"
    )
    assert thread is not None
    goal = agent.conversation_store.load_goal(thread.thread_id)
    assert goal is not None and goal.task_id == created.request_id and goal.status == "active"
    assert any(
        wake.reason == "thread_goal_continue" and wake.root_task_id == goal.task_id
        for wake in agent.conversation_store.pending_wake_signals()
    )

    paused = execute_gateway_conversation_control(agent, paths, _command("/goal pause"), _scope())
    assert paused.ok is True
    assert agent.conversation_store.load_goal(thread.thread_id).status == "paused"
    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}
    assert links[goal.task_id].status == "interrupted"

    resumed = execute_gateway_conversation_control(agent, paths, _command("/goal resume"), _scope())
    assert resumed.ok is True
    assert agent.conversation_store.load_goal(thread.thread_id).status == "active"
    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}
    assert links[goal.task_id].status == "active"

    edited = execute_gateway_conversation_control(
        agent, paths, _command("/goal edit 改为连续整理十四天资料"), _scope()
    )
    assert edited.ok is True
    assert (
        agent.conversation_store.load_goal(thread.thread_id).objective == "改为连续整理十四天资料"
    )
    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}
    assert links[goal.task_id].goal == "改为连续整理十四天资料"

    cleared = execute_gateway_conversation_control(agent, paths, _command("/goal clear"), _scope())
    assert cleared.ok is True
    assert agent.conversation_store.load_goal(thread.thread_id) is None
    assert (
        execute_gateway_conversation_control(agent, paths, _command("/goal"), _scope()).message
        == "当前没有持续目标。"
    )


def test_goal_rejects_second_unfinished_goal(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    first = execute_gateway_conversation_control(
        agent, paths, _command("/goal 第一件长期工作"), _scope()
    )
    second = execute_gateway_conversation_control(
        agent, paths, _command("/goal 第二件长期工作"), _scope()
    )

    assert first.ok is True
    assert second.ok is False
    assert "已有未结束" in second.message


def test_named_goals_coexist_list_and_clear_by_exact_name(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)

    first = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/goal 7d 周报整理 整理并核对本周资料"),
        _scope(),
    )
    second = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/goal 2d 依赖升级 检查并升级依赖"),
        _scope(),
    )
    duplicate = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/goal 1d 周报整理 再建一个同名目标"),
        _scope(),
    )
    status = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/status"),
        _scope(),
    )
    window_stop = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/stop"),
        _scope(),
    )
    cleared = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/goal 周报整理 clear"),
        _scope(),
    )
    thread = agent.conversation_store.resolve_thread(
        channel="feishu",
        channel_conversation_id="c-1",
        channel_user_id="u-1",
    )
    goals = agent.conversation_store.load_goals(thread.thread_id)

    assert first.ok is True and second.ok is True
    assert duplicate.ok is False
    assert "周报整理" in status.message and "依赖升级" in status.message
    assert "Goal：" in status.message
    assert window_stop.ok is False
    assert cleared.ok is True
    assert [(goal.name, goal.status) for goal in goals] == [("依赖升级", "active")]


def test_named_audit_survives_window_stop_and_clear_targets_only_that_audit(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-task-1",
            "goal": "逐条检查来源",
            "status": "active",
            "work_kind": "audit",
            "work_name": "安全巡检",
            "duration_seconds": 3600,
            "cancellation_scope": "detached",
        }
    )
    paths.processing.mkdir(parents=True, exist_ok=True)
    payload = _request("audit-task-1")
    payload["system_task"] = {
        "kind": "audit",
        "attributes": {
            "conversation_work_name": "安全巡检",
            "conversation_cancellation_scope": "detached",
        },
    }
    write_json_file(paths.processing / "audit-task-1.json", payload)

    window_stop = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/stop"),
        _scope(),
    )
    live_link = next(
        item
        for item in agent.conversation_store.task_links(thread.thread_id)
        if item.task_id == "audit-task-1"
    )
    stopped_request = json.loads(
        (paths.processing / "audit-task-1.json").read_text(encoding="utf-8")
    )
    status = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/status"),
        _scope(),
    )
    cleared = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit 安全巡检 clear"),
        _scope(),
    )
    link = next(
        item
        for item in agent.conversation_store.task_links(thread.thread_id)
        if item.task_id == "audit-task-1"
    )

    assert window_stop.ok is True
    assert stopped_request["cancel_requested"] is True
    assert live_link.status == "active"
    assert link.status == "cancelled"
    assert "Audit：" in status.message and "安全巡检" in status.message
    assert cleared.ok is True


def test_multiple_named_audits_are_listed_and_window_stop_only_stops_foreground(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    for task_id, name in (
        ("audit-auth", "登录巡检"),
        ("audit-net", "网络巡检"),
    ):
        agent.conversation_store.bind_task(
            {
                "thread_id": thread.thread_id,
                "task_id": task_id,
                "goal": f"持续执行{name}",
                "status": "active",
                "work_kind": "audit",
                "work_name": name,
                "duration_seconds": 3600,
                "cancellation_scope": "detached",
            }
        )
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "foreground.json", _request("foreground"))

    status = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/status"),
        _scope(),
    )
    stopped = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/stop"),
        _scope(),
    )
    cleared = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit 登录巡检 clear"),
        _scope(),
    )
    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}

    assert "登录巡检" in status.message and "网络巡检" in status.message
    assert stopped.ok is True and stopped.request_id == "foreground"
    assert cleared.ok is True
    assert links["audit-auth"].status == "cancelled"
    assert links["audit-net"].status == "active"


def test_global_status_counts_audit_elapsed_from_run_start_not_prepare_time(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-elapsed",
            "goal": "检查来源",
            "status": "active",
            "work_kind": "audit",
            "work_name": "计时巡检",
            "now": 1_000.0,
            "duration_seconds": 600,
            "expires_at": 5_500.0,
            "cancellation_scope": "detached",
        }
    )
    monkeypatch.setattr(control_service.time, "time", lambda: 5_000.0)

    result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/status"),
        _scope(),
    )

    assert result.ok is True
    assert result.status is not None
    audit = next(item for item in result.status.durable_work if item.name == "计时巡检")
    assert audit.elapsed_seconds == 100.0


def test_audit_start_is_one_typed_control_action_without_model_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    observed: dict[str, object] = {}

    def provision(_current_agent: object, **kwargs: object) -> dict[str, object]:
        observed["attrs"] = dict(kwargs.get("task_attributes") or {})
        return {"ok": True, "required": 1, "ready": 1, "waiting": 0}

    monkeypatch.setattr(source_worker, "provision_published_audit_source_workers", provision)

    result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit 15m 现场巡检 按已经确认的来源持续研判"),
        _scope(),
    )
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    link = next(
        item
        for item in agent.conversation_store.task_links(thread.thread_id)
        if item.work_kind == "audit" and item.work_name == "现场巡检"
    )

    assert result.ok is True
    assert result.message == "Audit“现场巡检”已启动，1 路来源工作者均已就绪。"
    assert link.status == "active"
    assert link.duration_seconds == 15 * 60
    assert link.run_prompt == "按已经确认的来源持续研判"
    assert observed["attrs"]["conversation_task_id"] == link.task_id
    assert observed["attrs"]["conversation_request_id"] == link.task_id
    assert observed["attrs"]["audit_guarantee"] is True


def test_audit_start_reports_durable_activation_when_worker_probe_fails(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)

    def fail_probe(_current_agent: object) -> dict[str, object]:
        raise RuntimeError("probe failed")

    monkeypatch.setattr(
        source_worker,
        "provision_published_audit_source_workers",
        fail_probe,
    )

    result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit 15m 现场巡检 按已经确认的来源持续研判"),
        _scope(),
    )
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    link = next(
        item
        for item in agent.conversation_store.task_links(thread.thread_id)
        if item.work_kind == "audit" and item.work_name == "现场巡检"
    )

    assert result.ok is True
    assert result.request_id == link.task_id
    assert result.message == (
        "Audit“现场巡检”已启动；"
        "来源工作者状态暂时无法核验，系统会继续自动接管，可用 status 查看。"
    )
    assert link.status == "active"


def test_audit_help_status_and_exact_case_sensitive_selection(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    for task_id, name, pending in (
        ("audit-upper", "ABC", "正在比较两种新方案"),
        ("audit-lower", "abc", "正在检查另一份来源"),
    ):
        agent.conversation_store.bind_task(
            {
                "thread_id": thread.thread_id,
                "task_id": task_id,
                "goal": "原有生效要求" if name == "ABC" else "",
                "status": "preparing",
                "work_kind": "audit",
                "work_name": name,
                "pending_prompt": pending,
                "now": time.time() - 3600 if name == "ABC" else time.time(),
                "duration_seconds": 240 if name == "ABC" else None,
                "expires_at": time.time() + 240 if name == "ABC" else None,
                "effective_source_bindings": (
                    [
                        {
                            "source_id": "login-api",
                            "url": "https://logs.example.invalid/events",
                        },
                        {
                            "source_id": "payment-api",
                            "url": "https://billing.example.invalid/events",
                        },
                        {
                            "source_id": "host-file",
                            "url": "file:///private/logs/host.jsonl",
                        },
                    ]
                    if name == "ABC"
                    else []
                ),
                "cancellation_scope": "foreground",
            }
        )

    goal_result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/goal 1d 周报整理 整理今天的资料"),
        _scope(),
    )
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "foreground.json", _request("foreground"))
    help_result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit help"),
        _scope(),
    )
    global_status = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/status"),
        _scope(),
    )
    exact = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit ABC status"),
        _scope(),
    )
    wrong_case = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit Abc status"),
        _scope(),
    )

    assert help_result.ok is True
    assert goal_result.ok is True
    assert "/audit <名称> prepare <内容>" in help_result.message
    assert "/audit <时长> <名称> <任务内容>" in help_result.message
    assert global_status.ok is True
    assert "状态：运行中" in global_status.message
    assert "ABC" in global_status.message and "abc" in global_status.message
    assert "准备中" in global_status.message
    assert "Goal：" in global_status.message and "周报整理" in global_status.message
    assert exact.ok is True
    assert "Audit：ABC" in exact.message
    assert "原有生效要求" in exact.message
    assert "正在比较两种新方案" in exact.message
    assert "来源：3（采集中 0，排空中 0，已准备 3，异常或缺岗 0）" in exact.message
    assert "待判积压：0" in exact.message
    assert "login-api（HTTP）｜已准备，尚未采集" in exact.message
    assert "host-file（文件）｜已准备，尚未采集" in exact.message
    assert "/private/logs" not in exact.message
    assert "audit-upper" not in exact.message
    assert str(tmp_path) not in exact.message
    assert wrong_case.ok is False
    assert "没有找到" in wrong_case.message

    agent.conversation_store.update_task_status(
        {"task_id": "audit-upper", "status": "active"}
    )
    active_without_workers = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit ABC status"),
        _scope(),
    )
    assert "来源：3（采集中 0，排空中 0，已准备 3，异常或缺岗 3）" in (
        active_without_workers.message
    )
    assert active_without_workers.message.count("等待来源工作者") == 3
    assert "已持续：1小时" not in active_without_workers.message


def test_audit_status_exposes_capacity_and_quota_facts_and_exact_resume(
    tmp_path,
    monkeypatch,
) -> None:
    from agent_py_agent.agent.agent_core.orchestration.dispatch import (
        capability_auto_sweep,
    )
    from agent_py_agent.agent.gateway_parts import audit_control_service
    from agent_py_agent.agent.ingestion import source_worker

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-capacity-visible",
            "goal": "持续研判已确认来源",
            "status": "active",
            "work_kind": "audit",
            "work_name": "容量巡检",
            "now": time.time() - 300,
            "duration_seconds": 3600,
            "expires_at": time.time() + 3300,
            "effective_source_bindings": [
                {
                    "source_id": "security-events",
                    "url": "https://logs.example.invalid/events",
                }
            ],
            "cancellation_scope": "detached",
        }
    )
    monkeypatch.setattr(
        audit_control_service,
        "audit_task_source_facts",
        lambda _agent, _audit_id: [
            {
                "source_id": "security-events",
                "source_url": "https://logs.example.invalid/events",
                "state_available": True,
                "collection_active": True,
                "closed": False,
                "audit_receipt": {"pending": 42},
                "source_worker": {"state": "awaiting_operator"},
                "capacity": {
                    "ingest_records_per_second": 5.5,
                    "oldest_pending_age_seconds": 125.0,
                    "processing_throughput": {"records_per_second": 3.25},
                    "processing_latency": {
                        "p50_seconds": 12.0,
                        "p95_seconds": 34.0,
                        "p99_seconds": 56.0,
                    },
                    "capacity_alert": {"active": True},
                },
            }
        ],
    )

    status = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit 容量巡检 status"),
        _scope(),
    )

    assert status.ok is True
    assert "来源：1（采集中 1，排空中 0，已准备 0，异常或缺岗 0）" in status.message
    assert "待判积压：42" in status.message
    assert "采集 5.50 条/秒，研判 3.25 条/秒" in status.message
    assert "最老待判：2分5秒" in status.message
    assert "P50 12秒，P95 34秒，P99 56秒" in status.message
    assert "容量告警：1 路；额度暂停：1 路" in status.message
    assert "模型额度耗尽，等待管理员恢复后执行 resume" in status.message

    monkeypatch.setattr(
        audit_control_service,
        "audit_task_source_facts",
        lambda _agent, _audit_id: [
            {
                "source_id": "security-events",
                "source_url": "https://logs.example.invalid/events",
                "state_available": True,
                "collection_active": False,
                "closed": False,
                "audit_receipt": {"pending": 42},
                "source_worker": {"state": "running"},
                "capacity": {},
            }
        ],
    )
    draining_status = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit 容量巡检 status"),
        _scope(),
    )
    assert "来源：1（采集中 0，排空中 1，已准备 0，异常或缺岗 0）" in (
        draining_status.message
    )
    assert "排空中，待判 42" in draining_status.message

    resumed: list[str] = []
    supervised: list[object] = []
    monkeypatch.setattr(
        source_worker,
        "resume_quota_blocked_source_workers",
        lambda _agent, audit_id: resumed.append(audit_id) or 1,
    )
    monkeypatch.setattr(
        capability_auto_sweep,
        "supervise_stalled_orphans",
        lambda current: supervised.append(current),
    )

    resume = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit 容量巡检 resume"),
        _scope(),
    )

    assert resume.ok is True
    assert resumed == ["audit-capacity-visible"]
    assert supervised == [agent]
    assert "已恢复 1 个因额度耗尽暂停的来源工作者" in resume.message


def test_audit_status_does_not_count_a_settled_removed_source() -> None:
    from agent_py_agent.agent.gateway_parts.audit_control_service import (
        _audit_status_sources,
    )

    link = SimpleNamespace(
        effective_source_bindings=(
            {
                "source_id": "current-source",
                "url": "https://logs.example.invalid/current",
            },
        )
    )
    sources = _audit_status_sources(
        link,
        [
            {
                "source_id": "current-source",
                "source_url": "https://logs.example.invalid/current",
                "state_available": True,
                "closed": False,
                "audit_receipt": {"pending": 0},
            },
            {
                "source_id": "removed-settled-source",
                "source_url": "https://logs.example.invalid/removed-settled",
                "state_available": True,
                "closed": True,
                "audit_receipt": {"pending": 0},
            },
            {
                "source_id": "removed-draining-source",
                "source_url": "https://logs.example.invalid/removed-draining",
                "state_available": True,
                "closed": True,
                "audit_receipt": {"pending": 2},
            },
        ],
    )

    assert len(sources) == 2
    assert sources[0]["display_name"] == "current-source"
    assert sources[1]["source_id"] == "removed-draining-source"
    assert all(
        source.get("source_id") != "removed-settled-source" for source in sources
    )


def test_exact_audit_status_keeps_latest_completed_history_queryable(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-old",
            "goal": "旧一轮要求",
            "status": "completed",
            "work_kind": "audit",
            "work_name": "安全巡检",
            "now": 10.0,
            "cancellation_scope": "detached",
        }
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-latest",
            "goal": "最新一轮要求",
            "status": "completed",
            "work_kind": "audit",
            "work_name": "安全巡检",
            "now": 20.0,
            "cancellation_scope": "detached",
        }
    )

    status = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit 安全巡检 status"),
        _scope(),
    )
    clear = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit 安全巡检 clear"),
        _scope(),
    )

    assert status.ok is True
    assert "状态：completed" in status.message
    assert "最新一轮要求" in status.message
    assert "旧一轮要求" not in status.message
    assert clear.ok is False
    assert "没有找到" in clear.message


def test_source_less_audit_background_close_fails_at_deadline_and_status_is_read_only(
    tmp_path,
) -> None:
    from agent_py_agent.agent.conversation.task_promotion import (
        complete_named_audit_task_if_settled,
    )

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-duration",
            "goal": "先准备数据源",
            "status": "preparing",
            "work_kind": "audit",
            "work_name": "时长核对",
            "now": 100.0,
            "cancellation_scope": "detached",
        }
    )
    activated = agent.conversation_store.activate_audit(
        {
            "task_id": "audit-duration",
            "goal": "监测三分钟",
            "duration_seconds": 180,
            "now": 1_000.0,
        }
    )
    assert activated is not None
    assert complete_named_audit_task_if_settled(agent, "audit-duration") is True

    result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit 时长核对 status"),
        _scope(),
    )

    assert result.ok is True
    assert "状态：failed" in result.message
    assert "已持续：3分0秒" in result.message


def test_exact_audit_status_never_runs_terminal_reconciliation(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-read-only-status",
            "goal": "监测结束后等待后台排空",
            "status": "active",
            "work_kind": "audit",
            "work_name": "只读状态",
            "now": 100.0,
            "duration_seconds": 60,
            "expires_at": 160.0,
            "cancellation_scope": "detached",
        }
    )

    result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit 只读状态 status"),
        _scope(),
    )
    link = agent.conversation_store.load_task_link("audit-read-only-status")

    assert result.ok is True
    assert "状态：active" in result.message
    assert link is not None and link.status == "active"


def test_audit_status_uses_exact_user_prompt_instead_of_derived_notes(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-exact-user-prompt",
            "goal": "模型把地址写成 http://127.0.0.1.18931/pull",
            "effective_user_prompt": "读取 http://127.0.0.1:18931/pull",
            "status": "preparing",
            "work_kind": "audit",
            "work_name": "精确原文",
            "cancellation_scope": "detached",
        }
    )

    status = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit 精确原文 status"),
        _scope(),
    )

    assert status.ok is True
    assert "读取 http://127.0.0.1:18931/pull" in status.message
    assert "127.0.0.1.18931" not in status.message


def test_persisted_audit_clear_cannot_cross_authenticated_owner(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            gateway_per_user_owner_scoping=True,
        ),
        tmp_path / "base",
    )
    paths = gateway_paths(agent)
    owner_agents = {}
    for user in ("alice", "bob"):
        payload = _request(f"req-{user}", user=user, conversation_id="shared-chat-id")
        owner = _resolve_request_agent(agent, payload)
        owner_agents[user] = owner
        thread = owner.conversation_store.get_or_create_thread(
            {
                "canonical_user_id": user,
                "channel": "feishu",
                "channel_conversation_id": "shared-chat-id",
                "channel_user_id": user,
            }
        )
        owner.conversation_store.bind_task(
            {
                "thread_id": thread.thread_id,
                "task_id": f"audit-{user}",
                "goal": f"只属于 {user} 的要求",
                "status": "active",
                "work_kind": "audit",
                "work_name": "同名巡检",
                "duration_seconds": 3600,
                "cancellation_scope": "detached",
            }
        )

    cleared = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit 同名巡检 clear"),
        GatewayControlScope("alice", "feishu", "shared-chat-id"),
    )
    alice_link = owner_agents["alice"].conversation_store.load_task_link("audit-alice")
    bob_link = owner_agents["bob"].conversation_store.load_task_link("audit-bob")

    assert cleared.ok is True
    assert alice_link is not None and alice_link.status == "cancelled"
    assert bob_link is not None and bob_link.status == "active"


def test_named_audit_clear_closes_sources_and_exact_descendant_tree(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    for task_id, name in (
        ("audit-auth-tree", "登录巡检"),
        ("audit-net-tree", "网络巡检"),
    ):
        agent.conversation_store.bind_task(
            {
                "thread_id": thread.thread_id,
                "task_id": task_id,
                "goal": f"持续执行{name}",
                "status": "active",
                "work_kind": "audit",
                "work_name": name,
                "duration_seconds": 3600,
                "cancellation_scope": "detached",
            }
        )
    owner_home = agent.home_paths.owner_home_dir

    def add_watch(task_id: str, suffix: str):
        source_url = f"http://source-{suffix}.example/events"
        state = new_state(
            owner_home,
            source_url,
            {"background_harvest": 0},
            watch_id=watch_id_for(owner_home, source_url, task_id),
        )
        state.audit_guarantee = True
        state.audit_root_task_id = task_id
        persist_state(state)
        return state

    auth_watch = add_watch("audit-auth-tree", "auth")
    net_watch = add_watch("audit-net-tree", "net")
    auth_watch.totals["spool_candidates"] = 3
    persist_state(auth_watch)
    auth_worker = agent.subagents.create_run(
        goal="consume auth",
        thought="",
        plan=[],
        parent_id="audit-auth-tree",
        root_id="audit-auth-tree",
        role="worker",
    )
    # Simulate a source runner result arriving after its watch was selected
    # for clear.  Ended source states must not escape exact Audit cancellation.
    auth_worker.status = TaskStatus.BLOCKED.value
    auth_worker.attributes = {
        **dict(auth_worker.attributes or {}),
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: "auth",
        AUDIT_SOURCE_WATCH_ID_ATTR: auth_watch.watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
            "audit-auth-tree",
            auth_watch.watch_id,
        ),
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        CONVERSATION_REQUEST_ID_ATTR: "audit-auth-tree",
    }
    agent.subagents.save(auth_worker)
    investigation = agent.subagents.create_run(
        goal="investigate auth finding",
        thought="",
        plan=[],
        parent_id=auth_worker.id,
        root_id="audit-auth-tree",
        role="worker",
    )
    investigation.status = TaskStatus.RUNNING.value
    agent.subagents.save(investigation)
    net_worker = agent.subagents.create_run(
        goal="consume net",
        thought="",
        plan=[],
        parent_id="audit-net-tree",
        root_id="audit-net-tree",
        role="worker",
    )
    net_worker.status = TaskStatus.RUNNING.value
    agent.subagents.save(net_worker)
    raw_spool = state_dir(owner_home) / f"{auth_watch.watch_id}.spool.ndjson"
    raw_spool.write_text('{"record":"preserved"}\n', encoding="utf-8")
    lease = state_dir(owner_home) / f"{auth_watch.watch_id}.worker-lease.json"
    lease.write_text('{"attempt_id":"old"}', encoding="utf-8")

    cleared = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit 登录巡检 clear"),
        _scope(),
    )
    deadline = time.time() + 2.0
    while time.time() < deadline:
        statuses = {
            task.id: task.status
            for task in agent.subagents.list_runs()
        }
        if (
            statuses.get(auth_worker.id) == TaskStatus.CANCELLED.value
            and statuses.get(investigation.id) == TaskStatus.CANCELLED.value
        ):
            break
        time.sleep(0.01)

    auth_disk = load_state(owner_home, auth_watch.watch_id)
    net_disk = load_state(owner_home, net_watch.watch_id)
    assert cleared.ok is True
    assert auth_disk is not None and auth_disk.closed is True
    assert auth_disk.close_reason == "named_audit_clear"
    assert auth_disk.closed_at > 0
    assert auth_disk.close_pending_records == 3
    assert net_disk is not None and net_disk.closed is False
    assert agent.subagents.load(auth_worker.id).status == TaskStatus.CANCELLED.value
    assert agent.subagents.load(investigation.id).status == TaskStatus.CANCELLED.value
    assert agent.subagents.load(net_worker.id).status == TaskStatus.RUNNING.value
    assert not lease.exists()
    assert raw_spool.read_text(encoding="utf-8") == '{"record":"preserved"}\n'


def test_queued_named_audit_clear_is_owner_scoped(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    for request_id, user, conversation_id in (
        ("audit-alice", "alice", "chat-alice"),
        ("audit-bob", "bob", "chat-bob"),
    ):
        payload = _request(
            request_id,
            user=user,
            conversation_id=conversation_id,
        )
        payload["system_task"] = {
            "kind": "audit",
            "attributes": {
                "conversation_work_name": "安全巡检",
                "conversation_cancellation_scope": "detached",
            },
        }
        write_json_file(paths.inbox / f"{request_id}.json", payload)

    cleared = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/audit 安全巡检 clear"),
        _scope(user="alice", conversation_id="chat-alice"),
    )
    alice = json.loads((paths.inbox / "audit-alice.json").read_text(encoding="utf-8"))
    bob = json.loads((paths.inbox / "audit-bob.json").read_text(encoding="utf-8"))

    assert cleared.ok is True and cleared.request_id == "audit-alice"
    assert alice["cancel_requested"] is True
    assert bob.get("cancel_requested") is not True


def test_stop_pauses_active_goal_without_deleting_it(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    created = execute_gateway_conversation_control(
        agent, paths, _command("/goal 持续完成数据整理"), _scope()
    )
    thread = agent.conversation_store.resolve_thread(
        channel="feishu", channel_conversation_id="c-1", channel_user_id="u-1"
    )
    paths.processing.mkdir(parents=True, exist_ok=True)
    payload = _request("req-live-goal")
    payload["conversation_runtime"] = {
        "thread_id": thread.thread_id,
        "task_id": created.request_id,
        "task_path": "",
    }
    write_json_file(paths.processing / "req-live-goal.json", payload)

    stopped = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())

    goal = agent.conversation_store.load_goal(thread.thread_id)
    assert created.ok is True and stopped.ok is True
    assert goal is not None and goal.status == "paused"
    assert goal.task_id == created.request_id


def test_stop_without_live_turn_does_not_change_goal_or_task_lifecycle(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    created = execute_gateway_conversation_control(
        agent, paths, _command("/goal 持续完成数据整理"), _scope()
    )
    thread = agent.conversation_store.resolve_thread(
        channel="feishu", channel_conversation_id="c-1", channel_user_id="u-1"
    )

    stopped = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/stop"),
        _scope(),
    )

    goal = agent.conversation_store.load_goal(thread.thread_id)
    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}
    assert stopped.ok is False
    assert "没有运行中的内容" in stopped.message
    assert goal is not None and goal.status == "active"
    assert links[created.request_id].status == "active"


def test_first_work_tool_resumes_stopped_goal_in_same_workspace(tmp_path) -> None:
    from agent_py_agent.agent.conversation.goal_runtime import schedule_goal_activated_in_turn
    from agent_py_agent.agent.conversation.task_promotion import (
        promote_current_conversation_task,
    )

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    created = execute_gateway_conversation_control(
        agent, paths, _command("/goal 持续完成数据整理"), _scope()
    )
    thread = agent.conversation_store.resolve_thread(
        channel="feishu", channel_conversation_id="c-1", channel_user_id="u-1"
    )
    paths.processing.mkdir(parents=True, exist_ok=True)
    payload = _request("req-live-goal")
    payload["conversation_runtime"] = {
        "thread_id": thread.thread_id,
        "task_id": created.request_id,
        "task_path": "",
    }
    write_json_file(paths.processing / "req-live-goal.json", payload)
    stopped = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())
    attrs = {
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": created.request_id,
    }
    agent._current_run_params = RunParams(
        request_id="req-resume",
        run_id="req-resume",
        task_id="req-resume",
        source="gateway",
        task_attributes=attrs,
    )
    try:
        selected = promote_current_conversation_task(agent)
        scheduled = schedule_goal_activated_in_turn(agent, attrs)
    finally:
        del agent._current_run_params

    goal = agent.conversation_store.load_goal(thread.thread_id)
    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}
    assert stopped.ok is True
    assert selected is not None and selected.task_id == created.request_id
    assert selected.task_path == links[created.request_id].task_path
    assert goal is not None and goal.status == "active" and goal.task_id == created.request_id
    assert links[created.request_id].status == "active"
    assert scheduled is True
    assert any(
        item.reason == "thread_goal_continue" and item.root_task_id == created.request_id
        for item in agent.conversation_store.pending_wake_signals()
    )


def test_first_work_after_stop_resumes_goal_workspace_without_selection_command(tmp_path) -> None:
    from agent_py_agent.agent.conversation.task_promotion import (
        promote_current_conversation_task,
    )

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    created = execute_gateway_conversation_control(
        agent, paths, _command("/goal 持续完成数据整理"), _scope()
    )
    thread = agent.conversation_store.resolve_thread(
        channel="feishu", channel_conversation_id="c-1", channel_user_id="u-1"
    )
    task_root = tmp_path / "tasks" / "2026-07-16" / "resume-demo"
    task_root.mkdir(parents=True)
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": created.request_id,
            "goal": "持续完成数据整理",
            "task_path": str(task_root),
            "status": "active",
        }
    )
    paths.processing.mkdir(parents=True, exist_ok=True)
    payload = _request("req-live-goal")
    payload["conversation_runtime"] = {
        "thread_id": thread.thread_id,
        "task_id": created.request_id,
        "task_path": str(task_root),
    }
    write_json_file(paths.processing / "req-live-goal.json", payload)
    execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())
    attrs = {
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": created.request_id,
        "run_workspace": {
            "task_root": str(task_root),
            "output_dir": str(task_root / "output"),
            "work_dir": str(task_root / "work"),
        },
    }
    agent._current_run_params = RunParams(
        request_id="req-resume-tool",
        run_id="req-resume-tool",
        task_id="req-resume-tool",
        source="gateway",
        task_attributes=attrs,
    )
    try:
        result = promote_current_conversation_task(agent)
    finally:
        del agent._current_run_params

    goal = agent.conversation_store.load_goal(thread.thread_id)
    assert result is not None and result.task_id == created.request_id
    assert result.task_path == str(task_root)
    assert goal is not None and goal.status == "active"
    assert attrs["thread_goal_activation_pending"] is True


def test_btw_on_goal_keeps_goal_continuation_reason(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    created = execute_gateway_conversation_control(
        agent, paths, _command("/goal 持续完成数据整理"), _scope()
    )

    steered = execute_gateway_conversation_control(
        agent, paths, _command("/btw 先处理今天新增的数据"), _scope()
    )

    assert created.ok is True and steered.ok is True
    wakes = [
        item
        for item in agent.conversation_store.pending_wake_signals()
        if item.root_task_id == created.request_id
    ]
    assert wakes
    assert all(item.reason == "thread_goal_continue" for item in wakes)
    assert any(item.metadata.get("guidance_id") for item in wakes)


def test_btw_follows_durable_task_after_initial_request_finished(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread, _link = _bind_durable_task(agent, "req-background")
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-chat.json", _request("req-chat"))

    result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 最终预算控制在四百元内"),
        _scope(),
    )

    assert result.ok is True
    assert result.request_id == "req-background"
    assert agent.conversation_store.pending_guidance("request", "req-chat") == []
    guidance = agent.conversation_store.pending_guidance("task", "req-background")
    assert [item.message for item in guidance] == ["最终预算控制在四百元内"]
    wakes = agent.conversation_store.pending_wake_signals()
    assert any(
        item.thread_id == thread.thread_id
        and item.root_task_id == "req-background"
        and item.reason == "user_guidance"
        for item in wakes
    )
    deliver, reason = _background_delivery_decision(
        agent,
        BackgroundRunRequest(
            thread_id=thread.thread_id,
            task_id="req-background",
            reason="user_guidance",
        ),
    )
    assert deliver is False
    assert reason == "user_guidance_applied_internal"


def test_selected_task_is_persisted_on_the_live_gateway_request(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "req-turn.json"
    write_json_file(request_path, _request("req-turn"))
    thread, link = _bind_durable_task(agent, "task-existing")
    agent._current_run_params = RunParams(
        request_id="req-turn",
        run_id="req-turn",
        task_id="req-turn",
        task_attributes={"conversation_thread_id": thread.thread_id},
        conversation_task_binding_callback=_GatewayTaskBindingWriter(request_path, "req-turn"),
    )
    try:
        from agent_py_agent.agent.conversation.task_promotion import (
            bind_current_conversation_workspace,
        )

        selected = bind_current_conversation_workspace(agent, link.task_id)
    finally:
        del agent._current_run_params

    payload = json.loads(request_path.read_text(encoding="utf-8"))
    assert selected is not None
    assert payload["conversation_runtime"] == {
        "thread_id": thread.thread_id,
        "task_id": "task-existing",
        "task_path": selected.task_path,
    }
    assert selected.task_path


def test_selected_task_reuses_the_single_thread_history_without_guidance_copy(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    thread, link = _bind_durable_task(agent, "task-existing")
    current_message = "继续第二步，只做数据库评分、衰减和对应测试。"
    agent.conversation_store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": current_message,
            "channel": "feishu",
            "metadata": {"gateway_request_id": "req-followup-2"},
        }
    )
    attrs = {
        "conversation_thread_id": thread.thread_id,
        CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
    }
    agent._current_run_params = RunParams(
        request_id="req-followup-2",
        run_id="req-followup-2",
        task_id="req-followup-2",
        source="gateway",
        root_user_prompt=current_message,
        task_attributes=attrs,
    )
    try:
        from agent_py_agent.agent.conversation.task_promotion import (
            bind_current_conversation_workspace,
        )

        first = bind_current_conversation_workspace(agent, link.task_id)
        second = bind_current_conversation_workspace(agent, link.task_id)
    finally:
        del agent._current_run_params

    agent.conversation_store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "同一会话的下一条消息",
            "channel": "feishu",
            "metadata": {"gateway_request_id": "req-next"},
        }
    )
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        context_markdown,
    )

    background_context = context_markdown(
        agent=agent,
        store=agent.conversation_store,
        thread=thread,
        request=BackgroundRunRequest(
            thread_id=thread.thread_id,
            task_id=link.task_id,
            reason="scheduled_progress_report",
        ),
    )
    assert first is not None and second is not None
    assert attrs["conversation_task_id"] == link.task_id
    assert agent.conversation_store.recent_guidance("task", link.task_id, limit=0) == []
    assert current_message in background_context
    assert "同一会话的下一条消息" in background_context


def test_linked_live_request_controls_exact_task_and_status_turn(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            model_name="MiniMax-M2.7",
            gateway_per_user_owner_scoping=False,
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    thread, _link = _bind_durable_task(agent, "task-selected", goal="较早的总任务")
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-unrelated-newer",
            "goal": "不应被本轮控制选中的任务",
            "status": "active",
            "now": time.time() + 30,
        }
    )
    payload = _request("req-current-turn")
    payload["goal"] = "继续完成当前第五步"
    payload["conversation_runtime"] = {
        "thread_id": thread.thread_id,
        "task_id": "task-selected",
        "task_path": "",
    }
    write_json_file(paths.processing / "req-current-turn.json", payload)

    steered = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 先把当前第五步的兼容性补齐"),
        _scope(),
    )
    result = execute_gateway_conversation_control(agent, paths, _command("/status"), _scope())

    assert steered.ok is True and steered.request_id == "req-current-turn"
    assert [
        item.message for item in agent.conversation_store.pending_guidance("task", "task-selected")
    ] == ["先把当前第五步的兼容性补齐"]
    assert agent.conversation_store.pending_guidance("task", "task-unrelated-newer") == []
    assert agent.conversation_store.pending_wake_signals() == []
    assert result.request_id == "task-selected"
    assert result.status is not None
    assert result.status.task == "继续完成当前第五步"


def test_btw_keeps_same_task_across_foreground_to_background_handoff(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    thread, _link = _bind_durable_task(agent, "task-root")
    request_path = paths.processing / "req-live.json"
    payload = _request("req-live")
    payload["conversation_runtime"] = {
        "thread_id": thread.thread_id,
        "task_id": "task-root",
        "task_path": "",
    }
    write_json_file(request_path, payload)
    real_target = control_service._active_control_target

    def finish_foreground_after_target_lookup(base_agent, gateway_paths_value, scope):
        target = real_target(base_agent, gateway_paths_value, scope)
        request_path.unlink()
        return target

    monkeypatch.setattr(
        control_service,
        "_active_control_target",
        finish_foreground_after_target_lookup,
    )

    result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 继续补齐同一个任务的边界测试"),
        _scope(),
    )

    assert result.ok is True
    assert result.request_id == "task-root"
    assert [
        item.message for item in agent.conversation_store.pending_guidance("task", "task-root")
    ] == ["继续补齐同一个任务的边界测试"]
    wakes = agent.conversation_store.pending_wake_signals()
    assert len(wakes) == 1
    assert wakes[0].root_task_id == "task-root"
    assert wakes[0].reason == "user_guidance"


def test_linked_request_without_a_verifiable_path_is_not_treated_as_retired() -> None:
    linked = control_service._GatewayRequestRecord(
        None,
        {
            "id": "req-live",
            "conversation_runtime": {"task_id": "task-root"},
        },
    )

    assert control_service._linked_request_target_state(linked, "task-root") == "unavailable"


def test_stop_linked_durable_task_also_interrupts_live_turn(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    thread, _link = _bind_durable_task(agent, "task-root")
    request_path = paths.processing / "req-live.json"
    payload = _request("req-live")
    payload["conversation_runtime"] = {
        "thread_id": thread.thread_id,
        "task_id": "task-root",
        "task_path": "",
    }
    write_json_file(request_path, payload)
    ready = [threading.Event(), threading.Event()]
    observed = [threading.Event(), threading.Event()]

    def worker(index: int, request_id: str) -> None:
        with register_interruptible(f"conversation-request:{request_id}"):
            ready[index].set()
            while not is_interrupted():
                time.sleep(0.01)
            observed[index].set()

    workers = [
        threading.Thread(target=worker, args=(0, "task-root")),
        threading.Thread(target=worker, args=(1, "req-live")),
    ]
    for item in workers:
        item.start()
    assert all(item.wait(timeout=2) for item in ready)

    result = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())
    for item in workers:
        item.join(timeout=2)

    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}
    stopped_payload = json.loads(request_path.read_text(encoding="utf-8"))
    assert result.ok is True and result.request_id == "req-live"
    assert all(item.is_set() for item in observed)
    assert links["task-root"].status == "interrupted"
    assert stopped_payload["cancel_requested"] is True


def test_btw_expected_task_check_rejects_task_switch_race(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread, _link = _bind_durable_task(agent, "task-old")
    append_guidance = agent.conversation_store.append_guidance

    def append_then_switch(request):
        entry = append_guidance(request)
        agent.conversation_store.bind_task(
            {
                "thread_id": thread.thread_id,
                "task_id": "task-new",
                "goal": "用户刚刚启动的新任务",
                "status": "active",
                "now": time.time() + 10,
            }
        )
        return entry

    monkeypatch.setattr(agent.conversation_store, "append_guidance", append_then_switch)

    result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 只应用到我发送时看到的当前任务"),
        _scope(),
    )

    assert result.ok is False
    assert result.request_id == "task-old"
    assert "结束或切换" in result.message
    assert agent.conversation_store.pending_guidance("task", "task-old") == []
    assert agent.conversation_store.pending_guidance("task", "task-new") == []
    assert not any(
        item.reason == "user_guidance" for item in agent.conversation_store.pending_wake_signals()
    )


def test_status_uses_typed_facts_without_guidance_history(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            model_name="MiniMax-M2.7",
            gateway_per_user_owner_scoping=False,
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    chunk_path = gateway_chunk_path(paths, "req-1")
    chunk_path.write_text(
        '{"kind":"tool_progress","progress":{"tool":"run_command","phase":"finished","status":"localized","ok":true,"detail":"secret"}}\n',
        encoding="utf-8",
    )
    agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )

    result = execute_gateway_conversation_control(agent, paths, _command("/status"), _scope())

    assert result.ok is True
    assert "状态：运行中" in result.message
    assert "最近进展：刚完成一个执行步骤" in result.message
    assert "MiniMax-M2.7" in result.message
    assert "run_command" not in result.message
    assert "secret" not in result.message
    assert "引导" not in result.message


def test_status_follows_durable_task_after_initial_request_finished(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            model_name="MiniMax-M2.7",
            gateway_per_user_owner_scoping=False,
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    _bind_durable_task(agent, "req-background", goal="整理一周入职方案")
    agent.subagents.create_run(
        goal="整理每日安排",
        thought="先列清单",
        plan=["读取", "整理"],
        attributes={CONVERSATION_REQUEST_ID_ATTR: "req-background"},
    )

    result = execute_gateway_conversation_control(agent, paths, _command("/status"), _scope())

    assert result.ok is True
    assert result.request_id == "req-background"
    assert result.status is not None
    assert result.status.state == "running"
    assert result.status.task == "整理一周入职方案"
    assert result.status.subagent_total == 1
    assert result.status.subagent_running == 1
    assert "状态：运行中" in result.message
    assert "子代理 1" in result.message


def test_status_keeps_resumable_task_but_does_not_call_it_running_without_executor(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            model_name="MiniMax-M2.7",
            gateway_per_user_owner_scoping=False,
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    _bind_durable_task(agent, "req-resumable", goal="继续原来的代码任务")

    result = execute_gateway_conversation_control(agent, paths, _command("/status"), _scope())

    assert result.ok is True
    assert result.request_id == "req-resumable"
    assert result.status is not None
    assert result.status.state == "idle"
    assert result.status.task == "继续原来的代码任务"
    assert result.status.elapsed_seconds == 0
    assert result.status.recent_progress == ""
    assert "状态：空闲" in result.message
    assert "任务：继续原来的代码任务" in result.message


def test_completed_task_projection_with_live_claim_remains_controllable(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread, _link = _bind_durable_task(
        agent,
        "req-background",
        goal="继续同一个后台任务",
    )
    claim = agent.conversation_store.claim_background_run(
        {
            "thread_id": thread.thread_id,
            "task_id": "req-background",
            "reason": "user_guidance",
            "lease_seconds": 300,
        }
    )
    assert claim is not None
    completed = agent.conversation_store.update_task_status(
        {
            "task_id": "req-background",
            "status": "completed",
            "expected_status": "active",
        }
    )
    assert completed is not None and completed.status == "completed"

    status = execute_gateway_conversation_control(agent, paths, _command("/status"), _scope())
    steered = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 按刚补充的要求继续"),
        _scope(),
    )
    stopped = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())

    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}
    assert status.request_id == "req-background"
    assert status.status is not None and status.status.state == "running"
    assert status.status.task == "继续同一个后台任务"
    assert steered.ok is True and steered.request_id == "req-background"
    assert [
        item.message for item in agent.conversation_store.pending_guidance("task", "req-background")
    ] == ["按刚补充的要求继续"]
    assert agent.conversation_store.pending_wake_signals() == []
    assert stopped.ok is True and stopped.request_id == "req-background"
    assert links["req-background"].status == "interrupted"


def test_expired_claim_does_not_revive_completed_task_projection(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread, _link = _bind_durable_task(agent, "req-expired")
    claim = agent.conversation_store.claim_background_run(
        {
            "thread_id": thread.thread_id,
            "task_id": "req-expired",
            "reason": "user_guidance",
            "lease_seconds": 30,
            "now": time.time() - 3600,
        }
    )
    assert claim is not None
    completed = agent.conversation_store.update_task_status(
        {
            "task_id": "req-expired",
            "status": "completed",
            "expected_status": "active",
        }
    )
    assert completed is not None and completed.status == "completed"

    status = execute_gateway_conversation_control(agent, paths, _command("/status"), _scope())
    steered = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 不应进入过期任务"),
        _scope(),
    )

    assert status.request_id == ""
    assert status.status is not None and status.status.state == "idle"
    assert steered.ok is False
    assert agent.conversation_store.pending_guidance("task", "req-expired") == []


def test_control_candidates_read_one_execution_snapshot_per_thread(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    thread, first = _bind_durable_task(agent, "req-first")
    _thread, second = _bind_durable_task(agent, "req-second")
    for link in (first, second):
        assert (
            agent.conversation_store.update_task_status(
                {
                    "task_id": link.task_id,
                    "status": "completed",
                    "expected_status": "active",
                }
            )
            is not None
        )
    assert (
        agent.conversation_store.claim_background_run(
            {
                "thread_id": thread.thread_id,
                "task_id": "req-second",
                "reason": "user_guidance",
                "lease_seconds": 300,
            }
        )
        is not None
    )

    calls = {"policies": 0, "claim": 0}
    original_policies = agent.conversation_store.list_progress_policies_report
    original_claim = agent.conversation_store.load_background_run_claim_report

    def policies(*args, **kwargs):
        calls["policies"] += 1
        return original_policies(*args, **kwargs)

    def claim(*args, **kwargs):
        calls["claim"] += 1
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(agent.conversation_store, "list_progress_policies_report", policies)
    monkeypatch.setattr(agent.conversation_store, "load_background_run_claim_report", claim)

    candidates = control_service._control_active_conversation_links(
        agent.conversation_store,
        thread.thread_id,
        agent.conversation_store.task_links(thread.thread_id),
    )

    assert [item.task_id for item in candidates] == ["req-second"]
    assert calls == {"policies": 1, "claim": 1}


def test_live_claim_does_not_revive_explicitly_interrupted_task(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread, _link = _bind_durable_task(agent, "req-stopped")
    claim = agent.conversation_store.claim_background_run(
        {
            "thread_id": thread.thread_id,
            "task_id": "req-stopped",
            "reason": "user_guidance",
            "lease_seconds": 300,
        }
    )
    assert claim is not None
    interrupted = agent.conversation_store.update_task_status(
        {
            "task_id": "req-stopped",
            "status": "interrupted",
            "expected_status": "active",
        }
    )
    assert interrupted is not None and interrupted.status == "interrupted"

    status = execute_gateway_conversation_control(agent, paths, _command("/status"), _scope())
    steered = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/btw 不应进入已经停止的执行轮"),
        _scope(),
    )

    assert status.request_id == ""
    assert status.status is not None and status.status.state == "idle"
    assert steered.ok is False
    assert agent.conversation_store.pending_guidance("task", "req-stopped") == []


def test_status_reports_the_single_thread_compact_generation(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    _bind_durable_task(agent, "req-background")

    result = execute_gateway_conversation_control(agent, paths, _command("/status"), _scope())

    assert result.status is not None
    assert result.status.compact_generation == 0
    assert "上下文：尚未压缩" in result.message
    assert "聊天上下文" not in result.message
    assert "任务上下文" not in result.message


def test_context_control_reads_the_same_thread_and_auto_compact_policy(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            model_name="test-model",
            model_context_window_tokens=20_000,
            memory_compact_auto_trigger_percent=90,
            gateway_per_user_owner_scoping=False,
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    agent.conversation_store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "查看真实上下文占用",
            "channel": "feishu",
        }
    )

    result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/context"),
        _scope(),
    )
    unchanged = agent.conversation_store.load_thread(thread.thread_id)

    assert result.ok is True
    assert "模型：test-model" in result.message
    assert "20,000 tokens" in result.message
    assert "90%" in result.message
    assert "未压缩消息 1 条" in result.message
    assert unchanged is not None and unchanged.compact_generation == 0


def test_manual_compact_uses_canonical_checkpoint_lane_and_custom_instructions(tmp_path) -> None:
    prompts: list[str] = []

    class SummaryBackend:
        name = "summary-test"

        def generate(self, prompt: str, **_kwargs) -> ModelResponse:
            prompts.append(prompt)
            return ModelResponse(text="已保留的会话摘要", backend=self.name)

    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            model_context_window_tokens=20_000,
            gateway_per_user_owner_scoping=False,
        ),
        tmp_path,
    )
    agent.backend = SummaryBackend()
    paths = gateway_paths(agent)
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    for role in ("user", "assistant"):
        agent.conversation_store.append_message(
            {
                "thread_id": thread.thread_id,
                "role": role,
                "content": f"需要压缩的 {role} 消息",
                "channel": "feishu",
            }
        )

    result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/compact 优先保留未完成事项"),
        _scope(),
    )
    compacted = agent.conversation_store.load_thread(thread.thread_id)

    assert result.ok is True
    assert "Context compacted · generation 1" in result.message
    assert compacted is not None and compacted.compact_generation == 1
    assert compacted.compact_checkpoint_id
    assert "优先保留未完成事项" in prompts[0]


def test_manual_compact_rejects_a_live_turn_and_effort_never_fakes_a_setting(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-live.json", _request("req-live"))

    compact = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/compact"),
        _scope(),
    )
    effort = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/effort high"),
        _scope(),
    )
    effort_status = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/effort"),
        _scope(),
    )

    assert compact.ok is False
    assert "仍在运行" in compact.message
    assert effort.ok is False
    assert "未改变任何模型参数" in effort.message
    assert effort_status.ok is True
    assert "供应商管理推理强度" in effort_status.message


def test_stop_persists_and_signals_only_matching_request(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "req-1.json"
    write_json_file(request_path, _request("req-1"))
    ready = threading.Event()
    observed = threading.Event()

    def worker() -> None:
        with register_interruptible("conversation-request:req-1"):
            ready.set()
            while not is_interrupted():
                time.sleep(0.01)
            observed.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(timeout=2)

    result = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())
    thread.join(timeout=2)

    assert result.ok is True
    assert observed.is_set()
    payload = request_path.read_text(encoding="utf-8")
    assert '"cancel_requested": true' in payload
    assert '"control_status": "stopping"' in payload


def test_stop_expected_turn_never_switches_to_newer_live_request(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "req-new.json"
    write_json_file(request_path, _request("req-new"))
    scope = GatewayControlScope(
        "u-1",
        "feishu",
        "c-1",
        metadata={
            "message_id": "stop-old-window",
            "expected_turn_id": "req-old",
        },
    )

    result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/stop"),
        scope,
    )

    assert result.ok is False
    assert result.delivery_status == "rejected"
    assert result.request_id == "req-old"
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    assert payload.get("cancel_requested") is not True
    assert payload.get("turn_phase", "open") == "open"


def test_stop_ack_is_bounded_when_provider_transport_close_blocks(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "req-slow-close.json"
    write_json_file(request_path, _request("req-slow-close"))
    ready = threading.Event()
    release_cleanup = threading.Event()
    observed = threading.Event()

    def worker() -> None:
        with register_interruptible("conversation-request:req-slow-close"):
            with register_interrupt_callback(release_cleanup.wait):
                ready.set()
                while not is_interrupted():
                    time.sleep(0.01)
            observed.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(timeout=2)

    started = time.monotonic()
    result = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())
    elapsed = time.monotonic() - started

    assert result.ok is True
    assert elapsed < 0.5
    assert observed.wait(timeout=2)
    release_cleanup.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_stop_interrupts_active_ordinary_task_after_foreground_yield(tmp_path) -> None:
    """前台请求已结束、没有 claim/进程时，thread 的 active root 仍必须可被 /stop。"""
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread, _link = _bind_durable_task(agent, "req-waiting-children")
    agent.conversation_store.select_workspace_task(
        {"thread_id": thread.thread_id, "task_id": "req-waiting-children"}
    )

    result = execute_gateway_conversation_control(
        agent,
        paths,
        _command("/stop"),
        _scope(),
    )

    stopped = agent.conversation_store.load_task_link("req-waiting-children")
    assert result.ok is True and result.request_id == "req-waiting-children"
    assert stopped is not None and stopped.status == "interrupted"


def test_stop_interrupts_durable_task_and_cancels_only_current_children(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    thread, _link = _bind_durable_task(agent, "req-background")
    child = agent.subagents.create_run(
        goal="整理子目录",
        thought="先检查",
        plan=["读取", "整理"],
        attributes={CONVERSATION_REQUEST_ID_ATTR: "req-background"},
    )
    agent.conversation_store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": child.id,
            "goal": child.goal,
            "status": "active",
        }
    )
    agent.local_store.task_registry.register_task(
        "req-background",
        status="running",
        goal="整理持久后台资料",
    )
    ready = threading.Event()
    observed = threading.Event()

    def worker() -> None:
        with register_interruptible("conversation-request:req-background"):
            ready.set()
            while not is_interrupted():
                time.sleep(0.01)
            observed.set()

    thread_worker = threading.Thread(target=worker)
    thread_worker.start()
    assert ready.wait(timeout=2)

    result = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())
    thread_worker.join(timeout=2)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and agent.subagents.load(child.id).status != "CANCELLED":
        time.sleep(0.01)

    links = {item.task_id: item for item in agent.conversation_store.task_links(thread.thread_id)}
    assert result.ok is True
    assert result.request_id == "req-background"
    assert observed.is_set()
    assert links["req-background"].status == "interrupted"
    assert links[child.id].status == "cancelled"
    assert agent.subagents.load(child.id).status == "CANCELLED"
    assert agent.local_store.task_registry.lookup_task("req-background")["status"] == "interrupted"
    deliver, reason = _background_delivery_decision(
        agent,
        BackgroundRunRequest(thread_id=thread.thread_id, task_id="req-background"),
    )
    assert deliver is False
    assert reason == "task_interrupted"


def test_status_and_stop_follow_typed_request_lineage_to_subagents(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    child = agent.subagents.create_run(
        goal="整理子目录",
        thought="先检查",
        plan=["读取", "整理"],
        attributes={CONVERSATION_REQUEST_ID_ATTR: "req-1"},
    )

    status = execute_gateway_conversation_control(agent, paths, _command("/status"), _scope())
    stopped = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and agent.subagents.load(child.id).status != "CANCELLED":
        time.sleep(0.01)

    assert status.status is not None
    assert status.status.subagent_total == 1
    assert status.status.subagent_running == 1
    assert stopped.ok is True
    assert agent.subagents.load(child.id).status == "CANCELLED"


def test_stop_does_not_recreate_request_that_finished_during_control(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic as real_update

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "req-finished.json"
    write_json_file(request_path, _request("req-finished"))

    def finish_then_update(path, updater, **kwargs):
        path.unlink()
        return real_update(path, updater, **kwargs)

    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_service.update_json_file_atomic",
        finish_then_update,
    )

    result = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())

    assert result.ok is False
    assert "刚刚结束" in result.message
    assert not request_path.exists()


def test_interrupted_request_finishes_without_model_execution(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "req-cancelled.json"
    payload = _request("req-cancelled")
    payload["cancel_requested"] = True
    write_json_file(request_path, payload)
    monkeypatch.setattr(
        agent,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    response = _handle_gateway_request(agent, request_path)

    assert response["ok"] is True
    assert response["status"] == "interrupted"
    assert response["error_code"] == "INTERRUPTED"
    assert response["response"] == ""
    assert response["channel_delivery"]["projection_status"] == "suppressed_user_stop"


def test_interrupted_result_never_becomes_assistant_transcript(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )
    context = _GatewayAskRunContext(
        agent=agent,
        request=_request("req-stop"),
        request_path=tmp_path / "req-stop.json",
        response_path=tmp_path / "req-stop.response.json",
        request_id="req-stop",
        on_chunk=None,
    )
    result = ModelResponse(
        text="",
        backend="test",
        runtime_status="cancelled",
        runtime_reason="user_stop",
        runtime_source="conversation_control",
    )

    persisted = _persist_gateway_assistant_result(
        context,
        _GatewayConversationContext(thread_id=thread.thread_id),
        result,
    )

    assert persisted.channel_delivery["projection_status"] == "suppressed_user_stop"
    assert agent.conversation_store.recent_messages(thread.thread_id, limit=10) == []


def test_control_fails_closed_for_other_user(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-2.json", _request("req-2", user="u-2"))

    result = execute_gateway_conversation_control(
        agent, paths, _command("/stop"), _scope(user="u-1")
    )

    assert result.ok is False
    assert result.request_id == ""


def test_http_control_endpoint_returns_immediate_conversation_status(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    port = _free_port()
    server = GatewayHTTPServer(
        port,
        paths,
        params=GatewayHTTPServerParams(agent=agent),
    )
    server.start()
    try:
        body = json.dumps(
            {
                "command": "/status",
                "user_id": "u-1",
                "channel": "feishu",
                "conversation_id": "c-1",
                "metadata": {"message_id": "control-status-1"},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/control",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()

    assert payload["ok"] is True
    assert payload["kind"] == "status"
    assert payload["request_id"] == "req-1"
    assert "状态：运行中" in payload["message"]


def test_http_ask_routes_verbose_before_active_turn_guidance(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    auth = AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True))
    port = _free_port()
    server = GatewayHTTPServer(
        port,
        paths,
        params=GatewayHTTPServerParams(agent=agent, auth_middleware=auth),
    )
    server.start()
    try:
        body = json.dumps(
            {
                "kind": "ask",
                "goal": "/verbose on",
                "conversation_id": "c-1",
                "metadata": {"message_id": "control-verbose-1"},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/ask",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-User-Id": "u-1",
                "X-Channel": "feishu",
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()

    thread = agent.conversation_store.resolve_thread(
        channel="feishu",
        channel_conversation_id="c-1",
        channel_user_id="u-1",
    )
    assert payload["status"] == "control"
    assert payload["kind"] == "verbose"
    assert payload["ok"] is True
    assert thread is not None and thread.verbose_level == "on"
    assert agent.conversation_store.recent_messages(thread.thread_id, limit=10) == []
    assert agent.conversation_store.pending_guidance("request", "req-1") == []
    assert list(paths.inbox.glob("*.json")) == []


def test_http_ask_routes_stop_to_live_window_interrupt(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "req-1.json"
    write_json_file(request_path, _request("req-1"))
    ready = threading.Event()
    stopped = threading.Event()

    def worker() -> None:
        with register_interruptible("conversation-request:req-1"):
            ready.set()
            while not is_interrupted():
                time.sleep(0.01)
            stopped.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(timeout=2)
    agent.conversation_store.append_guidance(
        {
            "target_type": "request",
            "target_id": "req-1",
            "message": "尚未消费的旧引导",
            "sender": "feishu:u-1",
            "delivery": "current_request",
        }
    )
    auth = AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True))
    port = _free_port()
    server = GatewayHTTPServer(
        port,
        paths,
        params=GatewayHTTPServerParams(agent=agent, auth_middleware=auth),
    )
    server.start()
    try:
        body = json.dumps(
            {
                "kind": "ask",
                "goal": "/stop",
                "conversation_id": "c-1",
                "metadata": {
                    "message_id": "control-stop-1",
                    "expected_turn_id": "req-1",
                },
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/ask",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-User-Id": "u-1",
                "X-Channel": "feishu",
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()
    thread.join(timeout=2)

    current = json.loads(request_path.read_text(encoding="utf-8"))
    assert payload["status"] == "control"
    assert payload["kind"] == "stop"
    assert payload["request_id"] == "req-1"
    assert stopped.is_set()
    assert current["cancel_requested"] is True
    assert agent.conversation_store.pending_guidance("request", "req-1") == []


def test_http_ask_rejects_unknown_slash_without_model_or_guidance(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    auth = AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True))
    port = _free_port()
    server = GatewayHTTPServer(
        port,
        paths,
        params=GatewayHTTPServerParams(agent=agent, auth_middleware=auth),
    )
    server.start()
    try:
        body = json.dumps(
            {
                "kind": "ask",
                "goal": "/not-a-command anything",
                "conversation_id": "c-1",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/ask",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-User-Id": "u-1",
                "X-Channel": "feishu",
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()

    assert payload["status"] == "control"
    assert payload["kind"] == "unsupported"
    assert payload["ok"] is False
    assert agent.conversation_store.pending_guidance("request", "req-1") == []
    assert list(paths.inbox.glob("*.json")) == []


def test_http_audit_start_executes_control_instead_of_queueing_or_steering(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    auth = AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True))
    port = _free_port()
    server = GatewayHTTPServer(
        port,
        paths,
        params=GatewayHTTPServerParams(agent=agent, auth_middleware=auth),
    )
    server.start()
    try:
        body = json.dumps(
            {
                "kind": "ask",
                "goal": "/audit 30d 安全巡检 逐条核对这些来源",
                "conversation_id": "c-1",
                "metadata": {"message_id": "control-audit-1"},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/ask",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-User-Id": "u-1",
                "X-Channel": "feishu",
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()

    assert payload["status"] == "control"
    assert payload["disposition"] == "system_command"
    assert payload["kind"] == "audit"
    assert payload["ok"] is True
    assert "Audit“安全巡检”已启动" in payload["message"]
    assert list(paths.inbox.glob("*.json")) == []
    assert agent.conversation_store.pending_guidance("request", "req-1") == []


def test_http_ask_steers_active_turn_without_creating_a_second_request(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    auth = AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True))
    port = _free_port()
    server = GatewayHTTPServer(
        port,
        paths,
        params=GatewayHTTPServerParams(agent=agent, auth_middleware=auth),
    )
    server.start()
    try:
        body = json.dumps(
            {
                "kind": "ask",
                "goal": "先简单回答我这句，原任务继续",
                "conversation_id": "c-1",
                "metadata": {"message_id": "om-live-chat"},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/ask",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-User-Id": "u-1",
                "X-Channel": "feishu",
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()

    assert str(payload["request_id"]).startswith("gwreq-msg-")
    assert payload["target_turn_id"] == "req-1"
    assert payload["status"] == "delivery_unknown"
    assert payload["disposition"] == "active_turn_input"
    assert payload["delivery_status"] == "unknown"
    assert payload["input_state"] == "active_pending"
    assert list(paths.inbox.glob("*.json")) == []
    pending = agent.conversation_store.pending_guidance("request", "req-1")
    assert [item.message for item in pending] == ["先简单回答我这句，原任务继续"]
    assert pending[0].metadata["channel_message_id"] == "om-live-chat"


def test_promote_resumes_exact_active_sticky_task_without_shadow_link(tmp_path) -> None:
    """问题5 影子任务:每轮 /ask 派生新请求 id,精确 id 无 active link 时,
    回落线程持久化 workspace_task_id 续接原任务身份+原目录,不另建新 link。"""
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    task_dir = tmp_path / "tasks" / "req-first"
    task_dir.mkdir(parents=True, exist_ok=True)
    thread, _link = _bind_durable_task(agent, "req-first", task_path=str(task_dir))
    agent.conversation_store.select_workspace_task(
        {"thread_id": thread.thread_id, "task_id": "req-first"}
    )
    attrs = {
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": "req-second",
    }
    agent._current_run_params = RunParams(
        request_id="req-second",
        run_id="req-second",
        task_id="req-second",
        source="gateway",
        root_user_prompt="继续完成复刻任务",
        task_attributes=attrs,
    )
    try:
        from agent_py_agent.agent.conversation.task_promotion import (
            promote_current_conversation_task,
        )

        promoted = promote_current_conversation_task(agent)
    finally:
        del agent._current_run_params

    assert promoted is not None
    assert promoted.task_id == "req-first"  # 续接原任务身份,不派生新 id
    assert attrs["conversation_task_id"] == "req-first"
    assert promoted.task_path == str(task_dir)  # 同目录
    links = agent.conversation_store.task_links(thread.thread_id)
    assert [item.task_id for item in links] == ["req-first"]  # 无影子新 link
    assert str(links[0].status).strip().lower() == "active"


def test_promote_completed_sticky_task_starts_fresh_workspace(tmp_path) -> None:
    """终态(completed)sticky 任务不再吸附新消息:新消息开新任务、新目录(问题1
    真机 2026-08-09:celery 完成后 click/jinja2/requests 等新任务消息全被吸进
    celery 旧目录——新 req id 配旧 task_path)。原任务保持终态;「同一任务续做
    同目录」由工具路径保留:模型显式写入旧任务目录时 bind_current_conversation_
    workspace 走 _continue_terminal_link_as_new_execution(同 cwd 新执行代数)。"""
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    task_dir = tmp_path / "tasks" / "req-first"
    task_dir.mkdir(parents=True, exist_ok=True)
    thread, _link = _bind_durable_task(agent, "req-first", task_path=str(task_dir))
    agent.conversation_store.update_task_status(
        {"task_id": "req-first", "status": "completed", "expected_status": "active"}
    )
    agent.conversation_store.select_workspace_task(
        {"thread_id": thread.thread_id, "task_id": "req-first"}
    )
    attrs = {
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": "req-second",
    }
    agent._current_run_params = RunParams(
        request_id="req-second",
        run_id="req-second",
        task_id="req-second",
        source="gateway",
        root_user_prompt="继续把复刻任务做完",
        task_attributes=attrs,
    )
    try:
        from agent_py_agent.agent.conversation.task_promotion import (
            promote_current_conversation_task,
        )

        promoted = promote_current_conversation_task(agent)
    finally:
        del agent._current_run_params

    assert promoted is not None
    assert promoted.task_id == "req-second"  # 新执行代数
    assert promoted.task_path != str(task_dir)  # 新任务新目录,不再吸附旧目录
    links = agent.conversation_store.task_links(thread.thread_id)
    by_id = {item.task_id: item for item in links}
    assert str(by_id["req-first"].status).strip().lower() == "completed"  # 原任务保持终态
    assert str(by_id["req-second"].status).strip().lower() == "active"


def test_promote_without_sticky_link_still_binds_fresh_task(tmp_path) -> None:
    """无 sticky 目录(全新会话首轮):保持原行为,新建任务 link。"""
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    thread = agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
            "now": time.time() - 40,
        }
    )
    attrs = {
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": "req-new",
    }
    agent._current_run_params = RunParams(
        request_id="req-new",
        run_id="req-new",
        task_id="req-new",
        source="gateway",
        root_user_prompt="整理今天的资料",
        task_attributes=attrs,
    )
    try:
        from agent_py_agent.agent.conversation.task_promotion import (
            promote_current_conversation_task,
        )

        promoted = promote_current_conversation_task(agent)
    finally:
        del agent._current_run_params

    assert promoted is not None
    assert promoted.task_id == "req-new"
    assert attrs["conversation_task_id"] == "req-new"


def test_promote_blocked_by_running_policy_does_not_create_shadow_link(tmp_path) -> None:
    """旧任务仍被 progress policy 驱动执行中:promote 不建影子 link,
    由 workspace execution blocker 拦截本轮工作步骤。"""
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    task_dir = tmp_path / "tasks" / "req-first"
    task_dir.mkdir(parents=True, exist_ok=True)
    thread, _link = _bind_durable_task(agent, "req-first", task_path=str(task_dir))
    agent.conversation_store.select_workspace_task(
        {"thread_id": thread.thread_id, "task_id": "req-first"}
    )
    agent.conversation_store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "req-first",
            "interval_seconds": 60,
            "now": time.time() - 30,
        }
    )
    attrs = {
        "conversation_thread_id": thread.thread_id,
        "conversation_task_id": "req-second",
    }
    agent._current_run_params = RunParams(
        request_id="req-second",
        run_id="req-second",
        task_id="req-second",
        source="gateway",
        root_user_prompt="继续完成复刻任务",
        task_attributes=attrs,
    )
    try:
        from agent_py_agent.agent.conversation.task_promotion import (
            promote_current_conversation_task,
        )

        promoted = promote_current_conversation_task(agent)
    finally:
        del agent._current_run_params

    assert promoted is None
    links = agent.conversation_store.task_links(thread.thread_id)
    assert [item.task_id for item in links] == ["req-first"]  # 无影子 link


def test_http_ask_prepared_fallback_preserves_all_execution_options() -> None:
    from agent_py_agent.agent.gateway_parts.http_handlers import (
        _AskRequestContext,
        _build_ask_request,
        _http_idempotent_request_identity,
    )

    body = {
        "metadata": {"message_id": "msg-options"},
        "conversation_id": "session-options",
        "inject": ["遵守规范"],
        "prompt_files": ["spec.md"],
        "save": False,
        "include_prompt": True,
        "resume_context": True,
        "client_capabilities": {
            "tool_approval": True,
            "rich_transcript": True,
        },
    }
    request_id, digest = _http_idempotent_request_identity(
        body,
        goal="继续完成",
        user_id="local-agent",
        channel="chat",
    )
    routed = dict(body)
    routed["metadata"] = {
        **body["metadata"],
        "client_input_digest": digest,
        "gateway_input_request_id": request_id,
    }

    prepared = _build_ask_request(
        _AskRequestContext(routed, "继续完成", request_id, "local-agent", "chat")
    )

    assert prepared["inject"] == ["遵守规范"]
    assert prepared["prompt_files"] == ["spec.md"]
    assert prepared["save"] is False
    assert prepared["include_prompt"] is True
    assert prepared["resume_context"] is True
    assert prepared["client_capabilities"] == {
        "tool_approval": True,
        "rich_transcript": True,
    }
    changed = dict(body)
    changed["save"] = True
    _same_request_id, changed_digest = _http_idempotent_request_identity(
        changed,
        goal="继续完成",
        user_id="local-agent",
        channel="chat",
    )
    assert changed_digest != digest


def test_input_lifecycle_ignores_orphan_terminal_projections(tmp_path) -> None:
    from agent_py_agent.agent.gateway_parts.input_delivery_service import (
        _gateway_turn_lifecycle,
    )

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    request_id = "gw-orphan-projection"
    for path in (paths.responses, paths.done, paths.failed):
        path.mkdir(parents=True, exist_ok=True)
    (paths.responses / f"{request_id}.json").write_text(
        json.dumps({"id": request_id, "ok": True, "response": "orphan"}),
        encoding="utf-8",
    )
    (paths.done / f"{request_id}.json").write_text(
        json.dumps({"id": request_id, "status": "done"}),
        encoding="utf-8",
    )

    assert _gateway_turn_lifecycle(paths, request_id) == "unknown"

    paths.terminal.mkdir(parents=True, exist_ok=True)
    (paths.terminal / f"{request_id}.json").write_text(
        json.dumps(
            {
                "schema_version": "gateway_terminal_request.v1",
                "id": request_id,
                "terminal_response": {"id": request_id, "ok": True},
            }
        ),
        encoding="utf-8",
    )
    assert _gateway_turn_lifecycle(paths, request_id) == "terminal"


def test_input_queue_refuses_orphan_projection_without_canonical(tmp_path) -> None:
    import pytest

    from agent_py_agent.agent.gateway_parts.input_delivery_service import (
        gateway_input_transition,
        load_or_prepare_gateway_input_locked,
        queue_gateway_input_locked,
    )
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    request_id = "gw-orphan-queue"
    digest = "stable-input-digest"
    prepared = {
        "id": request_id,
        "kind": "ask",
        "goal": "must not overwrite orphan result",
        "metadata": {"client_input_digest": digest},
    }
    with gateway_input_transition(paths, request_id):
        receipt, created = load_or_prepare_gateway_input_locked(
            paths,
            request_id=request_id,
            client_input_digest=digest,
            client_message_id="msg-orphan",
            guidance_dedupe_key="guidance/orphan",
            prepared_request=prepared,
        )
        assert created is True
        paths.responses.mkdir(parents=True, exist_ok=True)
        (paths.responses / f"{request_id}.json").write_text(
            json.dumps({"id": request_id, "ok": True}),
            encoding="utf-8",
        )
        with pytest.raises(DataCorruptionError):
            queue_gateway_input_locked(paths, receipt)

    assert not (paths.inbox / f"{request_id}.json").exists()
