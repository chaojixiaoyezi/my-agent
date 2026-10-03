"""审计 #3 修复真测:web_fetch SSRF 加固——连接 pin 到网关校验过的 IP + 每个重定向跳重新过网关。

真起本地 HTTP server:正常抓取经 pin 连到校验过的回环 IP 拿到响应(证明连的就是网关校验的 IP);
302 跳转到 169.254.169.254(云 metadata)被重新过网关拦下(不再盲目跟随到内网);302 跳转到安全目标仍跟随。
学 工具运行时 受控请求。
"""

from __future__ import annotations

import errno
import socket
import threading
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
