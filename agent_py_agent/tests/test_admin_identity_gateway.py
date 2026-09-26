"""Gateway 侧的 IM 管理员身份与聊天内审批。

钉住：
1. 已绑定的 IM 一对一私聊解析为 local/main；群聊、未绑定、缺私聊类型、开关关闭时不变；控制作用域与请求解析一致。
2. `/admin <密码>` 经真实 `/ask` 入口处理：回执只存 `/admin ******`，任何文件都没有明文，不写入请求队列。
3. 只有服务端核实的管理员私聊（或显式声明能力的 TUI）才开启交互审批。
4. `/progress` 只公开工具名与脱敏摘要，IM 渲染提示 /approve 与 /deny。
5. `/approve <密码>` 只批准本会话唯一的待决审批；`/deny` 不要密码；无待决、多个待决、他人会话、旧执行一律拒绝。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.manager import _render_gateway_progress
from agent_py_agent.agent.contracts.tool_approval import build_tool_approval_request
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts.control_operation_service import (
    execute_gateway_control_operation,
)
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    execute_gateway_conversation_control,
    resolve_gateway_scope_owner,
)
from agent_py_agent.agent.gateway_parts.http_handlers import (
    _read_public_progress_events,
    handle_ask,
)
from agent_py_agent.agent.gateway_parts.paths import (
    claimed_request_chunk_path,
    gateway_paths_from_root,
)
from agent_py_agent.agent.gateway_parts.permission_bridge import gateway_permission_decision_path
from agent_py_agent.agent.gateway_parts.request_execution import (
    _gateway_request_interactive_approvals,
)
from agent_py_agent.agent.gateway_parts.request_worker import _resolve_request_owner_identity
from agent_py_agent.agent.gateway_parts.stream_writer import (
    BufferedChunkStreamWriter,
    write_chunk_event,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.agent.user_space.admin_channel_identity import (
    bind_admin_channel_identity,
    find_admin_channel_identity,
    remove_admin_channel_identity,
)
from agent_py_agent.agent.user_space.admin_password import set_admin_password
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

_SECRET = "Correct-Horse-42"
_LOCAL_MAIN = OwnerIdentity.local_main()


def _agent(tmp_path: Path, *, enabled: bool = True, provider: str = "local", kind: str = "main"):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = SimpleNamespace(
        gateway_per_user_owner_scoping=True,
        admin_channel_identity_enabled=enabled,
        my_agent_owner_provider=provider,
        my_agent_owner_kind=kind,
        my_agent_owner_id="main",
    )
    return SimpleNamespace(config=config, home_paths=SimpleNamespace(root=home, owner_provider=provider, owner_kind=kind))


def _request(user: str = "ou_admin", *, chat_type: str = "p2p", channel: str = "feishu") -> dict:
    return {
        "user_id": user,
        "metadata": {"user_id": user, "channel": channel, "channel_chat_type": chat_type, "channel_chat_id": "oc_chat"},
    }


def _scope(
    user: str = "ou_admin",
    *,
    conversation: str = "oc_p2p",
    chat_type: str = "p2p",
    channel: str = "feishu",
    message_id: str = "om-control",
) -> GatewayControlScope:
    return GatewayControlScope(
        user_id=user,
        channel=channel,
        conversation_id=conversation,
        metadata={"message_id": message_id, "channel_chat_type": chat_type},
    )


def _control(agent, paths, text: str, scope: GatewayControlScope | None = None):
    command = parse_conversation_control(text, reject_unknown_slash=True)
    assert command is not None and command.valid
    return execute_gateway_conversation_control(agent, paths, command, scope or _scope())


def _files_containing(root: Path, needle: str) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file() and needle.encode("utf-8") in path.read_bytes()]


def test_bound_private_chat_resolves_to_local_main_everywhere(tmp_path):
    agent = _agent(tmp_path)
    home = agent.home_paths.root
    assert _resolve_request_owner_identity(agent, _request()) == OwnerIdentity.provider_user("feishu", "ou_admin")

    bind_admin_channel_identity(home, "feishu", "ou_admin")

    assert _resolve_request_owner_identity(agent, _request()) == _LOCAL_MAIN
    assert resolve_gateway_scope_owner(agent, _scope()) == _LOCAL_MAIN
    assert _resolve_request_owner_identity(agent, _request(chat_type="group")) == OwnerIdentity.provider_group(
        "feishu", "oc_chat"
    )
    assert resolve_gateway_scope_owner(agent, _scope(chat_type="group")) != _LOCAL_MAIN
    assert _resolve_request_owner_identity(agent, _request(chat_type="")) == OwnerIdentity.provider_user(
        "feishu", "ou_admin"
    ), "缺少显式私聊类型时不按管理员处理"
    assert _resolve_request_owner_identity(agent, _request("ou_other")) == OwnerIdentity.provider_user("feishu", "ou_other")
    assert _resolve_request_owner_identity(_agent(tmp_path, enabled=False), _request()) != _LOCAL_MAIN
    non_local = _agent(tmp_path, provider="feishu", kind="user")
    assert _resolve_request_owner_identity(non_local, _request()) == OwnerIdentity.provider_user("feishu", "ou_admin")

    remove_admin_channel_identity(home, "feishu", "ou_admin")
    assert _resolve_request_owner_identity(agent, _request()) == OwnerIdentity.provider_user("feishu", "ou_admin")


# LLM: 只模拟 HTTP handler 的读正文和回写接口；没有认证中间件时身份取正文里的 user_id/channel（与原合同一致）。
# 类用途: 驱动真实 handle_ask，把响应留在内存中供断言。
class _AskHandler:
    def __init__(self, body):
        self.body, self.headers, self.client_address, self.responses = body, {}, ("127.0.0.1", 1), []

    def _read_json(self):
        return dict(self.body)

    def _send_json(self, status, payload):
        self.responses.append((status, payload))


def _admin_ask(text: str, message_id: str = "om-admin-1") -> _AskHandler:
    return _AskHandler(
        {
            "kind": "ask",
            "prompt": text,
            "user_id": "ou_admin",
            "channel": "feishu",
            "conversation_id": "oc_p2p",
            "metadata": {"message_id": message_id, "channel": "feishu", "channel_chat_type": "p2p"},
        }
    )


def test_admin_login_through_ask_binds_and_redacts_every_persisted_copy(tmp_path):
    agent = _agent(tmp_path)
    set_admin_password(agent.home_paths.root, _SECRET)
    server = SimpleNamespace(agent=agent, paths=gateway_paths_from_root(tmp_path / "gateway"))

    handler = _admin_ask(f"/admin {_SECRET}")
    handle_ask(handler, server, lambda: "req-must-not-exist")

    status, payload = handler.responses[-1]
    assert status == 200 and payload["kind"] == "admin" and payload["ok"] is True
    assert "已验证管理员身份" in payload["message"] and _SECRET not in json.dumps(payload, ensure_ascii=False)
    assert find_admin_channel_identity(agent.home_paths.root, "feishu", "ou_admin") is not None
    receipts = list((tmp_path / "gateway" / "control_operations").glob("*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text(encoding="utf-8"))["command_text"] == "/admin ******"
    assert not (tmp_path / "gateway" / "requests" / "pending").exists(), "控制命令不进模型队列"
    assert _files_containing(tmp_path, _SECRET) == []

    replay = _admin_ask(f"/admin {_SECRET}")
    handle_ask(replay, server, lambda: "req-must-not-exist")
    assert replay.responses[-1][1]["operation_id"] == payload["operation_id"], "同一消息重投只重放回执"


def test_admin_refuses_wrong_password_group_terminal_and_disabled_scopes(tmp_path):
    agent = _agent(tmp_path)
    paths = gateway_paths_from_root(tmp_path / "gateway")
    set_admin_password(agent.home_paths.root, _SECRET)

    wrong = _control(agent, paths, "/admin not-the-password")
    assert (wrong.ok, wrong.error_code, wrong.message) == (False, "ADMIN_PASSWORD_REJECTED", "管理员身份验证未通过。")
    group = _control(agent, paths, f"/admin {_SECRET}", _scope(chat_type="group"))
    terminal = _control(agent, paths, f"/admin {_SECRET}", _scope("local-agent", channel="chat"))
    disabled = _control(_agent(tmp_path, enabled=False), paths, f"/admin {_SECRET}")
    assert {group.error_code, terminal.error_code, disabled.error_code} == {"ADMIN_IDENTITY_SCOPE_INVALID"}
    assert "立即撤回" in group.message and _SECRET not in group.message
    assert find_admin_channel_identity(agent.home_paths.root, "feishu", "ou_admin") is None

    assert "未绑定" in _control(agent, paths, "/admin status").message
    assert _control(agent, paths, f"/admin {_SECRET}").ok is True
    assert "已绑定" in _control(agent, paths, "/admin status").message
    logout = _control(agent, paths, "/admin logout")
    assert logout.ok is True and find_admin_channel_identity(agent.home_paths.root, "feishu", "ou_admin") is None


def test_server_side_interactive_approvals_only_for_bound_admin_private_chat(tmp_path):
    agent = _agent(tmp_path)
    assert _gateway_request_interactive_approvals(agent, _request()) is False
    assert _gateway_request_interactive_approvals(agent, {"client_capabilities": {"tool_approval": True}}) is True

    bind_admin_channel_identity(agent.home_paths.root, "feishu", "ou_admin")

    assert _gateway_request_interactive_approvals(agent, _request()) is True
    assert _gateway_request_interactive_approvals(agent, _request(chat_type="group")) is False
    assert _gateway_request_interactive_approvals(agent, _request("ou_other")) is False
    scoped = SimpleNamespace(
        config=agent.config,
        home_paths=SimpleNamespace(root=agent.home_paths.root, owner_provider="feishu", owner_kind="user"),
    )
    assert _gateway_request_interactive_approvals(scoped, _request()) is False, "执行 owner 必须真的是本机管理员"


def _permission(request_id: str, *, call_id: str = "call-1", command: str = "rm -r /Users/alice/private/build"):
    call = ToolCall(
        call_id=call_id,
        tool_name="run_command",
        arguments={"command": command},
        source_protocol="native",
        schema_hash="sha256:admin-approval",
        run_id="run-admin",
        turn_id=request_id,
        attempt_id="attempt-admin",
    )
    return build_tool_approval_request(
        call, request_id=request_id, round_number=1, call_index=0, description=f"run_command({command})"
    )


def test_progress_exposes_only_tool_and_redacted_summary_and_renders_hint(tmp_path):
    chunk = tmp_path / "req.chunks.jsonl"
    request = _permission("req-progress")
    write_chunk_event(chunk, {"kind": "permission_requested", "permission": request.to_dict()})

    events, cursor = _read_public_progress_events(chunk, 0, channel="feishu")

    assert cursor == 1 and len(events) == 1
    event = events[0]
    assert set(event) == {"kind", "tool", "summary"} and event["tool"] == "run_command"
    assert "/Users/alice" not in event["summary"] and "build" in event["summary"]
    text = _render_gateway_progress(event)
    assert text.startswith("代理请求：run_command(")
    assert "回复 /approve <管理员密码> 允许本次，/deny 拒绝。" in text


def _processing_request(paths, request_id: str, *, user: str = "ou_admin", conversation: str = "oc_p2p") -> Path:
    paths.processing.mkdir(parents=True, exist_ok=True)
    record = paths.processing / f"{request_id}.json"
    record.write_text(
        json.dumps(
            {
                "id": request_id,
                "request_id": request_id,
                "kind": "ask",
                "status": "processing",
                "turn_phase": "open",
                "user_id": user,
                "lease_started_at": time.time() - 5,
                "metadata": {"user_id": user, "channel": "feishu", "channel_chat_type": "p2p"},
                "conversation": {
                    "channel": "feishu",
                    "channel_conversation_id": conversation,
                    "channel_user_id": user,
                    "canonical_user_id": user,
                },
            }
        ),
        encoding="utf-8",
    )
    return record


def _start_waiting(record: Path, request_id: str, request):
    writer = BufferedChunkStreamWriter(claimed_request_chunk_path(record, request_id), interactive_approvals=True)
    result: dict[str, object] = {}
    thread = threading.Thread(target=lambda: result.update(writer.request_permission(request.to_dict())), daemon=True)
    thread.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        chunk = writer.chunk_path
        if chunk.exists() and "permission_requested" in chunk.read_text(encoding="utf-8"):
            break
        time.sleep(0.01)
    return thread, result


def test_approve_resolves_the_single_pending_approval_after_password(tmp_path):
    agent = _agent(tmp_path)
    paths = gateway_paths_from_root(tmp_path / "gateway")
    set_admin_password(agent.home_paths.root, _SECRET)
    bind_admin_channel_identity(agent.home_paths.root, "feishu", "ou_admin")
    record = _processing_request(paths, "req-approve")
    thread, result = _start_waiting(record, "req-approve", _permission("req-approve"))

    wrong = _control(agent, paths, "/approve not-the-password")
    assert (wrong.ok, wrong.error_code) == (False, "ADMIN_PASSWORD_REJECTED")
    time.sleep(0.15)
    assert thread.is_alive() and not result, "错误密码不能产生决定"

    command = parse_conversation_control(f"/approve {_SECRET}", reject_unknown_slash=True)
    receipt = execute_gateway_control_operation(
        agent, paths, command, _scope(message_id="om-approve"), command_text=f"/approve {_SECRET}"
    )
    thread.join(timeout=2.0)

    assert receipt.state == "completed" and receipt.result["ok"] is True
    assert receipt.result["request_id"] == "req-approve" and receipt.command_text == "/approve ******"
    assert "已批准本次操作：run_command(" in receipt.result["message"]
    assert not thread.is_alive() and result["decision"] == "approved"
    assert _files_containing(tmp_path, _SECRET) == []
    again = _control(agent, paths, f"/approve {_SECRET}", _scope(message_id="om-approve-2"))
    assert (again.ok, again.error_code) == (False, "APPROVAL_NOT_PENDING")


def test_deny_needs_no_password_but_approve_needs_binding(tmp_path):
    agent = _agent(tmp_path)
    paths = gateway_paths_from_root(tmp_path / "gateway")
    set_admin_password(agent.home_paths.root, _SECRET)
    record = _processing_request(paths, "req-deny")
    thread, result = _start_waiting(record, "req-deny", _permission("req-deny"))

    unbound = _control(agent, paths, f"/approve {_SECRET}")
    assert (unbound.ok, unbound.error_code) == (False, "ADMIN_IDENTITY_NOT_BOUND")
    assert not (agent.home_paths.root / "config" / "admin-password-attempts.json").exists(), "未绑定时不校验密码"

    denied = _control(agent, paths, "/deny")
    thread.join(timeout=2.0)
    assert denied.ok is True and "已拒绝本次操作" in denied.message
    assert result["decision"] == "denied"


def test_missing_ambiguous_foreign_and_stale_approvals_are_refused(tmp_path):
    agent = _agent(tmp_path)
    paths = gateway_paths_from_root(tmp_path / "gateway")
    assert _control(agent, paths, "/deny").error_code == "APPROVAL_NOT_PENDING"

    record = _processing_request(paths, "req-many")
    chunk = claimed_request_chunk_path(record, "req-many")
    first, second = _permission("req-many", call_id="call-a"), _permission("req-many", call_id="call-b")
    for request in (first, second):
        write_chunk_event(chunk, {"kind": "permission_requested", "permission": request.to_dict()})
    assert _control(agent, paths, "/deny").error_code == "APPROVAL_AMBIGUOUS"
    assert not gateway_permission_decision_path(chunk, first).exists()

    write_chunk_event(chunk, {"kind": "permission_resolved", "permission_id": first.permission_id, "decision": "denied"})
    other_conversation = _control(agent, paths, "/deny", _scope(conversation="oc_other"))
    other_user = _control(agent, paths, "/deny", _scope("ou_intruder"))
    assert other_conversation.error_code == other_user.error_code == "APPROVAL_NOT_PENDING"
    assert not gateway_permission_decision_path(chunk, second).exists()

    stale = _processing_request(paths, "req-stale", conversation="oc_stale")
    old_event = {"t": time.time() - 3600, "kind": "permission_requested", "permission": _permission("req-stale").to_dict()}
    claimed_request_chunk_path(stale, "req-stale").write_text(json.dumps(old_event) + "\n", encoding="utf-8")
    assert _control(agent, paths, "/deny", _scope(conversation="oc_stale")).error_code == "APPROVAL_NOT_PENDING"

    assert _control(agent, paths, "/deny").ok is True
    assert gateway_permission_decision_path(chunk, second).exists()


@pytest.mark.parametrize("text", ["/admin status", "/deny"])
def test_terminal_channel_is_told_to_use_the_approval_panel(tmp_path, text):
    result = _control(_agent(tmp_path), gateway_paths_from_root(tmp_path / "gateway"), text, _scope("local-agent", channel="chat"))
    assert result.ok is False and result.error_code == "ADMIN_IDENTITY_SCOPE_INVALID" and "审批面板" in result.message


# ---- 没有模型时的 /admin 指引（管理员在飞书里不用排查就知道先绑定） ----


def test_model_not_configured_in_unbound_admin_p2p_points_to_admin(tmp_path):
    from agent_py_agent.agent.gateway_parts.request_execution import _gateway_user_error
    from agent_py_agent.agent.gateway_parts.request_worker import admin_binding_hint_for_request

    agent = _agent(tmp_path)
    assert admin_binding_hint_for_request(agent, _request()) == "", "没设管理员密码时不提示"
    set_admin_password(agent.home_paths.root, _SECRET)
    hint = admin_binding_hint_for_request(agent, _request())
    assert "/admin <管理员密码>" in hint and "撤回" in hint
    message = _gateway_user_error(agent, _request(), "MODEL_NOT_CONFIGURED")
    assert message.startswith("尚未配置模型") and message.endswith(hint)
    assert _gateway_user_error(agent, _request(), "PROVIDER_CONNECTION_FAILED").find("/admin") == -1, "其它错误码不变"
    assert admin_binding_hint_for_request(agent, _request(chat_type="group")) == "", "群聊不提示"
    assert admin_binding_hint_for_request(_agent(tmp_path, enabled=False), _request()) == "", "开关关闭不提示"
    bind_admin_channel_identity(agent.home_paths.root, "feishu", "ou_admin")
    assert admin_binding_hint_for_request(agent, _request()) == "", "已绑定的私聊不再提示"
    assert admin_binding_hint_for_request(agent, _request(user="ou_other")) == hint, "只看本私聊自己的绑定"


def test_model_view_without_choices_adds_admin_hint_only_when_empty(tmp_path):
    from agent_py_agent.agent.gateway_parts.model_profile_service import _admin_hint

    agent = _agent(tmp_path)
    set_admin_password(agent.home_paths.root, _SECRET)
    empty = {"profiles": []}
    assert "/admin <管理员密码>" in _admin_hint(agent, empty, _scope())
    assert _admin_hint(agent, {"profiles": [{"id": "p1", "available": True}]}, _scope()) == "", "有可选模型时不提示"
    assert _admin_hint(agent, empty, _scope(chat_type="group")) == ""
