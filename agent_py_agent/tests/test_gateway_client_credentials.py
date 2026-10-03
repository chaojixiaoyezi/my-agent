"""G3：真实客户端运输接随机端口假服务；凭据只来自 tmp_path。

返工（g3f）：凭据读不到时按 G2b 开关分流——默认（开关关）降级为不带 X-Gateway-Token 继续发送，
只记一次结构化 warning；开关开才在网络请求前拒绝（零请求、G1 原因码）。成功附凭据、配置 token 优先、
direct 不读凭据的用例全部保留。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.delivery import PendingGatewayReply
from agent_py_agent.agent.adapter.manager import ChannelManager, configure_gateway_client
from agent_py_agent.agent.adapter.protocol import IncomingMessage
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts.client_credentials import GatewayClientCredentials
from agent_py_agent.agent.gateway_parts.local_client_token import (
    LocalClientCredentialError,
    ensure_local_client_credential,
    local_client_credential_path,
)
from agent_py_agent.agent.plugin_command_catalog import PluginCommandCatalog
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent
from agent_py_agent.cli.chat_parts.control_runtime import (
    ChatControlExecution,
    ChatControlState,
    execute_chat_control,
    request_gateway_control_status,
)
from agent_py_agent.cli.chat_parts.plugin_command_client import PluginCommandClient

_CREDENTIAL_LOGGER = "agent_py_agent.agent.gateway_parts.client_credentials"


@pytest.fixture
def fake_gateway():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            payload = json.loads(data) if data else None
            requests.append((self.path, self.headers, payload))
            body = json.dumps({
                "ok": True, "request_id": "request-fixture", "status": "queued",
                "kind": "status", "message": "功能照旧", "response": "功能照旧",
                "operation_id": "operation-fixture", "receipt_id": "operation-fixture",
                "control_state": "completed", "delivery_status": "accepted",
                "input_state": "consumed", "events": [], "next": 0,
                "catalog": PluginCommandCatalog("fixture-scope").to_payload(), "panels": [],
            }).encode()
            media_type = "application/json"
            if payload and payload.get("interactive"):
                from agent_py_agent.agent.gateway_parts.command_stream_protocol import (
                    CommandStreamFrame,
                )
                request_id = payload["plugin_request_id"]
                body = CommandStreamFrame(request_id, "connected", {"owner": {"provider": "local", "owner_kind": "main", "owner_id": "main"}}).encode()
                body += CommandStreamFrame(request_id, "result", {"ok": True, "state": "succeeded", "message": "功能照旧"}).encode()
                media_type = "application/x-ndjson"
            self.send_response(200)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _reply
        do_POST = _reply

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield SimpleNamespace(port=server.server_port, requests=requests)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


@pytest.fixture
def credential_home(tmp_path, monkeypatch):
    root = tmp_path / "host-data"
    monkeypatch.setenv("MY_AGENT_HOME", str(root))
    return root, ensure_local_client_credential(root)


@pytest.fixture
def credential_environment(credential_home, fake_gateway, caplog):
    """把一个用例需要的根、token、端口、请求记录和日志捕获收成一个四元组，避免测试函数签名过长。"""
    root, token = credential_home
    return root, token, fake_gateway.port, caplog, fake_gateway.requests


@pytest.fixture(autouse=True)
def _reset_credential_warning_dedup():
    """warning 去重是进程级状态；每个用例前后清空，保证“只记一次”断言跨用例真实。"""
    from agent_py_agent.agent.gateway_parts import client_credentials

    def _clear() -> None:
        with client_credentials._CREDENTIAL_WARNINGS_LOCK:
            client_credentials._WARNED_CREDENTIAL_REASONS.clear()

    _clear()
    yield
    _clear()


def _agent(root, port, configured_token="", *, require_credential=False):
    return SimpleNamespace(
        home_paths=SimpleNamespace(root=root), root=root, workspace_roots=[root],
        config=SimpleNamespace(
            gateway_port=port,
            gateway_auth_token=configured_token,
            gateway_require_local_credential=require_credential,
        ),
        owner_identity=OwnerIdentity.local_main(),
    )


def _tui(root, port, configured_token="", *, require_credential=False):
    agent = _agent(root, port, configured_token, require_credential=require_credential)
    return GatewayChatClientAgent(
        SimpleNamespace(), agent.config,
        root, [root], SimpleNamespace(root=root),
        owner_identity=OwnerIdentity.provider_user("tui-fixture", "alice"),
    )


# LLM: 控制客户端的三项差异（模式、部署 token、是否强制凭据）收进一个小数据对象，避免 helper 参数超过 4 个。
# 类用途: 描述一个控制测试替身要用的传输模式和凭据来源。
@dataclass(frozen=True)
class _ControlOptions:
    use_gateway: bool = True
    configured_token: str = ""
    require_credential: bool = False


def _control(root, port, options: _ControlOptions | None = None):
    options = options or _ControlOptions()
    return ChatControlExecution(
        _agent(root, port, options.configured_token, require_credential=options.require_credential),
        options.use_gateway,
        ChatControlState(False, 0, "", 0.0, "session-fixture"),
    )


def _im_manager(root, port, *, require_credential=False):
    manager = ChannelManager(gateway_port=port)
    if require_credential:
        manager._reply_poll_client.credentials = GatewayClientCredentials(
            root, require_local_credential=True,
        )
    return manager


def _message():
    return IncomingMessage(
        "feishu", "user-fixture", "问题", "message-fixture",
        conversation_id="session-fixture",
    )


def _break_credential(root, token, reason):
    path = local_client_credential_path(root)
    if reason == "MISSING":
        path.unlink()
    elif reason == "PERMISSIONS":
        path.chmod(0o644)
    else:
        path.write_text(token + "-broken", encoding="ascii")


def _invoke(client, root, port, *, require_credential=False):
    if client == "tui":
        return _tui(root, port, require_credential=require_credential).post_gateway_json("/client/history", {}, timeout=2)[1]
    if client == "cli":
        result = execute_chat_control(_control(root, port, _ControlOptions(require_credential=require_credential)), parse_conversation_control("/status"))
        return {"ok": result.ok, "error_code": result.error_code, "message": result.message}
    result = _im_manager(root, port, require_credential=require_credential)._submit_gateway_ask(_message())
    return {"ok": result.ok, "request_id": result.request_id}


def _credential_warnings(caplog):
    return [record for record in caplog.records if record.name == _CREDENTIAL_LOGGER]


@pytest.mark.parametrize("client", ["tui", "cli", "im"])
def test_each_client_sends_file_credential_and_keeps_function(client, credential_home, fake_gateway):
    root, token = credential_home
    assert _invoke(client, root, fake_gateway.port)["ok"] is True
    assert len(fake_gateway.requests) == 1
    headers = fake_gateway.requests[0][1]
    assert headers.get_all("X-Gateway-Token") == [token], "每个客户端必须且只能发送一个凭据头"
    assert headers["X-User-Id"] == {"tui": "alice", "cli": "local-agent", "im": "user-fixture"}[client]


@pytest.mark.parametrize("client", ["tui", "cli", "im"])
def test_configured_token_is_the_only_header_and_does_not_require_file(client, credential_home, fake_gateway):
    root, _token = credential_home
    local_client_credential_path(root).unlink()
    configured = "configured-fixture-token"
    if client == "tui":
        assert _tui(root, fake_gateway.port, configured).post_gateway_json("/client/history", {}, timeout=2)[1]["ok"]
    elif client == "cli":
        assert execute_chat_control(_control(root, fake_gateway.port, _ControlOptions(configured_token=configured)), parse_conversation_control("/status")).ok
    else:
        manager = ChannelManager(gateway_port=fake_gateway.port)
        configure_gateway_client(manager, _agent(root, fake_gateway.port, configured))
        assert manager._submit_gateway_ask(_message()).ok
    assert fake_gateway.requests[0][1].get_all("X-Gateway-Token") == [configured]


@pytest.mark.parametrize("operation", ["get", "control_status", "progress", "input_receipt", "control_receipt", "response"])
def test_read_and_reconciliation_paths_also_attach_credential(operation, credential_home, fake_gateway):
    root, token = credential_home
    pending = PendingGatewayReply(
        "request-fixture", "feishu", "user-fixture", "message-fixture",
        operation_id="operation-fixture", receipt_id="operation-fixture",
        conversation_id="session-fixture",
    )
    if operation == "get":
        assert _tui(root, fake_gateway.port).get_gateway_json("/input-status/request-fixture", timeout=2)[0] == 200
    elif operation == "control_status":
        assert request_gateway_control_status(_control(root, fake_gateway.port), "operation-fixture").ok
    else:
        client = ChannelManager(gateway_port=fake_gateway.port)._reply_poll_client
        result = client.poll_response(pending, interval=0) if operation == "response" else getattr(client, "poll_" + operation)(pending)
    if operation == "response":
        assert result == "功能照旧"
    assert fake_gateway.requests[0][1].get_all("X-Gateway-Token") == [token]


def test_direct_mode_does_not_read_gateway_credential(credential_home, fake_gateway):
    root, _token = credential_home
    local_client_credential_path(root).unlink()
    result = execute_chat_control(_control(root, fake_gateway.port, _ControlOptions(use_gateway=False)), parse_conversation_control("/status"))
    assert result.ok and fake_gateway.requests == []


def _plugin_request(operation, root, port, *, require_credential=False):
    from agent_py_agent.agent.common.cancellation import CancellationToken
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli.chat_parts.command_interaction import CommandInteraction
    client = PluginCommandClient(
        _agent(root, port, require_credential=require_credential), "session-fixture", use_gateway=True,
    )
    if operation == "catalog":
        return client.refresh()
    if operation == "panels":
        return client.panels((("fixture", "panel"),))
    interaction = CommandInteraction("command-fixture", lambda *_: pytest.fail("不申请审批"), CancellationToken(), gateway_paths_from_root(root / "gateway"))
    return client.command("/plugins list", revision="fixture-revision", interaction=interaction)


@pytest.mark.parametrize("operation", ["catalog", "panels", "stream"])
def test_plugin_transports_attach_credential(operation, credential_home, fake_gateway):
    root, token = credential_home
    assert _plugin_request(operation, root, fake_gateway.port).get("ok", True)
    assert len(fake_gateway.requests) == 1
    assert fake_gateway.requests[0][1].get_all("X-Gateway-Token") == [token]


@pytest.mark.parametrize("module", ["run_channel_e2e_group", "run_r1_gate", "run_r1_02_gate", "gateway_pressure"])
def test_repository_harness_http_headers(module, credential_home, fake_gateway):
    import importlib
    root, token = credential_home
    harness = importlib.import_module("scripts." + module)
    url = f"http://127.0.0.1:{fake_gateway.port}"
    if module == "gateway_pressure":
        assert harness._post_ask(url, "user-fixture", "功能照旧")[0] == 200
    else:
        assert harness._http("POST", url + "/ask", {"kind": "ask"}, {"X-User-Id": "user-fixture"})["ok"]
    assert fake_gateway.requests[0][1].get_all("X-Gateway-Token") == [token]


@pytest.mark.parametrize("client", ["tui", "cli", "im"])
def test_configured_and_file_tokens_do_not_merge(client, credential_home, fake_gateway):
    root, file_token = credential_home
    configured = "configured-fixture-token"
    if client == "tui":
        assert _tui(root, fake_gateway.port, configured).post_gateway_json("/client/history", {}, timeout=2)[1]["ok"]
    elif client == "cli":
        assert execute_chat_control(_control(root, fake_gateway.port, _ControlOptions(configured_token=configured)), parse_conversation_control("/status")).ok
    else:
        manager = ChannelManager(gateway_port=fake_gateway.port)
        configure_gateway_client(manager, _agent(root, fake_gateway.port, configured))
        assert manager._submit_gateway_ask(_message()).ok
    values = fake_gateway.requests[0][1].get_all("X-Gateway-Token")
    assert values == [configured] and file_token not in str(values)


def test_proxy_health_never_receives_gateway_credential(credential_home, fake_gateway):
    from scripts.run_r1_02_gate import _proxy_health
    root, _token = credential_home
    local_client_credential_path(root).unlink()
    assert _proxy_health(f"http://127.0.0.1:{fake_gateway.port}/health")["http_status"] == 200
    assert fake_gateway.requests[0][1].get_all("X-Gateway-Token") is None


# --- 返工（g3f）：开关关（默认）降级继续发送 / 开关开拒绝 ---


@pytest.mark.parametrize("client, reason", [(c, r) for c in ("tui", "cli", "im") for r in ("MISSING", "PERMISSIONS", "INVALID")])
def test_credential_failure_with_switch_off_degrades_and_warns_once(client, reason, credential_environment):
    root, token, port, caplog, requests = credential_environment
    fake_gateway = SimpleNamespace(port=port, requests=requests)
    _break_credential(root, token, reason)
    caplog.set_level(logging.WARNING, logger=_CREDENTIAL_LOGGER)
    assert _invoke(client, root, port)["ok"] is True
    assert _invoke(client, root, port)["ok"] is True
    assert len(fake_gateway.requests) == 2, "开关关时凭据读不到也必须照常发出请求"
    assert all(headers.get_all("X-Gateway-Token") is None for _path, headers, _payload in fake_gateway.requests)
    warnings = _credential_warnings(caplog)
    assert len(warnings) == 1, "同一原因在同一进程只记一次 warning"
    assert f"reason_code=LOCAL_CLIENT_CREDENTIAL_{reason}" in warnings[0].getMessage()
    assert token not in caplog.text
    for warning in warnings:
        assert token not in warning.getMessage() and str(root) not in warning.getMessage()


@pytest.mark.parametrize("client", ["tui", "cli", "im"])
@pytest.mark.parametrize("reason", ["MISSING", "PERMISSIONS", "INVALID"])
def test_credential_failure_with_switch_on_is_rejected_before_http(client, reason, credential_home, fake_gateway):
    root, token = credential_home
    _break_credential(root, token, reason)
    try:
        result = _invoke(client, root, fake_gateway.port, require_credential=True)
    except LocalClientCredentialError as exc:
        result = {"ok": False, "error_code": exc.reason_code, "message": str(exc)}
    assert result.get("error_code") == f"LOCAL_CLIENT_CREDENTIAL_{reason}"
    assert result["ok"] is False
    assert token not in result["message"] and str(root) not in result["message"]
    assert "Gateway" in result["message"], "客户端错误应给宿主处置提示"
    assert fake_gateway.requests == [], "开关开时读取失败不得发送请求"


def test_degraded_warning_dedup_is_per_reason(credential_home, fake_gateway, caplog):
    root, token = credential_home
    caplog.set_level(logging.WARNING, logger=_CREDENTIAL_LOGGER)
    path = local_client_credential_path(root)
    path.unlink()
    _tui(root, fake_gateway.port).post_gateway_json("/client/history", {}, timeout=2)
    path.write_text(token + "-broken", encoding="ascii")
    path.chmod(0o600)
    _tui(root, fake_gateway.port).post_gateway_json("/client/history", {}, timeout=2)
    messages = [record.getMessage() for record in _credential_warnings(caplog)]
    assert len(messages) == 2, "不同原因各记一次"
    assert "LOCAL_CLIENT_CREDENTIAL_MISSING" in messages[0]
    assert "LOCAL_CLIENT_CREDENTIAL_INVALID" in messages[1]


def test_credential_never_enters_logs_environment_or_error_text(credential_home, fake_gateway, caplog):
    root, token = credential_home
    caplog.set_level(logging.DEBUG)
    for client in ("tui", "cli", "im"):
        assert _invoke(client, root, fake_gateway.port)["ok"]
    assert token not in caplog.text
    import sys
    assert all(token not in arg for arg in sys.argv)
    assert all(token not in value for value in os.environ.values())
    local_client_credential_path(root).write_text(token + "-invalid", encoding="ascii")
    # 降级路径：照常发送，日志里也不得出现凭据或路径。
    assert _invoke("cli", root, fake_gateway.port)["ok"]
    assert token not in caplog.text
    for warning in _credential_warnings(caplog):
        assert token not in warning.getMessage() and str(root) not in warning.getMessage()
    # 开关开路径：错误文字只给原因码。
    try:
        _invoke("tui", root, fake_gateway.port, require_credential=True)
    except LocalClientCredentialError as exc:
        assert token not in str(exc) and token not in repr(exc)


def test_adapter_startup_degrades_when_switch_off(credential_home, fake_gateway, caplog):
    root, _token = credential_home
    local_client_credential_path(root).unlink()
    caplog.set_level(logging.WARNING, logger=_CREDENTIAL_LOGGER)
    manager = ChannelManager(gateway_port=fake_gateway.port)
    configure_gateway_client(manager, _agent(root, fake_gateway.port))
    result = manager._submit_gateway_ask(_message())
    assert result.ok and len(fake_gateway.requests) == 1
    assert fake_gateway.requests[0][1].get_all("X-Gateway-Token") is None
    assert len(_credential_warnings(caplog)) == 1


def test_adapter_startup_refuses_missing_credential_when_switch_on(credential_home, fake_gateway):
    root, _token = credential_home
    local_client_credential_path(root).unlink()
    manager = ChannelManager(gateway_port=fake_gateway.port)
    with pytest.raises(LocalClientCredentialError) as caught:
        configure_gateway_client(manager, _agent(root, fake_gateway.port, require_credential=True))
    assert caught.value.reason_code == "LOCAL_CLIENT_CREDENTIAL_MISSING"
    assert fake_gateway.requests == []


@pytest.mark.parametrize("operation", ["catalog", "panels", "stream"])
def test_plugin_credential_failure_with_switch_off_still_sends(operation, credential_home, fake_gateway, caplog):
    root, _token = credential_home
    local_client_credential_path(root).unlink()
    caplog.set_level(logging.WARNING, logger=_CREDENTIAL_LOGGER)
    result = _plugin_request(operation, root, fake_gateway.port)
    assert result.get("ok", True)
    assert len(fake_gateway.requests) == 1
    assert fake_gateway.requests[0][1].get_all("X-Gateway-Token") is None
    assert len(_credential_warnings(caplog)) == 1


@pytest.mark.parametrize("operation", ["catalog", "panels", "stream"])
def test_plugin_credential_failure_with_switch_on_is_rejected_not_unknown(operation, credential_home, fake_gateway):
    root, _token = credential_home
    local_client_credential_path(root).unlink()
    result = _plugin_request(operation, root, fake_gateway.port, require_credential=True)
    assert result["error_code"] == "LOCAL_CLIENT_CREDENTIAL_MISSING"
    assert result.get("state") != "outcome_unknown"
    assert "Gateway" in result["message"] and not fake_gateway.requests


def test_durable_im_credential_failure_with_switch_on_replies_once_without_post(credential_home, fake_gateway, monkeypatch):
    root, _token = credential_home
    manager = _im_manager(root, fake_gateway.port, require_credential=True)
    sent = []
    monkeypatch.setattr(manager, "_send_gateway_reply", lambda msg, request_id, text: sent.append(text) or True)
    local_client_credential_path(root).unlink()
    from agent_py_agent.agent.adapter.ingress import build_gateway_ingress_record
    row = build_gateway_ingress_record(channel="feishu", user_id="user-fixture", conversation_id="session-fixture", provider_message_id="message-fixture", content="问题", metadata={}, timestamp=1.0)
    manager._delivery_worker.enqueue(row)
    assert manager._delivery_worker.run_once() == 1
    assert manager._delivery_worker.run_once() == 0
    assert len(sent) == 1 and "LOCAL_CLIENT_CREDENTIAL_MISSING" in sent[0]
    assert fake_gateway.requests == []


def test_durable_im_credential_failure_with_switch_off_posts_without_token(credential_home, fake_gateway):
    root, _token = credential_home
    manager = ChannelManager(gateway_port=fake_gateway.port)
    local_client_credential_path(root).unlink()
    from agent_py_agent.agent.adapter.ingress import build_gateway_ingress_record
    row = build_gateway_ingress_record(channel="feishu", user_id="user-fixture", conversation_id="session-fixture", provider_message_id="message-fixture", content="问题", metadata={}, timestamp=1.0)
    manager._delivery_worker.enqueue(row)
    # 没有注册 feishu 适配器时，提交成功后的占位/watcher 阶段会退避；本用例只观察“提交照常发出”。
    manager._delivery_worker.run_once()
    assert len(fake_gateway.requests) == 1
    assert fake_gateway.requests[0][1].get_all("X-Gateway-Token") is None


def test_goal_editor_preserves_credential_failure_code(credential_home, fake_gateway):
    from agent_py_agent.cli.chat_parts.tui_goal_editor import _request_goal
    root, _token = credential_home
    local_client_credential_path(root).unlink()
    result = _request_goal(_tui(root, fake_gateway.port, require_credential=True), "session-fixture", {"operation": "view"})
    assert result["error_code"] == "LOCAL_CLIENT_CREDENTIAL_MISSING"
    assert "Gateway" in result["message"] and fake_gateway.requests == []


def test_goal_editor_degrades_with_switch_off(credential_home, fake_gateway, caplog):
    from agent_py_agent.cli.chat_parts.tui_goal_editor import _request_goal
    root, _token = credential_home
    local_client_credential_path(root).unlink()
    caplog.set_level(logging.WARNING, logger=_CREDENTIAL_LOGGER)
    result = _request_goal(_tui(root, fake_gateway.port), "session-fixture", {"operation": "view"})
    assert result.get("ok") is True
    assert len(fake_gateway.requests) == 1
    assert fake_gateway.requests[0][1].get_all("X-Gateway-Token") is None
    assert len(_credential_warnings(caplog)) == 1
