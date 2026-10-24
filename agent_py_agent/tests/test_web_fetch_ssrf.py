"""审计 #3 修复真测:web_fetch SSRF 加固——连接 pin 到网关校验过的 IP + 每个重定向跳重新过网关。

真起本地 HTTP server:正常抓取经 pin 连到校验过的回环 IP 拿到响应(证明连的就是网关校验的 IP);
302 跳转到 169.254.169.254(云 metadata)被重新过网关拦下(不再盲目跟随到内网);302 跳转到安全目标仍跟随。
学 工具运行时 受控请求。
"""

from __future__ import annotations

import errno
import socket
import threading
from copy import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_py_agent.agent.contracts.gates.network_safety import (
    GatewayEndpointConfig,
    NetworkSafetySettings,
)
from agent_py_agent.agent.tooling.web import (
    WebFetchTool,
    _effective_gateway_endpoint,
    _network_safety_error,
)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # 静音
        pass

    def _send(self, status: int, ctype: str, body: bytes, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.server.request_paths.append(self.path)
        self.server.request_headers.append(dict(self.headers))
        port = self.server.server_address[1]
        if self.path == "/page":
            self._send(200, "text/html; charset=utf-8", b"<html><body><h1>Hi</h1><p>Hello world</p></body></html>")
            return
        if self.path == "/to-gateway":
            self._send(
                302,
                "text/plain",
                b"",
                {"Location": f"http://127.0.0.1:{self.server.gateway_port}/page"},
            )
            return
        if self.path == "/to-metadata":
            self._send(302, "text/plain", b"", {"Location": "http://169.254.169.254/latest/meta-data/"})
            return
        if self.path == "/to-safe":
            self._send(302, "text/plain", b"", {"Location": f"http://safe.test:{port}/page"})
            return
        self._send(404, "text/plain", b"nope")


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.request_paths = []
    srv.request_headers = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


@pytest.fixture
def gateway_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.request_paths = []
    srv.request_headers = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _random_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# LLM: 组件测试只连本文件创建的随机端口服务；Gateway 端口后备必须保留调用者显式传入的 0。
# 函数用途: 构造启用私网测试授权并固定解析 IP 的 web_fetch 测试工具。
def _tool(gateway_port: int | None = None) -> WebFetchTool:
    # resolver 把所有测试 host 解析到本机测试服务器;allow_private 让首跳可达本机(metadata 仍 ALWAYS_BLOCKED)
    return WebFetchTool(
        max_chars=10000,
        timeout=10,
        resolver=lambda _h: ("127.0.0.1",),
        allow_private_resolution=True,
        gateway_endpoint_config=GatewayEndpointConfig(
            configured_port=_random_port() if gateway_port is None else gateway_port,
        ),
    )


def test_normal_fetch_pins_to_resolved_ip(server) -> None:
    port = server.server_address[1]
    result = _tool().execute({"url": f"http://app.test:{port}/page", "format": "text"})
    assert result.ok is True  # pin 到网关校验过的 127.0.0.1 → 连到真服务器拿到响应
    assert "Hello world" in result.output
    header_names = {name.casefold() for name in server.request_headers[-1]}
    assert "x-gateway-token" not in header_names
    assert "authorization" not in header_names


def test_redirect_to_cloud_metadata_blocked(server) -> None:
    port = server.server_address[1]
    result = _tool().execute({"url": f"http://app.test:{port}/to-metadata", "format": "text"})
    assert result.ok is False  # 302 → 169.254.169.254 被重新过网关拦下,不盲目跟随到云 metadata
    assert result.error_code == "NETWORK_ALWAYS_BLOCKED_IP"


def test_redirect_to_safe_target_followed(server) -> None:
    port = server.server_address[1]
    result = _tool().execute({"url": f"http://app.test:{port}/to-safe", "format": "text"})
    assert result.ok is True  # 安全目标的重定向重新过网关后仍正常跟随
    assert "Hello world" in result.output


@pytest.mark.parametrize(
    "endpoint",
    (GatewayEndpointConfig(), GatewayEndpointConfig(configured_port=0)),
)
def test_public_target_allowed_when_gateway_port_is_absent_or_disabled(endpoint) -> None:
    error = _network_safety_error(
        "web_fetch",
        "https://public.example.test/data",
        lambda _host: ("93.184.216.34",),
        NetworkSafetySettings(gateway_endpoint=endpoint),
    )

    assert error is None


def test_invalid_gateway_port_only_rejects_local_resolved_address() -> None:
    settings = NetworkSafetySettings(
        allow_private_resolution=True,
        gateway_endpoint=GatewayEndpointConfig(configured_port="invalid"),
    )
    public = _network_safety_error(
        "web_fetch", "https://public.example.test/data", lambda _host: ("93.184.216.34",), settings
    )
    local = _network_safety_error(
        "web_fetch", "http://127.0.0.1:45123/data", lambda _host: ("127.0.0.1",), settings
    )

    assert public is None
    assert local is not None
    assert local.error_code == "NETWORK_GATEWAY_PORT_UNAVAILABLE"


def test_effective_gateway_ports_combine_g4_registry_and_config(monkeypatch, gateway_server) -> None:
    from agent_py_agent.agent.attempt import sandbox

    configured_port = _random_port()
    runtime_port = int(gateway_server.server_address[1])
    monkeypatch.setattr(sandbox, "gateway_bound_ports", lambda: (runtime_port,))

    ports, state, source = _effective_gateway_endpoint(
        GatewayEndpointConfig(configured_port=configured_port)
    )

    assert ports == tuple(sorted({runtime_port, configured_port}))
    assert state == "known"
    assert source == "registry+config"


def test_gateway_port_zero_and_absent_are_not_applicable(monkeypatch) -> None:
    from agent_py_agent.agent.attempt import sandbox

    monkeypatch.setattr(sandbox, "gateway_bound_ports", lambda: ())
    for endpoint in (GatewayEndpointConfig(), GatewayEndpointConfig(configured_port=0)):
        ports, state, _source = _effective_gateway_endpoint(endpoint)
        assert ports == ()
        assert state == "not_applicable"


def test_non_loopback_local_address_is_detected_by_bind_probe(monkeypatch) -> None:
    from agent_py_agent.agent.tooling import web

    class LocalBind:
        def bind(self, _address) -> None:
            return None

        def close(self) -> None:
            return None

    attempts = []
    monkeypatch.setattr(web.socket, "socket", lambda *args: (attempts.append(args), LocalBind())[1])
    settings = NetworkSafetySettings(
        allow_private_resolution=True,
        gateway_endpoint=GatewayEndpointConfig(configured_port=45123),
    )
    first = _network_safety_error(
        "web_fetch",
        "http://public.example.test:45123/data",
        lambda _host: ("192.0.2.44",),
        settings,
    )
    second = _network_safety_error(
        "web_fetch",
        "http://public.example.test:45123/data",
        lambda _host: ("192.0.2.44",),
        settings,
    )

    assert first is not None and first.error_code == "NETWORK_GATEWAY_LOCAL_PORT_BLOCKED"
    assert second is not None and second.error_code == "NETWORK_GATEWAY_LOCAL_PORT_BLOCKED"
    assert len(attempts) == 2


def test_bind_probe_eaddrnotavail_marks_address_nonlocal(monkeypatch) -> None:
    from agent_py_agent.agent.tooling import web

    class NonLocalBind:
        def bind(self, _address) -> None:
            raise OSError(errno.EADDRNOTAVAIL, "not assigned")

        def close(self) -> None:
            return None

    attempts = []
    monkeypatch.setattr(web.socket, "socket", lambda *args: (attempts.append(args), NonLocalBind())[1])
    error = _network_safety_error(
        "web_fetch",
        "http://public.example.test:45123/data",
        lambda _host: ("192.0.2.44",),
        NetworkSafetySettings(
            allow_private_resolution=True,
            gateway_endpoint=GatewayEndpointConfig(configured_port=45123),
        ),
    )

    assert error is None
    assert len(attempts) == 1


def test_other_bind_error_is_unknown_and_denied_only_at_gateway_port(monkeypatch) -> None:
    from agent_py_agent.agent.tooling import web

    class UnknownBind:
        def bind(self, _address) -> None:
            raise OSError(errno.EACCES, "probe unavailable")

        def close(self) -> None:
            return None

    attempts = []
    monkeypatch.setattr(web.socket, "socket", lambda *args: (attempts.append(args), UnknownBind())[1])
    settings = NetworkSafetySettings(
        allow_private_resolution=True,
        gateway_endpoint=GatewayEndpointConfig(configured_port=45123),
    )
    matching = _network_safety_error(
        "web_fetch",
        "http://public.example.test:45123/data",
        lambda _host: ("192.0.2.44",),
        settings,
    )
    other_port = _network_safety_error(
        "web_fetch",
        "http://public.example.test:45124/data",
        lambda _host: ("192.0.2.44",),
        settings,
    )

    assert matching is not None
    assert matching.error_code == "NETWORK_GATEWAY_LOCAL_ADDRESS_UNAVAILABLE"
    assert other_port is None
    assert len(attempts) == 1


@pytest.mark.parametrize(
    "host",
    ("127.0.0.1", "localhost", "[::1]", "[::ffff:127.0.0.1]", "0.0.0.0"),
)
def test_gateway_local_targets_are_blocked_even_when_private_is_allowed(
    gateway_server, host: str
) -> None:
    port = gateway_server.server_address[1]

    result = _tool(gateway_port=port).execute(
        {"url": f"http://{host}:{port}/page", "format": "text"}
    )

    assert result.ok is False
    assert result.error_code == "NETWORK_GATEWAY_LOCAL_PORT_BLOCKED"
    assert "不能访问本机 Gateway" in result.output
    assert gateway_server.request_paths == []


def test_redirect_to_gateway_port_is_rechecked_and_blocked(server, gateway_server) -> None:
    server.gateway_port = gateway_server.server_address[1]
    result = _tool(gateway_port=server.gateway_port).execute(
        {"url": f"http://app.test:{server.server_address[1]}/to-gateway", "format": "text"}
    )

    assert result.ok is False
    assert result.error_code == "NETWORK_GATEWAY_LOCAL_PORT_BLOCKED"
    assert server.request_paths == ["/to-gateway"]
    assert gateway_server.request_paths == []


def test_resolve_pin_returns_validated_ip_or_error() -> None:
    tool = _tool()
    ok = tool._resolve_pin("http://app.test/page")
    assert ok.ip == "127.0.0.1" and ok.error is None  # 放行 → 返回校验过的固定 IP
    blocked = tool._resolve_pin("http://169.254.169.254/latest/meta-data/")
    assert blocked.ip is None and blocked.error is not None  # 拒 → 带错误,无 IP 可 pin


# LLM: 字面主机不能被注入解析器替换，真正连接必须 pin 到被审计的 canonical IP。
# 函数用途: 测 web_fetch 与 watch_stream 的字面 IP pin 一致性。
@pytest.mark.parametrize(("host", "expected"), [("127.0.0.1", "127.0.0.1"), ("[::ffff:127.0.0.1]", "127.0.0.1"), ("2130706433", "127.0.0.1"), ("[::1]", "::1")])
def test_literal_pin_does_not_consult_resolver(monkeypatch, host, expected):
    from types import SimpleNamespace

    from agent_py_agent.agent.ingestion import watch_tool

    calls = []

    # LLM: 解析器故意返回与字面主机不同的地址，验证生产代码不会采用它。
    # 函数用途: 记录不应发生的字面地址解析。
    def resolver(value):
        calls.append(value)
        return ("10.20.30.40",)
    tool = _tool(gateway_port=9000)
    tool.resolver = resolver
    monkeypatch.setattr(watch_tool, "_default_network_resolver", resolver)
    watch = SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(gateway_port=9000)))
    url = f"http://{host}:8000/page"
    for pin in (tool._resolve_pin(url), watch_tool._resolve_watch_pin(watch, url, (), True)):
        assert pin.error is None
        assert pin.ip == expected
    assert calls == []


