"""凭据门后的有界观察：只用随机端口、临时根和无业务副作用的假处理器。"""
from __future__ import annotations

import http.client
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware, extract_user_from_request
from agent_py_agent.agent.gateway_parts import http_service
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.local_client_token import (
    ensure_local_client_credential,
    load_local_client_credential,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.user_space.home_layout import home_paths


def _request(port, method, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=b"{}" if method == "POST" else None, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        return response.status, json.loads(data) if response.getheader("Content-Type", "").startswith("application/json") else data.decode()
    finally:
        connection.close()


@pytest.fixture
def listener(tmp_path, monkeypatch):
    reached = []
    def echo(handler):
        reached.append(handler.path)
        handler._send_json(200, {"identity": list(extract_user_from_request(handler))})
    monkeypatch.setattr(http_service.GatewayHTTPHandler, "_handle_ask", echo)
    monkeypatch.setattr(http_service.GatewayHTTPHandler, "_handle_result", echo)
    monkeypatch.setattr(http_service.GatewayHTTPHandler, "_handle_session_bind", echo)
    middleware = AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True),
                                auth_token=ensure_local_client_credential(tmp_path / "configured-token-fixture"))
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "gateway"),
                               params=GatewayHTTPServerParams(
                                   agent=SimpleNamespace(home_paths=home_paths(tmp_path)),
                                   auth_middleware=middleware,
                               ))
    server.start()
    try:
        server.fixture_reached_handlers = reached
        yield server, server.server.server_address[1], tmp_path
    finally:
        server.stop()


def _observations(port):
    status, body = _request(port, "GET", "/status")
    assert status == 200
    assert isinstance(body.get("uncredentialed_loopback_by_endpoint"), dict)
    return body["uncredentialed_loopback_by_endpoint"]


def test_uncredentialed_loopback_is_rejected_and_counts_only_template(listener):
    server, port, root = listener
    headers = {"X-User-Id": "fixture-user", "X-Channel": "feishu"}
    for method, path, request_headers in [("POST", "/ask", headers),
          ("GET", "/result/fixture-request-A", {}), ("GET", "/result/fixture-request-B?since=2", {}),
          ("POST", "/sessions/fixture-session/bind", {})]:
        status, body = _request(port, method, path, request_headers)
        assert status == 403 and body["error_code"] == "LOCAL_CREDENTIAL_REQUIRED"
        assert "identity" not in body
    assert server.fixture_reached_handlers == []

    facts = _observations(port)
    assert set(facts) == {"/ask", "/result/*", "/sessions/*/bind"}
    assert facts["/result/*"]["count"] == 2 and facts["/ask"]["count"] == 1
    for row in facts.values():
        assert set(row) == {"count", "last_at"}
        assert isinstance(row["count"], int) and row["count"] > 0
        assert isinstance(row["last_at"], float) and row["last_at"] > 0
    serialized = json.dumps(facts)
    assert all(value not in serialized for value in ["fixture-request", "fixture-user", "fixture-session",
                                                    load_local_client_credential(root)])


@pytest.mark.parametrize("credential_source", ["local", "configured"])
def test_credentialed_identity_is_honored_even_remote_and_not_counted(listener, credential_source):
    server, port, root = listener
    token = load_local_client_credential(root) if credential_source == "local" else server.auth_middleware.auth_token
    headers = {"x-gateway-token": token, "X-User-Id": "fixture-user", "X-Channel": "feishu"}
    assert server.auth_middleware.extract_identity(headers, "192.0.2.1") == ("fixture-user", "feishu")
    assert _request(port, "POST", "/ask", headers) == (200, {"identity": ["fixture-user", "feishu"]})
    assert _observations(port) == {}


def test_public_and_plugin_token_routes_are_not_counted(listener):
    server, port, root = listener
    assert _request(port, "GET", "/status")[0] == 200
    assert _request(port, "GET", "/metrics")[0] == 200
    # 插件令牌档不参与归零，连无效令牌拒绝也不能创建计数键。
    assert _request(port, "POST", "/plugin-host/query")[0] == 403
    assert _request(port, "GET", "/unknown/fixture-id")[0] == 404
    assert _observations(port) == {}


def test_valid_activation_token_alone_reaches_plugin_route_without_count(listener, monkeypatch):
    from agent_py_agent.agent import plugin_host_api as api

    server, port, root = listener
    ref = SimpleNamespace(scope=SimpleNamespace(activation_id="fixture-activation"),
                          require=lambda: SimpleNamespace(activation=SimpleNamespace(activation_id="fixture-activation")))
    env = api.issue_host_api_env(ref, "fixture-plugin")
    monkeypatch.setattr(api, "query_host", lambda *args, **kwargs: {"fixture": True})
    status, body = _request(port, "POST", "/plugin-host/query", {"X-Plugin-Host-Token": env[api.HOST_API_TOKEN_ENV]})
    assert status == 200 and body == {"ok": True, "fixture": True}
    assert _observations(port) == {}
    assert load_local_client_credential(root) != env[api.HOST_API_TOKEN_ENV]


