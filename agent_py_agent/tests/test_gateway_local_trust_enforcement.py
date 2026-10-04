"""G2b：开关 gateway_require_local_credential 打开后的服务端强制判定、启动 fail-closed、插件令牌豁免与 TUI 启动预检。

只用临时数据根、随机端口和假处理器；不读真实 secrets、不连真实 Gateway。
开关关的行为不变由 G1/G2a/G3 既有测试与守卫覆盖，本文件聚焦开关打开的差异：
无凭据回环一律匿名/按原规则拒绝，来源未知（peer_ip=None）不再当可信，插件令牌档要求本机回环、不要求客户端凭据。
"""
from __future__ import annotations

import http.client
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import (
    AuthMiddleware,
    extract_user_from_request,
    require_trusted_source,
)
from agent_py_agent.agent.gateway_parts import http_service
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
    GatewayLocalCredentialRequired,
)
from agent_py_agent.agent.gateway_parts.local_client_token import (
    ensure_local_client_credential,
    local_client_credential_path,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

LOCAL = "127.0.0.1"


# LLM: 强制档中间件统一构造入口；凭据只留在内存，不写日志/env。测试专用 helper。
# 函数用途: 构造开启 G2b 开关的鉴权中间件，可选绑定本机/配置凭据。
def _forced(credential: str = "", auth_token: str = "") -> AuthMiddleware:
    middleware = AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True), auth_token=auth_token,
                                require_local_credential=True)
    if credential:
        middleware.set_local_client_credential(credential)
    return middleware


# 函数用途: 用随机端口发一次请求，返回 (状态码, JSON 或文本)。
def _request(port, method, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=b"{}" if method == "POST" else None, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        content_type = response.getheader("Content-Type", "")
        return response.status, json.loads(data) if content_type.startswith("application/json") else data.decode()
    finally:
        connection.close()


# 函数用途: 构造带数据根与 owner home 的假 Agent；owner home 落在 <root>/owners/... 下，推导出的数据根与 root 一致（luna4 的 G1 合同）。
def _agent_with_root(root):
    return SimpleNamespace(home_paths=SimpleNamespace(root=root, owner_home_dir=root / "owners" / "local" / "main"))


def test_enforced_loopback_without_credential_is_anonymous():
    middleware = _forced()
    assert middleware.extract_identity({"X-User-Id": "fixture-user", "X-Channel": "feishu"}, LOCAL) == ("anonymous", "external")
    assert middleware.extract_identity({}, LOCAL) == ("anonymous", "external")
    assert middleware.check_trusted({}, LOCAL) is False
    ok, _permission, status, _body = middleware.require_admin({"X-Channel": "chat", "X-User-Id": "admin"}, LOCAL)
    assert not ok and status == 403


def test_enforced_wrong_or_empty_credential_is_anonymous():
    middleware = _forced(credential="right-token")
    assert middleware.extract_identity({"X-Gateway-Token": "wrong-token", "X-User-Id": "bob"}, LOCAL)[0] == "anonymous"
    assert middleware.extract_identity({"X-Gateway-Token": ""}, LOCAL) == ("anonymous", "external")
    assert middleware.check_trusted({"X-Gateway-Token": "right-token"}, LOCAL) is True


def test_enforced_valid_credential_keeps_admin_and_identity():
    middleware = _forced(credential="local-token", auth_token="configured-token")
    assert middleware.extract_identity(
        {"X-Gateway-Token": "local-token", "X-User-Id": "bob", "X-Channel": "feishu"}, LOCAL) == ("bob", "feishu")
    assert middleware.extract_identity({"X-Gateway-Token": "configured-token"}, LOCAL) == ("admin", "chat")


def test_enforced_unknown_peer_is_not_trusted():
    middleware = _forced(credential="local-token")
    assert middleware.extract_identity({}, None) == ("anonymous", "external")
    assert middleware.check_trusted({}, None) is False


def test_enforced_require_trusted_source_rejects_and_allows():
    middleware = _forced(credential="local-token")
    replies = []

    def handler_for(headers):
        return SimpleNamespace(_auth_middleware=middleware, headers=headers, client_address=("127.0.0.1", 51234),
                               _send_json=lambda status, body: replies.append((status, body)))

    assert require_trusted_source(handler_for({})) is True
    assert replies[-1][0] == 403
    assert require_trusted_source(handler_for({"X-Gateway-Token": "local-token"})) is False


def test_enforced_startup_generates_missing_credential_and_starts(tmp_path):
    # G1 的"缺则生成"是正常初始化路径：强制档下缺失也照常生成并启动，不是 fail-closed 场景。
    root = tmp_path / "data-root"
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "queue"),
                               params=GatewayHTTPServerParams(agent=_agent_with_root(root),
                                                              require_local_credential=True))
    server.start()
    try:
        assert server.local_credential_status == "ok"
        assert local_client_credential_path(root).is_file()
    finally:
        server.stop()