# LLM: 复制 handler 共享缓存不能共享旧授权；两种撤权都要拒绝已有私网响应。
# 函数用途: 用真实回环 HTTP 响应复现浅拷贝缓存越权。
@pytest.mark.parametrize("grant", ["hosts", "toggle"])
def test_shallow_copy_cache_cannot_reuse_revoked_private_grant(server, tmp_path, grant):
    tool = WebFetchTool(
        max_chars=10000, timeout=5, artifact_root=tmp_path,
        resolver=lambda _host: ("127.0.0.1",),
        allowed_private_hosts=("127.0.0.1",) if grant == "hosts" else (),
        allow_private_resolution=grant == "toggle",
        gateway_endpoint_config=GatewayEndpointConfig(configured_port=9000),
    )
    params = {"url": f"http://127.0.0.1:{server.server_address[1]}/page", "format": "text"}
    assert tool.execute(params).ok
    restricted = copy(tool)
    restricted.allowed_private_hosts = ()
    restricted.allow_private_resolution = False
    result = restricted.execute(params)
    assert not result.ok
    assert result.error_code == "NETWORK_PRIVATE_HOST_BLOCKED"
    assert server.request_paths == ["/page"]


# LLM: 缓存键相同也须重查动态端口事实；不能拿撤销前的公网/私网检查代替本次授权。
# 函数用途: 测缓存命中后新成为 Gateway 的端口会被拦下。
def test_cache_hit_rechecks_current_gateway_port(server, tmp_path, monkeypatch):
    from agent_py_agent.agent.attempt import sandbox

    tool = _tool(gateway_port=9000)
    tool.artifact_root = tmp_path
    params = {"url": f"http://127.0.0.1:{server.server_address[1]}/page"}
    monkeypatch.setattr(sandbox, "gateway_bound_ports", lambda: ())
    assert tool.execute(params).ok
    monkeypatch.setattr(sandbox, "gateway_bound_ports", lambda: (server.server_address[1],))
    result = tool.execute(params)
    assert result.error_code == "NETWORK_GATEWAY_LOCAL_PORT_BLOCKED"
    assert server.request_paths == ["/page"]


