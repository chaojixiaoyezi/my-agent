"""S12b: the admin private chat's frozen host_path_display=full reaches every IM exit of that request; others keep basenames.

Exits: /ask receipt -> adapter pending record -> final reply, progress text/card drafts, adapter card steps, /progress,
the gateway writer's card label and the channel-message tool. Credential masking stays on with full paths.
"""
from __future__ import annotations

import io
import json
from email.message import Message
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.delivery import PendingGatewayReply
from agent_py_agent.agent.adapter.feishu_progress_card import render_progress_card
from agent_py_agent.agent.adapter.ingress import GatewayIngressRecord
from agent_py_agent.agent.adapter.manager import (
    _ChannelIngressCoordinator,
    _deliver_gateway_message,
    _gateway_ingress_submission,
    _pending_gateway_message,
)
from agent_py_agent.agent.adapter.progress_draft import send_gateway_draft
from agent_py_agent.agent.conversation.host_actor import HostChannelActor
from agent_py_agent.agent.delivery import (
    ChannelAdapterRegistry,
    ChannelCapabilities,
    DeliveryService,
)
from agent_py_agent.agent.delivery.execution_view import project_execution_view, step_display_text
from agent_py_agent.agent.gateway_parts import http_handlers as http
from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.tests.test_gateway_http_prepare_boundary import (  # noqa: F401
    _body,
    _submit,
    server,
)

_PATH = "/Users/alice/projects/heron/src/lexer.py"
# 形似 GitHub token 的合成值，只用来证明完整路径里的凭据片段仍被遮蔽。
_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


# 函数用途: 记录统一投递服务交给假飞书适配器的最终回复、进度正文与卡片。
class _Feishu:
    provider_idempotent_delivery = True
    provider_idempotency_window_seconds = 3600
    supports_edit_progress = True
    supports_progress_cards = True

    def __init__(self):
        self.sent, self.created = [], []

    def finalize_response(self, target, handle, message):
        self.sent.append((target, message.content))
        return True

    def send_message(self, target, message):
        self.sent.append((target, message.content))
        return True

    def start_progress_message(self, target, text, *, reply_to="", idempotency_key="", **options):
        self.created.append((target, text, options))
        return "om_progress"

    def edit_progress_message(self, message_id, text, **options):
        return True


# 函数用途: 真实 DeliveryService + 假飞书适配器。
def _service():
    registry = ChannelAdapterRegistry()
    adapter = _Feishu()
    registry.register_adapter("feishu", adapter, capabilities=ChannelCapabilities(
        text=True, reply=True, proactive=True, edit_progress=True))
    return DeliveryService(registry), adapter


# 函数用途: 构造一条待回送记录；display 为空时按旧记录形状（无该字段）构造。
def _pending(display="", **fields):
    extra = {"host_path_display": display} if display else {}
    return PendingGatewayReply("req-s12b", "feishu", "ou_admin", "om_inbound", conversation_id="feishu-p2p",
                               **fields, **extra)


# 函数用途: 在已绑定管理员、本机 full-access 的网关上受理一条飞书消息，返回 /ask 回执与请求编号。
def _admit(server, user="ou_admin", chat="p2p"):  # noqa: F811
    config = server.agent.config
    config.admin_channel_identity_enabled, config.access_mode = True, "full-access"
    bind_admin_channel_identity(server.agent.home_paths.root, "feishu", "ou_admin")
    body = _body()
    body["metadata"]["channel_chat_type"] = chat
    (status, result), request_id = _submit(server, body, channel="feishu", user_id=user)
    assert status == 202, result
    return result, request_id


@pytest.mark.parametrize(("user", "chat", "full"), [
    ("ou_admin", "p2p", True), ("ou_admin", "group", False), ("ou_other", "p2p", False),
])
def test_ask_receipt_returns_the_frozen_display_only_for_the_admin_private_chat(server, user, chat, full):  # noqa: F811
    result, _request_id = _admit(server, user, chat)
    assert result.get("host_path_display") == ("full" if full else None)
    replay, _ = _admit(server, user, chat)
    assert replay.get("host_path_display") == result.get("host_path_display"), "重放沿冻结回执，不重新判定"


@pytest.mark.parametrize(("user", "chat", "display"), [("ou_admin", "p2p", "full"), ("ou_admin", "group", "")])
def test_receipt_display_flows_into_the_durable_pending_record(server, user, chat, display):  # noqa: F811
    result, request_id = _admit(server, user, chat)
    record = GatewayIngressRecord("ing-s12b", "feishu", user, "feishu-chat", "fixture-message", "0" * 64, "合成正文",
                                  {"feishu_chat_type": chat}, state="submitted", submission=dict(result))
    assert _gateway_ingress_submission(record).host_path_display == display
    queued = []
    manager = SimpleNamespace(_reply_delivery=SimpleNamespace(enqueue=lambda row: (queued.append(row), (row, True))[1]),
                              _adapters={})
    _ChannelIngressCoordinator(manager).handoff_reply(record)
    [pending] = queued
    assert pending.request_id == request_id and pending.host_path_display == display
    restored = PendingGatewayReply.from_dict(json.loads(json.dumps(pending.to_dict())))
    assert restored.host_path_display == display
    assert _pending_gateway_message(restored).metadata["host_path_display"] == display
    legacy = pending.to_dict()
    legacy.pop("host_path_display")
    assert PendingGatewayReply.from_dict(legacy).host_path_display == "", "旧记录缺字段按 basename"


