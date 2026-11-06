"""边缘身份开关的行为、工厂接线与真实 HTTP 往返；凭据只使用临时宿主。"""
from __future__ import annotations

import http.client
import json
from email.message import Message
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.configuration import build_gateway_auth_middleware
from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware, require_trusted_source
from agent_py_agent.agent.auth.models import Role
from agent_py_agent.agent.gateway_parts.client_credentials import gateway_client_credentials
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.cli.adapter import _feishu_adapter_config

_DEPLOYMENT = "synthetic-s1w5-deployment"
_LOCAL = "synthetic-s1w5-local"
_TOKEN = ("X-Gateway-Token", _DEPLOYMENT)
_USER = ("X-User-Id", "ou_x")
_CHANNEL = ("X-Channel", "feishu")
_REQUIRED = "GATEWAY_EDGE_IDENTITY_REQUIRED"
_FORBIDDEN = "GATEWAY_EDGE_IDENTITY_FORBIDDEN"
_MESSAGE = "untrusted source:不可信来源不可提交任务"


# LLM: 原始副本保留到中间件，不经字典合并；只构造测试数据。
# 函数用途: 将头列表转成真实 HTTPMessage 同类的大小写不敏感载体。
def _headers(*pairs):
    headers = Message()
    for name, value in pairs:
        headers[name] = value
    return headers


# LLM: 显式开关只影响部署凭据；本机凭据使用固定合成值，不读写宿主秘密。
# 函数用途: 构造带两种凭据的独立中间件。
def _middleware(*, enabled=True, manager=None):
    mw = AuthMiddleware(manager or AuthManager(), _DEPLOYMENT, edge_identity_required=enabled)
    mw.set_local_client_credential(_LOCAL)
    return mw


# LLM: 复用真实入口，只替换 handler 的响应写出以捕获状态和结构化正文。
# 函数用途: 观察来源拒绝，不替代认证与角色判定。
def _source_result(mw, headers):
    sent = []
    handler = SimpleNamespace(
        _auth_middleware=mw, headers=headers, client_address=("127.0.0.1", 1),
        _send_json=lambda status, body: sent.append((status, body)),
    )
    return require_trusted_source(handler), sent


@pytest.mark.parametrize("pairs,identity,trusted,error_code", [
    ([], ("anonymous", "external"), False, "LOCAL_CREDENTIAL_REQUIRED"),
    ([_TOKEN], ("admin", "chat"), True, None),
    ([_TOKEN, _USER, _CHANNEL], ("ou_x", "feishu"), True, None),
    ([("X-Gateway-Token", _LOCAL)], ("admin", "chat"), True, None),
    ([_TOKEN, ("x-gateway-token", "different")], ("anonymous", "external"), False, None),
    ([_TOKEN, _USER, ("x-user-id", "different"), _CHANNEL], ("anonymous", "external"), False, None),
    ([_TOKEN, _USER, ("x-user-id", "ou_x"), _CHANNEL], ("ou_x", "feishu"), True, None),
    ([_TOKEN, _USER, ("X-Channel", " ")], ("ou_x", " "), True, None),
    ([("X-Gateway-Token", "different"), _USER, _CHANNEL], ("anonymous", "external"), False, "LOCAL_CREDENTIAL_REQUIRED"),
])
# LLM: 同时固定旧合同结果，防止新版本两个默认调用一起回归仍互相相等。
# 函数用途: 核对默认与显式关闭的身份、信任和来源拒绝响应。
def test_switch_off_matches_legacy_for_all_entry_points(pairs, identity, trusted, error_code):
    legacy = AuthMiddleware(AuthManager(), _DEPLOYMENT)
    legacy.set_local_client_credential(_LOCAL)
    explicit_off = _middleware(enabled=False)
    headers = _headers(*pairs)
    assert explicit_off.extract_identity(headers) == legacy.extract_identity(headers) == identity
    assert explicit_off.check_trusted(headers) == legacy.check_trusted(headers) == trusted
    assert _source_result(explicit_off, headers) == _source_result(legacy, headers)
    body = {"error": "forbidden", "message": _MESSAGE}
    if error_code is not None:
        body["error_code"] = error_code
    assert _source_result(explicit_off, headers) == ((False, []) if trusted else (True, [(403, body)]))
    assert explicit_off.edge_identity_error(headers) is None