# LLM: 配置与本次 watch 授权取并集，但 IP 网段放行不影响 Gateway 或元数据。
# 函数用途: 覆盖两条网络入口的精确域名、通配符、IP 与 CIDR 格式。
@pytest.mark.parametrize(("host", "entries", "allowed"), [
    ("git.corp.local", ("git.corp.local",), True),
    ("api.corp.local", ("*.corp.local",), True),
    ("corp.local", ("*.corp.local",), False),
    ("evilcorp.local", ("*.corp.local",), False),
    ("api.local", ("*.local",), False),
    ("127.0.0.1", ("127.0.0.1",), True),
    ("localhost", ("127.0.0.0/8",), True),
    ("192.168.0.50", ("192.168.0.0/24",), True),
    ("10.2.3.4", ("10.0.0.0/8",), True),
])
def test_web_and_watch_share_private_allowlist(monkeypatch, host, entries, allowed):
    from types import SimpleNamespace

    from agent_py_agent.agent.ingestion import watch_tool

    # LLM: 域名测试只用固定私网答案，不发真实 DNS 请求；非localhost域名解析到内网IP，localhost解析到回环IP。
    # 函数用途: 为两个真实安全入口提供同一解析事实。
    def resolver(target_host):
        return ("127.0.0.1",) if target_host == "localhost" else ("10.1.2.3",)
    tool = WebFetchTool(max_chars=1000, timeout=5, resolver=resolver,
                        allowed_private_hosts=entries, allow_private_resolution=False)
    monkeypatch.setattr(watch_tool, "_default_network_resolver", resolver)
    config = SimpleNamespace(allowed_private_hosts=entries, gateway_port=None)
    watch = SimpleNamespace(agent=SimpleNamespace(config=config))
    settings = watch_tool._watch_network_safety_settings(watch, ("other.corp.local",), False)
    assert settings.allowed_private_hosts == (*entries, "other.corp.local")
    url = f"http://{host}:8000/page"
    for pin in (tool._resolve_pin(url), watch_tool._resolve_watch_pin(watch, url, (), False)):
        assert (pin.error is None) is allowed