def test_counter_thread_safety_and_bounded_schema(listener):
    server, port, _root = listener
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda index: _request(port, "GET", f"/result/fixture-request-{index}"), range(24)))
    assert all(status == 403 and body["error_code"] == "LOCAL_CREDENTIAL_REQUIRED" for status, body in replies)
    assert server.fixture_reached_handlers == []
    facts = _observations(port)
    assert set(facts) == {"/result/*"}
    assert facts["/result/*"]["count"] == 24


def test_uncredentialed_remote_and_unknown_peer_are_never_trusted(listener):
    server, port, _root = listener
    mw = server.auth_middleware
    headers = {"X-User-Id": "fixture-user", "X-Channel": "chat"}
    assert mw.extract_identity(headers, "192.0.2.1") == ("anonymous", "external")
    assert not mw.check_trusted(headers, "192.0.2.1")
    # 未知来源和已知远程均不能借无凭据字段取得管理员身份。
    assert mw.extract_identity({}, None) == ("anonymous", "external")
    assert not mw.check_trusted({}, None)
    assert _observations(port) == {}


def test_auth_disabled_keeps_explicit_single_machine_behavior(tmp_path, monkeypatch):
    monkeypatch.setattr(http_service.GatewayHTTPHandler, "_handle_ask",
                        lambda h: h._send_json(200, {"identity": list(extract_user_from_request(h))}))
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "gateway"))
    server.start()
    try:
        port = server.server.server_address[1]
        assert _request(port, "POST", "/ask", {"X-User-Id": "fixture-user", "X-Channel": "feishu"}) == (200, {"identity": ["admin", "chat"]})
        assert _observations(port) == {}
    finally:
        server.stop()


def test_dispatch_registry_is_single_source_and_tiers_match_review(tmp_path, monkeypatch):
    routes = getattr(http_service, "GATEWAY_HTTP_ROUTES", None)
    assert routes is not None, "分发和观察须复用同一张结构化路由表"
    pairs = [(method, route.template, route.access_tier) for method, group in routes.items() for route in group]
    assert len(pairs) == 32 and len({(method, template) for method, template, _ in pairs}) == 32
    assert ("POST", "/client/goal", "credential") in pairs
    assert ("POST", "/channel-observations", "credential") in pairs
    # 浏览器 OAuth 回调不携宿主头，仍由原处理器验签 state/actor；观察写入口必须持宿主凭据。
    assert {(method, path) for method, path, tier in pairs if tier == "public"} == {
        ("GET", "/status"), ("GET", "/metrics"), ("GET", "/feishu/oauth/callback*"),
    }
    assert {(method, path) for method, path, tier in pairs if tier == "plugin_token"} == {
        ("POST", "/plugin-host/query"), ("POST", "/plugin-host/v2/call"), ("POST", "/plugin-host/v2/stream"),
    }
    assert {path for _, path, tier in pairs if tier == "admin"} == {"/sessions/*/channels", "/admin/summary", "/stop", "/sessions/*/bind"}
    # 所有路由真正经过 do_GET/do_POST，未知路径404；只替换业务处理，不并排造观察路由表。
    called = []
    for method, group in routes.items():
        for route in group:
            def echo(handler, _server=None, label=route.template):
                called.append(label)
                handler._send_json(200, {"route": label})
            target = importlib.import_module(route.handler_module, http_service.__package__) if route.handler_module else http_service.GatewayHTTPHandler
            monkeypatch.setattr(target, route.handler_name, echo)
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "gateway"))
    server.start()
    try:
        port = server.server.server_address[1]
        for method, template, _tier in pairs:
            assert _request(port, method, template.replace("*", "fixture-id")) == (200, {"route": template})
        assert len(called) == len(pairs) == 32
        assert _request(port, "GET", "/no-route")[0] == 404
    finally:
        server.stop()


def test_empty_credentials_never_match_absent_or_empty_token(tmp_path):
    # 本机凭据已绑定、auth_token 为空：空请求头不能被当成“已携带凭据”（G1 变异防线）。
    credential = ensure_local_client_credential(tmp_path)
    bound = AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True), auth_token="")
    bound.set_local_client_credential(credential)
    assert bound.has_gateway_credential({}) is False
    assert bound.has_gateway_credential({"X-Gateway-Token": ""}) is False
    assert bound.extract_identity({}, "192.0.2.1") == ("anonymous", "external")
    # 两种凭据都为空时同样不接受空头；非空但错误的令牌也不匹配。
    empty = AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True), auth_token="")
    assert empty.has_gateway_credential({}) is False
    assert empty.extract_identity({}, "192.0.2.1") == ("anonymous", "external")
    assert bound.has_gateway_credential({"X-Gateway-Token": "wrong-token"}) is False