def test_enforced_startup_refuses_when_credential_cannot_be_created(tmp_path):
    # 数据根不可用（路径被文件占住）时连生成都失败：强制档拒绝启动并给结构化原因码。
    root = tmp_path / "data-root"
    root.write_text("not a directory", encoding="utf-8")
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "queue"),
                               params=GatewayHTTPServerParams(agent=_agent_with_root(root),
                                                              require_local_credential=True))
    with pytest.raises(GatewayLocalCredentialRequired) as caught:
        server.start()
    assert caught.value.reason_code.startswith("LOCAL_CLIENT_CREDENTIAL_")
    assert server.server is None
    server.stop()


@pytest.mark.parametrize("damage,expected_reason", [
    ("corrupt", "LOCAL_CLIENT_CREDENTIAL_INVALID"),
    ("file_mode", "LOCAL_CLIENT_CREDENTIAL_PERMISSIONS"),
])
def test_enforced_startup_refuses_unusable_credential(tmp_path, damage, expected_reason):
    root = tmp_path / "data-root"
    ensure_local_client_credential(root)
    path = local_client_credential_path(root)
    if damage == "corrupt":
        path.write_bytes(b"not-a-credential")
    else:
        path.chmod(0o644)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "queue"),
                               params=GatewayHTTPServerParams(agent=_agent_with_root(root),
                                                              require_local_credential=True))
    with pytest.raises(GatewayLocalCredentialRequired) as caught:
        server.start()
    assert caught.value.reason_code == expected_reason
    server.stop()
    # 坏文件/坏权限不被覆盖、不轮换。
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_enforced_startup_refuses_without_data_root(tmp_path):
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "queue"),
                               params=GatewayHTTPServerParams(agent=SimpleNamespace(), require_local_credential=True))
    with pytest.raises(GatewayLocalCredentialRequired) as caught:
        server.start()
    # luna4 的 G1 加固：Agent 缺 home_paths 合同按 agent_contract 拒绝（旧码 no_data_root 已由 LOCAL_CLIENT_CREDENTIAL_NO_DATA_ROOT 取代）。
    assert caught.value.reason_code == "agent_contract"
    server.stop()


def test_enforced_startup_starts_with_available_credential(tmp_path):
    root = tmp_path / "data-root"
    credential = ensure_local_client_credential(root)
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "queue"),
                               params=GatewayHTTPServerParams(agent=_agent_with_root(root),
                                                              auth_middleware=_forced(credential=credential),
                                                              require_local_credential=True))
    server.start()
    try:
        assert server.local_credential_status == "ok"
    finally:
        server.stop()


@pytest.fixture
def enforced_listener(tmp_path, monkeypatch):
    # 函数用途: 起一个开关打开的随机端口服务，业务处理器换成返回身份投影的 echo。
    def echo(handler):
        handler._send_json(200, {"identity": list(extract_user_from_request(handler))})
    monkeypatch.setattr(http_service.GatewayHTTPHandler, "_handle_ask", echo)
    monkeypatch.setattr(http_service.GatewayHTTPHandler, "_handle_result", echo)
    root = tmp_path / "data-root"
    credential = ensure_local_client_credential(root)
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "queue"),
                               params=GatewayHTTPServerParams(agent=_agent_with_root(root),
                                                              auth_middleware=_forced(credential=credential),
                                                              require_local_credential=True))
    server.start()
    try:
        yield server, server.server.server_address[1], credential
    finally:
        server.stop()


def test_enforced_http_loopback_without_credential_is_anonymous(enforced_listener):
    _server, port, _credential = enforced_listener
    assert _request(port, "POST", "/ask", {"X-User-Id": "fixture-user", "X-Channel": "feishu"}) == (
        200, {"identity": ["anonymous", "external"]})
    assert _request(port, "GET", "/result/fixture-request")[1]["identity"] == ["anonymous", "external"]