# LLM: 构造接线不能先把裸字符串拆开或把整数转成授权 IP，破坏共享解析器的严格输入规则。
# 函数用途: 测直接 web 构造及 watch 配置的无效授权输入不产生私网许可。
@pytest.mark.parametrize("entries", ["0.0.0.0", [0], {"0.0.0.0": True}])
def test_network_constructors_reject_malformed_private_grants(monkeypatch, entries):
    from types import SimpleNamespace

    from agent_py_agent.agent.ingestion import watch_tool

    tool = WebFetchTool(max_chars=1000, timeout=5, allowed_private_hosts=entries,
                        allow_private_resolution=False)
    watch = SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(allowed_private_hosts=entries)))
    for pin in (tool._resolve_pin("http://0.0.0.0:8000/"),
                watch_tool._resolve_watch_pin(watch, "http://0.0.0.0:8000/", (), False)):
        assert pin.error is not None
        assert pin.error.error_code == "NETWORK_PRIVATE_HOST_BLOCKED"


# LLM: 缓存重定向的实际响应地址也须重查端口；首跳仍安全不能证明最终目标仍获授权。
# 函数用途: 测重定向后新保护端口不能从首跳缓存里读取。
def test_cached_redirect_rechecks_response_endpoint(server, tmp_path, monkeypatch):
    from agent_py_agent.agent.attempt import sandbox
    from agent_py_agent.agent.tooling import web

    tool = _tool(gateway_port=9000)
    tool.artifact_root = tmp_path
    params = {"url": f"http://app.test:{server.server_address[1]}/to-safe"}
    monkeypatch.setattr(sandbox, "gateway_bound_ports", lambda: ())
    assert tool.execute(params).ok
    assert server.request_paths == ["/to-safe", "/page"]
    original = tool.network_safety_error

    # LLM: 仅用宿主端点配置变化模拟最终主机新成为 Gateway；不改缓存或响应正文。
    # 函数用途: 让重定向目标在第二次读取时按统一网络门受保护。
    def current_endpoint(name, url, resolver, settings):
        if "safe.test" in url:
            settings = NetworkSafetySettings(allow_private_resolution=True,
                gateway_endpoint=GatewayEndpointConfig(configured_port=server.server_address[1]))
        return original(name, url, resolver, settings)

    monkeypatch.setattr(tool, "network_safety_error", current_endpoint)
    monkeypatch.setattr(web, "_probe_local_address", lambda _ip: True)
    result = tool.execute(params)
    assert result.error_code == "NETWORK_GATEWAY_LOCAL_PORT_BLOCKED"
    assert server.request_paths == ["/to-safe", "/page"]