@pytest.mark.parametrize(("display", "full"), [("full", True), ("", False), ("basename", False)])
def test_adapter_final_reply_follows_the_pending_display(display, full):
    service, adapter = _service()
    pending = _pending(display)
    assert _deliver_gateway_message(service, adapter_available=True, msg=_pending_gateway_message(pending),
                                    request_id=pending.request_id, response_text=f"已改 {_PATH}")
    [(target, text)] = adapter.sent
    assert target == "ou_admin" and (_PATH in text) is full and "lexer.py" in text


def test_full_path_still_masks_a_credential_segment():
    service, adapter = _service()
    secret_path = f"/Users/alice/keys/{_TOKEN}/repo/lexer.py"
    pending = _pending("full")
    assert _deliver_gateway_message(service, adapter_available=True, msg=_pending_gateway_message(pending),
                                    request_id=pending.request_id, response_text=f"已改 {secret_path}")
    [(_target, text)] = adapter.sent
    assert _TOKEN not in text and "/Users/alice/keys/" in text and "/repo/lexer.py" in text
    shown = step_display_text(f"读取 {secret_path}", max_chars=240, preserve_host_paths=True)
    assert _TOKEN not in shown and "/Users/alice/keys/" in shown


@pytest.mark.parametrize(("display", "full"), [("full", True), ("", False)])
def test_progress_text_draft_follows_the_pending_display(display, full):
    service, adapter = _service()
    receipt = send_gateway_draft(service, _pending(display), f"执行中：读取 {_PATH}")
    assert receipt.ok, receipt
    [(_target, text, _options)] = adapter.created
    assert (_PATH in text) is full and "lexer.py" in text


@pytest.mark.parametrize(("display", "full"), [("full", True), ("", False)])
def test_progress_card_draft_follows_the_pending_display(display, full):
    service, adapter = _service()
    steps = [{"step_id": "call-1", "tool_name": "read_file", "phase": "running", "label": f"读取 {_PATH}"}]
    pending = _pending(display, progress_draft={"render_kind": "card", "steps": steps})
    receipt = send_gateway_draft(service, pending, "执行记录更新")
    assert receipt.ok, receipt
    [(_target, _text, options)] = adapter.created
    card = json.dumps(options["card"], ensure_ascii=False)
    assert (_PATH in card) is full and "lexer.py" in card


@pytest.mark.parametrize("preserve", [True, False])
def test_card_renderers_keep_full_paths_only_when_asked(preserve):
    events = [{"kind": "tool_progress", "level": "on", "call_id": "call-1", "tool": "read_file", "phase": "running",
               "label": f"读取 {_PATH}", "result_excerpt": _PATH}]
    [step] = project_execution_view(events, request_id="req-s12b", preserve_host_paths=preserve)["steps"]
    assert (_PATH in step["label"]) is preserve and (_PATH in step["result_excerpt"]) is preserve
    card = json.dumps(render_progress_card({"steps": [dict(step, detail_ref=_PATH)]}, preserve_host_paths=preserve),
                      ensure_ascii=False)
    assert (_PATH in card) is preserve and "lexer.py" in card
    assert (step_display_text(_PATH, max_chars=240, preserve_host_paths=preserve) == _PATH) is preserve


@pytest.mark.parametrize(("display", "full"), [("full", True), ("", False)])
def test_adapter_progress_poll_projects_steps_with_the_pending_display(monkeypatch, display, full):
    import agent_py_agent.agent.adapter.manager as manager
    import agent_py_agent.agent.gateway_parts.http_service as http_service

    body = {"request_id": "req-s12b", "next": 1, "events": [{
        "kind": "tool_progress", "level": "on", "call_id": "call-1", "tool": "read_file", "phase": "running",
        "label": f"读取 {_PATH}"}]}

    def opener(_request, *, timeout):
        response = io.BytesIO(json.dumps(body).encode())
        response.status = 200
        return response

    monkeypatch.setattr(http_service, "_server_instance", None)
    monkeypatch.setattr(manager, "open_authenticated_request", opener)
    client = manager._GatewayReplyPollClient(31415)
    client.credentials = SimpleNamespace(headers=lambda: {})
    [step] = client.poll_progress(_pending(display)).steps
    assert (_PATH in step["label"]) is full and "lexer.py" in step["label"]