@pytest.mark.parametrize("pairs", [
    [_TOKEN], [_TOKEN, _USER], [_TOKEN, _CHANNEL],
    [_TOKEN, ("X-User-Id", ""), _CHANNEL],
    [_TOKEN, ("X-User-Id", " \t"), _CHANNEL],
    [_TOKEN, _USER, ("X-Channel", "")],
    [_TOKEN, _USER, ("X-Channel", " \t")],
    [_TOKEN, _USER, ("x-user-id", "ou_x"), _CHANNEL],
    [_TOKEN, _USER, _CHANNEL, ("x-channel", "feishu")],
    [_TOKEN, _USER, ("X-User-Id", "other"), _CHANNEL],
    [_TOKEN, _USER, _CHANNEL, ("X-Channel", "web")],
])
def test_deployment_requires_unique_nonblank_raw_identity(pairs):
    mw = _middleware()
    headers = _headers(*pairs)
    assert mw.edge_identity_error(headers) == _REQUIRED
    assert mw.check_trusted(headers) is False
    assert mw.extract_identity(headers) == ("anonymous", "external")
    assert mw.require_admin(headers)[0] is False
    assert _source_result(mw, headers) == (
        True, [(403, {"error": "forbidden", "message": _MESSAGE, "error_code": _REQUIRED})],
    )


@pytest.mark.parametrize("channel", ["chat", "cli", "terminal", " chat ", "\tcli\t"])
def test_final_admin_role_is_forbidden(channel):
    mw = _middleware()
    headers = _headers(_TOKEN, _USER, ("X-Channel", channel))
    assert mw.edge_identity_error(headers) == _FORBIDDEN
    assert not mw.check_trusted(headers)
    assert mw.extract_identity(headers) == ("anonymous", "external")
    assert mw.require_admin(headers)[0] is False
    assert _source_result(mw, headers) == (
        True, [(403, {"error": "forbidden", "message": _MESSAGE, "error_code": _FORBIDDEN})],
    )


@pytest.mark.parametrize("admin_id,user,allowed", [
    ("admin", "admin", False), ("ops", "ops", False), ("ops", "admin", True),
    ("admin", "ou_x", True),
])
def test_configured_admin_identity_uses_manager_role(admin_id, user, allowed):
    mw = _middleware(manager=AuthManager(admin_user_id=admin_id))
    headers = _headers(_TOKEN, ("X-User-Id", user), _CHANNEL)
    assert mw.edge_identity_error(headers) == (None if allowed else _FORBIDDEN)
    assert mw.check_trusted(headers) is allowed
    if allowed:
        assert mw.extract_identity(headers) == (user, "feishu")
        assert mw.get_permission(headers).role is Role.USER
        assert _source_result(mw, headers) == (False, [])


def test_local_g1_without_identity_remains_terminal_admin():
    mw = _middleware()
    headers = _headers(("X-Gateway-Token", _LOCAL))
    assert mw.credential_kind(headers) == "local"
    assert mw.edge_identity_error(headers) is None
    assert mw.extract_identity(headers) == ("admin", "chat")
    assert mw.require_admin(headers)[0] is True
    assert _source_result(mw, headers) == (False, [])


@pytest.mark.parametrize("pairs,kind", [
    ([], None), ([("X-Gateway-Token", "")], None),
    ([("X-Gateway-Token", "different")], None),
    ([_TOKEN], "deployment"), ([("X-Gateway-Token", _LOCAL)], "local"),
    ([_TOKEN, ("x-gateway-token", _DEPLOYMENT)], "deployment"),
    ([_TOKEN, ("x-gateway-token", _LOCAL)], None),
])
def test_credential_kind_matches_existing_credential_gate(pairs, kind):
    mw = _middleware()
    headers = _headers(*pairs)
    assert mw.credential_kind(headers) == kind
    assert mw.has_gateway_credential(headers) is (kind is not None)
    if kind is None:
        assert mw.edge_identity_error(headers) is None
        assert not mw.check_trusted(headers)


def test_identical_local_and_deployment_secret_has_local_precedence():
    mw = _middleware()
    mw.set_local_client_credential(_DEPLOYMENT)
    headers = _headers(_TOKEN)
    assert mw.credential_kind(headers) == "local"
    assert mw.edge_identity_error(headers) is None
    assert mw.require_admin(headers)[0] is True