# LLM: 无端口注册、关闭 HTTP 或改成9000均不能取消8420；两入口、总开关及白名单路径一致。
# 函数用途: 覆盖实际设置装配后的默认 Gateway 端口保护矩阵。
@pytest.mark.parametrize("configured", [None, 0, 9000])
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]", "[::ffff:127.0.0.1]", "0.0.0.0"])
@pytest.mark.parametrize("allow_resolution", [False, True])
def test_default_gateway_port_survives_all_configurations(monkeypatch, configured, host, allow_resolution):
    from types import SimpleNamespace

    from agent_py_agent.agent.attempt import sandbox
    from agent_py_agent.agent.ingestion import watch_tool

    hosts = ("127.0.0.1", "localhost", "::1", "0.0.0.0", "127.0.0.0/8", "0.0.0.0/0")
    monkeypatch.setattr(sandbox, "gateway_bound_ports", lambda: ())
    monkeypatch.setattr(watch_tool, "_default_network_resolver", lambda _host: ("127.0.0.1",))
    endpoint = GatewayEndpointConfig(configured_port=configured)
    settings = NetworkSafetySettings(allowed_private_hosts=hosts, allow_private_resolution=allow_resolution,
                                     gateway_endpoint=endpoint)
    url = f"http://{host}:8420/"
    error = _network_safety_error("web_fetch", url, lambda _host: ("127.0.0.1",), settings)
    config = SimpleNamespace(allowed_private_hosts=hosts, gateway_port=configured)
    watch = SimpleNamespace(agent=SimpleNamespace(config=config))
    pin = watch_tool._resolve_watch_pin(watch, url, (), allow_resolution)
    assert error is not None and error.error_code == "NETWORK_GATEWAY_LOCAL_PORT_BLOCKED"
    assert pin.error is not None and pin.error.error_code == "NETWORK_GATEWAY_LOCAL_PORT_BLOCKED"