def test_enforced_http_valid_credential_keeps_identity(enforced_listener):
    _server, port, credential = enforced_listener
    headers = {"X-Gateway-Token": credential, "X-User-Id": "fixture-user", "X-Channel": "feishu"}
    assert _request(port, "POST", "/ask", headers) == (200, {"identity": ["fixture-user", "feishu"]})
    assert _request(port, "GET", "/result/fixture-request", {"X-Gateway-Token": credential})[1]["identity"] == ["admin", "chat"]


def test_enforced_http_wrong_or_empty_credential_is_anonymous(enforced_listener):
    _server, port, _credential = enforced_listener
    assert _request(port, "POST", "/ask", {"X-Gateway-Token": "wrong-token"})[1]["identity"] == ["anonymous", "external"]
    assert _request(port, "POST", "/ask", {"X-Gateway-Token": ""})[1]["identity"] == ["anonymous", "external"]


def test_enforced_plugin_token_route_passes_without_client_credential(enforced_listener, monkeypatch):
    # be 注意 3：插件进程只有 host-API 令牌、没有本机凭据，开关打开时 /plugin-host/query 仍通。
    from agent_py_agent.agent import plugin_host_api as api

    _server, port, _credential = enforced_listener
    ref = SimpleNamespace(scope=SimpleNamespace(activation_id="fixture-activation"),
                          require=lambda: SimpleNamespace(activation=SimpleNamespace(activation_id="fixture-activation")))
    env = api.issue_host_api_env(ref, "fixture-plugin")
    monkeypatch.setattr(api, "query_host", lambda *args, **kwargs: {"fixture": True})
    status, body = _request(port, "POST", "/plugin-host/query", {"X-Plugin-Host-Token": env[api.HOST_API_TOKEN_ENV]})
    assert status == 200 and body == {"ok": True, "fixture": True}


def test_enforced_plugin_token_route_still_rejects_without_valid_token(enforced_listener):
    _server, port, _credential = enforced_listener
    assert _request(port, "POST", "/plugin-host/query")[0] == 403
    assert _request(port, "POST", "/plugin-host/query", {"X-Plugin-Host-Token": "forged"})[0] == 403


# LLM: 必须修 1（9b 终审）：有效插件令牌也必须来自本机回环；直接调 handler，覆盖开关开、关两种档位。
# 函数用途: 打桩令牌校验与查询，直接调 handle_plugin_host_query，返回最后一次响应。
def _run_plugin_query(monkeypatch, peer, token, middleware):
    from agent_py_agent.agent import plugin_host_api as api

    monkeypatch.setattr(api, "verify_host_api_token",
                        lambda value: SimpleNamespace(plugin_id="p") if value == "valid" else None)
    monkeypatch.setattr(api, "query_host", lambda *_args, **_kwargs: {"reached_query": True})
    sent = []
    handler = SimpleNamespace(client_address=(peer, 5555), headers={"X-Plugin-Host-Token": token},
                              _auth_middleware=middleware,
                              _send_json=lambda status, body: sent.append((status, body)),
                              _read_json=lambda: {})
    api.handle_plugin_host_query(handler, SimpleNamespace(agent=None, port=1))
    return sent[-1]


@pytest.mark.parametrize("enforced", [True, False])
def test_plugin_token_remote_peer_is_rejected_switch_on_and_off(monkeypatch, enforced):
    middleware = _forced() if enforced else AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True))
    status, _body = _run_plugin_query(monkeypatch, "192.0.2.10", "valid", middleware)
    assert status == 403


@pytest.mark.parametrize("enforced", [True, False])
def test_plugin_token_loopback_peer_passes_switch_on_and_off(monkeypatch, enforced):
    middleware = _forced() if enforced else AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True))
    status, body = _run_plugin_query(monkeypatch, "127.0.0.1", "valid", middleware)
    assert status == 200 and body == {"ok": True, "reached_query": True}


# --- TUI 启动预检（3a 插话：开关开且凭据不可用时拒绝启动并给原因码，与 IM 适配器同口径） ---


# 函数用途: 写一个只含开关的临时配置，供 make_gateway_chat_client 读取。
def _write_require_config(tmp_path, enabled: bool) -> str:
    path = tmp_path / f"enforced-{str(enabled).lower()}.yaml"
    path.write_text(f"gateway_require_local_credential: {str(enabled).lower()}\n", encoding="utf-8")
    return str(path)