class _SpyManager(AuthManager):
    # LLM: 记录传入原值与真实角色；不复制角色策略或自行规范化。
    # 函数用途: 为两条身份路径提供同一个可观察认证入口。
    def authenticate(self, channel, user_id):
        permission = super().authenticate(channel, user_id)
        self.calls.append((channel, user_id, permission.role))
        return permission


@pytest.mark.parametrize("user,channel", [
    ("ou_x", "feishu"), ("u1", "web"), (" ou_x ", " feishu "),
    ("ADMIN", "CHAT"), (" admin ", "feishu"),
])
def test_role_check_and_final_identity_share_raw_values(user, channel):
    manager = _SpyManager()
    manager.calls = []
    mw = _middleware(manager=manager)
    headers = _headers(_TOKEN, ("X-User-Id", user), ("X-Channel", channel))
    assert mw.edge_identity_error(headers) is None
    gate_call = manager.calls[-1]
    identity = mw.extract_identity(headers)
    permission = manager.authenticate(*reversed(identity))
    assert identity == (user, channel)
    assert gate_call == manager.calls[-1] == (channel, user, Role.USER)
    assert permission.role is gate_call[2]


def test_manager_role_not_a_separate_channel_policy():
    mw = _middleware(manager=AuthManager(auth_enabled=False))
    assert mw.edge_identity_error(_headers(_TOKEN, _USER, _CHANNEL)) == _FORBIDDEN


@pytest.mark.parametrize("enabled", [None, False, True])
def test_config_composition_and_host_factories(enabled, tmp_path):
    cfg = SimpleNamespace(
        gateway_auth_token=_DEPLOYMENT, feishu_app_id="", feishu_app_secret="",
        feishu_verification_token="",
    )
    if enabled is not None:
        cfg.gateway_edge_identity_required = enabled
    agent = SimpleNamespace(config=cfg, home_paths=SimpleNamespace(root=tmp_path))
    mw = build_gateway_auth_middleware(cfg)
    assert mw.edge_identity_required is bool(enabled)
    expected = "" if enabled else _DEPLOYMENT
    credentials = gateway_client_credentials(agent)
    assert credentials.configured_token == expected
    assert credentials.data_root == tmp_path
    assert _feishu_adapter_config(agent)["gateway_auth_token"] == expected


# LLM: 通过真实 socket 发送原始头列表并解析真实响应，重复字段不在客户端合并。
# 函数用途: 对本测试临时 Gateway 执行一次 JSON POST 往返。
def _post_json(port, path, pairs, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body or {}).encode("utf-8")
    try:
        connection.putrequest("POST", path)
        for name, value in pairs:
            connection.putheader(name, value)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(data)))
        connection.endheaders(data)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_http_edge_identity_and_factory_g1_roundtrip(tmp_path):
    from agent_py_agent.tests.test_gateway_auth_hardening import _host, _Paths

    host = _host(tmp_path)
    host.config.gateway_auth_token = _DEPLOYMENT
    host.config.gateway_edge_identity_required = True
    mw = build_gateway_auth_middleware(host.config)
    server = GatewayHTTPServer(
        0, _Paths(tmp_path),
        params=GatewayHTTPServerParams(agent=host, auth_middleware=mw),
    )
    server.start()
    try:
        port = server.server.server_address[1]
        ask = {"kind": "ask", "goal": "边缘身份往返"}
        status, payload = _post_json(port, "/ask", [_TOKEN], ask)
        assert status == 403
        assert payload["error_code"] == _REQUIRED
        assert payload["message"] == _MESSAGE
        status, payload = _post_json(port, "/ask", [_TOKEN, _USER, _USER, _CHANNEL], ask)
        assert status == 403 and payload["error_code"] == _REQUIRED
        status, payload = _post_json(port, "/ask", [_TOKEN, _USER, ("X-Channel", "cli")], ask)
        assert status == 403 and payload["error_code"] == _FORBIDDEN
        identity = [_TOKEN, ("X-User-Id", "bob"), _CHANNEL]
        assert _post_json(port, "/ask", identity, ask)[0] == 202
        assert _post_json(port, "/stop", identity)[0] == 403
        client = gateway_client_credentials(host)
        assert client.configured_token == ""
        local_headers = client.headers()
        assert mw.credential_kind(local_headers) == "local"
        assert _post_json(port, "/stop", list(local_headers.items()))[0] == 200
    finally:
        server.stop()