# LLM: 以真正配置/Agent/Registry/预检/HTTP传输验证默认授权接线，禁用环境私网总开关。
# 函数用途: 证明 CIDR 配置不用逐调用 grant 也能经真实抓取入口读取授权内网。
def test_configured_cidr_flows_through_agent_and_registry(server, tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call

    monkeypatch.delenv("MY_AGENT_ALLOW_PRIVATE_URLS", raising=False)
    config = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), enable_plugins=False,
                         gateway_port=9000, allowed_private_hosts=["127.0.0.0/8"])
    agent = SimpleAgent(config, tmp_path / "project")
    handler = agent.tools.tools["web_fetch"]
    assert handler.allowed_private_hosts == ("127.0.0.0/8",)
    result = execute_registry_test_call(agent.tools, "web_fetch", {
        "url": f"http://127.0.0.1:{server.server_address[1]}/page", "mode": "text",
    }, register_with=agent)
    assert result.ok, result.output
    assert "Hello world" in result.output
    assert server.request_paths == ["/page"]


# LLM: 非法端口在传输前拒绝，原始 NETWORK_PORT_INVALID 留在 reported/gate，控制流用既有参数错误分类。
# 函数用途: 测两入口不会把 :0 偷换成 :80，也不把可修的非法端口误报为未知错误。
@pytest.mark.parametrize("port", ["0", "-1", "65536", "bad"])
def test_explicit_zero_port_is_rejected_before_transport(port):
    from types import SimpleNamespace

    from agent_py_agent.agent.ingestion import watch_tool

    url = f"http://127.0.0.1:{port}/"
    tool = _tool(gateway_port=9000)
    watch = SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(gateway_port=9000)))
    result = tool.execute({"url": url})
    assert not result.ok
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert result.reported_error_code == "NETWORK_PORT_INVALID"
    assert result.result_envelope["network_safety_gate"]["findings"][0]["code"] == "NETWORK_PORT_INVALID"
    pin = watch_tool._resolve_watch_pin(watch, url, (), True)
    assert pin.error is not None and pin.error.error_code == "TOOL_INVALID_ARGUMENTS"
    assert pin.error.reported_error_code == "NETWORK_PORT_INVALID"


# LLM: extract模式必须共享同一resolved_by_host以拦截跨URL的公网转私网DNS重绑定，且临时上下文恢复不得固化动态provider旧快照。
# 函数用途: 锁住extract模式的DNS重绑定防护与动态私网白名单热更新恢复。
def test_extract_pins_resolved_ips_and_preserves_dynamic_provider(server):
    from agent_py_agent.agent.tooling.registry_invoke import _execute_with_temporary_tool_context

    answers = iter([("93.184.216.34",), ("127.0.0.1",)])
    live_hosts = ["10.0.0.0/8"]
    tool = WebFetchTool(
        max_chars=10000,
        timeout=10,
        allow_private_resolution=True,
        allowed_private_hosts=lambda: list(live_hosts),
        resolver=lambda _host: next(answers),
        gateway_endpoint_config=GatewayEndpointConfig(configured_port=9000),
    )
    port = server.server_address[1]
    result = _execute_with_temporary_tool_context(
        tool,
        workspace_roots=(),
        allowed_private_hosts=("192.168.1.10",),
        allow_private_resolution=None,
        callback=lambda: tool.execute(
            {
                "urls": [
                    f"http://rebind.corp.local:{port}/page",
                    f"http://rebind.corp.local:{port}/page2",
                ],
                "mode": "extract",
            }
        ),
    )
    assert result.result_envelope["failures"][-1]["error_code"] == "NETWORK_DNS_REBINDING_BLOCKED"
    live_hosts.clear()
    assert tool.allowed_private_hosts == ()