def test_tui_startup_refuses_unavailable_credential_when_enforced(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.local_client_token import LocalClientCredentialError
    from agent_py_agent.cli.chat_client_context import make_gateway_chat_client

    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MY_AGENT_RUNTIME_CONFIG", raising=False)
    monkeypatch.delenv("MY_AGENT_RUNTIME_CONFIG_LAYERS", raising=False)
    args = SimpleNamespace(config=_write_require_config(tmp_path, True), workspace_root="")
    with pytest.raises(LocalClientCredentialError) as caught:
        make_gateway_chat_client(args)
    assert caught.value.reason_code == "LOCAL_CLIENT_CREDENTIAL_MISSING"


def test_tui_startup_degrades_when_not_enforced(tmp_path, monkeypatch):
    from agent_py_agent.cli.bootstrap import DEFAULT_CONFIG
    from agent_py_agent.cli.chat_client_context import make_gateway_chat_client

    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MY_AGENT_RUNTIME_CONFIG", raising=False)
    monkeypatch.delenv("MY_AGENT_RUNTIME_CONFIG_LAYERS", raising=False)
    client = make_gateway_chat_client(SimpleNamespace(config=str(DEFAULT_CONFIG), workspace_root=""))
    assert client.gateway_client_only is True


def test_tui_startup_passes_when_enforced_and_credential_available(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.cli.chat_client_context import make_gateway_chat_client

    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MY_AGENT_RUNTIME_CONFIG", raising=False)
    monkeypatch.delenv("MY_AGENT_RUNTIME_CONFIG_LAYERS", raising=False)
    ensure_local_client_credential(home_paths().root)
    client = make_gateway_chat_client(SimpleNamespace(config=_write_require_config(tmp_path, True), workspace_root=""))
    assert client.gateway_client_only is True


# --- 必须修 3（9b 终审）：从配置到服务端的两处开关接线必须有用例（变异 R5/R6 曾存活） ---


# 函数用途: 构造带 G2b 开关的假配置，供接线用例断言。
def _wiring_config(enabled: bool):
    return SimpleNamespace(auth_enabled=True, admin_user_id="admin", gateway_auth_token="",
                           gateway_require_local_credential=enabled, gateway_bind_host="127.0.0.1")


def test_middleware_switch_is_wired_from_config():
    # R5：_build_gateway_auth_middleware 必须把配置开关传给中间件——回环无凭据请求在开关打开时不可信。
    from agent_py_agent.cli.gateway_process import _build_gateway_auth_middleware

    on = _build_gateway_auth_middleware(_wiring_config(True))
    off = _build_gateway_auth_middleware(_wiring_config(False))
    assert on is not None and off is not None
    assert on.check_trusted({}, "127.0.0.1") is False
    assert off.check_trusted({}, "127.0.0.1") is True


def test_http_server_switch_is_wired_from_config(monkeypatch):
    # R6：_start_gateway_http 构造的 GatewayHTTPServerParams 必须跟着配置开关走（截获参数断言）。
    from agent_py_agent.cli import gateway_process

    captured = {}

    def capture(port, paths, *, params=None):
        captured["port"] = port
        captured["params"] = params
        return None

    monkeypatch.setattr(gateway_process, "start_http_server", capture)
    agent = SimpleNamespace(config=_wiring_config(True))
    gateway_process._start_gateway_http(agent, SimpleNamespace(), 48080)
    assert captured["params"].require_local_credential is True

    captured.clear()
    gateway_process._start_gateway_http(SimpleNamespace(config=_wiring_config(False)), SimpleNamespace(), 48080)
    assert captured["params"].require_local_credential is False


# 函数用途: 构造一个只够 cmd_gateway_run 预检用的假 agent（开关 + 数据根 root）。
def _run_agent(root, enabled: bool):
    return SimpleNamespace(config=SimpleNamespace(gateway_require_local_credential=enabled),
                           home_paths=SimpleNamespace(root=root))


def test_gateway_run_preflight_refuses_before_any_startup_side_effect(tmp_path, monkeypatch):
    # 开关打开、凭据坏（数据根被文件占住）：必须在 setup 之前零副作用退出。
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli import gateway_process

    root = tmp_path / "data-root"
    root.write_text("not a directory", encoding="utf-8")
    calls = {"setup": 0, "threads": 0}
    monkeypatch.setattr(gateway_process, "make_agent", lambda args: _run_agent(root, True))
    monkeypatch.setattr(gateway_process, "gateway_paths", lambda agent: gateway_paths_from_root(tmp_path / "queue"))
    monkeypatch.setattr(gateway_process, "_cmd_gateway_run_setup", lambda *a: calls.__setitem__("setup", calls["setup"] + 1))
    monkeypatch.setattr(gateway_process, "_cmd_gateway_run_threads",
                        lambda *a: calls.__setitem__("threads", calls["threads"] + 1))
    exit_code = gateway_process.cmd_gateway_run(SimpleNamespace(config="", workspace_root="", after_pid=0))
    assert exit_code == 2
    assert calls == {"setup": 0, "threads": 0}


def test_gateway_run_preflight_generates_missing_credential_and_reaches_setup(tmp_path, monkeypatch):
    # g2bf2 用例 1：开关开 + 凭据缺失 → 预检必须"生成"而不是退出；随后照常进入 setup，凭据权限 0600。
    from agent_py_agent.agent.gateway_parts.local_client_token import local_client_credential_path
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli import gateway_process

    root = tmp_path / "data-root"
    root.mkdir()
    calls = {"setup": 0}

    class _ReachedSetup(Exception):
        pass

    def stop_at_setup(*_args):
        calls["setup"] += 1
        raise _ReachedSetup

    agent = SimpleNamespace(config=SimpleNamespace(gateway_require_local_credential=True),
                            home_paths=SimpleNamespace(root=root, owner_home_dir=root / "owners" / "local" / "main"))
    monkeypatch.setattr(gateway_process, "make_agent", lambda args: agent)
    monkeypatch.setattr(gateway_process, "gateway_paths", lambda a: gateway_paths_from_root(tmp_path / "queue"))
    monkeypatch.setattr(gateway_process, "_cmd_gateway_run_setup", stop_at_setup)
    with pytest.raises(_ReachedSetup):
        gateway_process.cmd_gateway_run(SimpleNamespace(config="", workspace_root="", after_pid=0))
    assert calls["setup"] == 1
    path = local_client_credential_path(root)
    assert path.is_file() and (path.stat().st_mode & 0o777) == 0o600


def test_gateway_run_preflight_refuses_even_with_configured_token(tmp_path, monkeypatch):
    # g2bf2 用例 2：开关开 + 配了 gateway_auth_token + 本机凭据坏 → 预检仍必须拒绝（exit 2，setup 不被调用）。
    # 旧实现用客户端只读逻辑，配了 token 就完全不读本机凭据，这里会错误放行。
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli import gateway_process

    root = tmp_path / "data-root"
    root.mkdir()
    ensure_local_client_credential(root)
    local_client_credential_path(root).write_bytes(b"not-a-credential")
    calls = {"setup": 0}
    agent = SimpleNamespace(config=SimpleNamespace(gateway_require_local_credential=True, gateway_auth_token="deploy-token"),
                            home_paths=SimpleNamespace(root=root, owner_home_dir=root / "owners" / "local" / "main"))
    monkeypatch.setattr(gateway_process, "make_agent", lambda args: agent)
    monkeypatch.setattr(gateway_process, "gateway_paths", lambda a: gateway_paths_from_root(tmp_path / "queue"))
    monkeypatch.setattr(gateway_process, "_cmd_gateway_run_setup", lambda *a: calls.__setitem__("setup", calls["setup"] + 1))
    exit_code = gateway_process.cmd_gateway_run(SimpleNamespace(config="", workspace_root="", after_pid=0))
    assert exit_code == 2
    assert calls["setup"] == 0


def test_gateway_run_preflight_passes_and_proceeds_when_not_enforced(tmp_path, monkeypatch):
    # 开关关（G2a 档）：预检不拦，照常进入 setup（用哨兵异常在 setup 处停下，断言确实走到了）。
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli import gateway_process

    calls = {"setup": 0}

    class _ReachedSetup(Exception):
        pass

    def stop_at_setup(*_args):
        calls["setup"] += 1
        raise _ReachedSetup

    monkeypatch.setattr(gateway_process, "make_agent", lambda args: _run_agent(tmp_path / "root", False))
    monkeypatch.setattr(gateway_process, "gateway_paths", lambda agent: gateway_paths_from_root(tmp_path / "queue"))
    monkeypatch.setattr(gateway_process, "_cmd_gateway_run_setup", stop_at_setup)
    with pytest.raises(_ReachedSetup):
        gateway_process.cmd_gateway_run(SimpleNamespace(config="", workspace_root="", after_pid=0))
    assert calls["setup"] == 1