# 函数用途: 以真实鉴权中间件读取一次 /progress，返回公开事件 JSON 文本。
def _read_progress(server, request_id, *, user, channel="feishu", admin=""):  # noqa: F811
    from agent_py_agent.agent.auth.manager import AuthManager
    from agent_py_agent.agent.auth.middleware import AuthMiddleware

    headers = Message()
    headers["X-Gateway-Token"] = "fixture-owned-g1"
    headers["X-User-Id"], headers["X-Channel"] = user, channel
    sent = []
    handler = SimpleNamespace(
        path=f"/progress/{request_id}?since=0", headers=headers, client_address=("127.0.0.1", 1),
        _auth_middleware=AuthMiddleware(AuthManager(admin_user_id=admin, auth_enabled=True), auth_token="fixture-owned-g1"),
        _send_json=lambda code, payload: sent.append((code, payload)))
    http.handle_progress(handler, server)
    [(status, payload)] = sent
    assert status == 200, payload
    return json.dumps(payload["events"], ensure_ascii=False)


# 函数用途: 给请求写一条模型过程说明，模拟执行侧已落盘的进度行。
def _write_commentary(server, request_id):  # noqa: F811
    chunk = gateway_chunk_path(server.paths, request_id)
    chunk.parent.mkdir(parents=True, exist_ok=True)
    chunk.write_text(json.dumps({"kind": "assistant_commentary", "text": f"正在修改 {_PATH}"},
                                ensure_ascii=False) + "\n", encoding="utf-8")


@pytest.mark.parametrize(("user", "chat", "viewer", "channel", "admin", "full"), [
    ("ou_admin", "p2p", "ou_admin", "feishu", "", True),
    ("ou_admin", "group", "ou_admin", "feishu", "", False),
    ("ou_admin", "p2p", "ou_boss", "feishu", "ou_boss", False),
    ("ou_admin", "p2p", "ou_admin", "http", "", False),
])
def test_progress_poll_keeps_full_paths_only_for_the_requests_own_im_viewer(
        server, user, chat, viewer, channel, admin, full):  # noqa: F811
    _result, request_id = _admit(server, user, chat)
    _write_commentary(server, request_id)
    events = _read_progress(server, request_id, user=viewer, channel=channel, admin=admin)
    assert (_PATH in events) is full and "lexer.py" in events


@pytest.mark.parametrize("preserve", [True, False])
def test_gateway_writer_card_label_follows_the_frozen_request_flag(tmp_path, preserve):
    writer = BufferedChunkStreamWriter(tmp_path / "chunks.jsonl", delivery_channel="feishu", preserve_host_paths=preserve)
    writer.set_verbose_level("on")
    writer.write_progress({"tool": "read_file", "round": 1, "call_index": 0, "phase": "started",
                           "input": {"path": _PATH}}, "开始")
    writer.close()
    rows = [json.loads(line) for line in writer.chunk_path.read_text(encoding="utf-8").splitlines()]
    [label] = [row["progress"]["label"] for row in rows if row.get("kind") == "tool_progress"]
    assert (label == _PATH) is preserve and label.endswith("lexer.py")


# 函数用途: 给消息工具装上本轮宿主参数：来源私聊身份与本请求流写入器的冻结展示标志。
def _bind_turn(tool, *, preserve, actor=True):
    owner_id = tool.agent.home_paths.owner_id
    source = HostChannelActor(owner_id=owner_id, source_thread_id="thread-s12b", entry_request_id="req-s12b",
                              channel="feishu", conversation_id="feishu-p2p", actor_id="ou_current_user")
    tool.agent._current_run_params = SimpleNamespace(
        host_channel_actor=source if actor else None, task_attributes=None,
        on_chunk=SimpleNamespace(preserve_host_paths=preserve))


@pytest.mark.parametrize(("preserve", "actor", "full"), [(True, True, True), (False, True, False), (True, False, False)])
def test_message_tool_keeps_full_paths_only_for_the_turns_own_frozen_chat(tmp_path, monkeypatch, preserve, actor, full):
    from agent_py_agent.agent.capability import channel_message_tool
    from agent_py_agent.tests.test_channel_message_tool import _tool

    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    tool, adapter = _tool(owner_root)
    monkeypatch.setattr(channel_message_tool, "_delegated_origin_send_blocked", lambda *_args: False)
    _bind_turn(tool, preserve=preserve, actor=actor)
    result = tool.execute({"message": f"已改 {_PATH}", "__run_scope": {"request_id": "req-s12b"}})
    assert result.ok is True, result
    [(target, text)] = adapter.messages
    assert target == "ou_current_user" and (_PATH in text) is full and "lexer.py" in text
    evidence = result.result_envelope["delivery_evidence"]["content"]
    assert (_PATH in evidence) is full
