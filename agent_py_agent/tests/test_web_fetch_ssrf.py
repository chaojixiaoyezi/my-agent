"""审计 #3 修复真测:web_fetch SSRF 加固——连接 pin 到网关校验过的 IP + 每个重定向跳重新过网关。

真起本地 HTTP server:正常抓取经 pin 连到校验过的回环 IP 拿到响应(证明连的就是网关校验的 IP);
302 跳转到 169.254.169.254(云 metadata)被重新过网关拦下(不再盲目跟随到内网);302 跳转到安全目标仍跟随。
学 工具运行时 受控请求。
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_py_agent.agent.tooling.web import WebFetchTool


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
        port = self.server.server_address[1]
        if self.path == "/page":
            self._send(200, "text/html; charset=utf-8", b"<html><body><h1>Hi</h1><p>Hello world</p></body></html>")
        elif self.path == "/to-metadata":
            self._send(302, "text/plain", b"", {"Location": "http://169.254.169.254/latest/meta-data/"})
        elif self.path == "/to-safe":
            self._send(302, "text/plain", b"", {"Location": f"http://safe.test:{port}/page"})
        else:
            self._send(404, "text/plain", b"nope")


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _tool() -> WebFetchTool:
    # resolver 把所有测试 host 解析到本机测试服务器;allow_private 让首跳可达本机(metadata 仍 ALWAYS_BLOCKED)
    return WebFetchTool(max_chars=10000, timeout=10, resolver=lambda _h: ("127.0.0.1",), allow_private_resolution=True)


def test_normal_fetch_pins_to_resolved_ip(server) -> None:
    port = server.server_address[1]
    result = _tool().execute({"url": f"http://app.test:{port}/page", "format": "text"})
    assert result.ok is True  # pin 到网关校验过的 127.0.0.1 → 连到真服务器拿到响应
    assert "Hello world" in result.output


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


def test_resolve_pin_returns_validated_ip_or_error() -> None:
    tool = _tool()
    ok = tool._resolve_pin("http://app.test/page")
    assert ok.ip == "127.0.0.1" and ok.error is None  # 放行 → 返回校验过的固定 IP
    blocked = tool._resolve_pin("http://169.254.169.254/latest/meta-data/")
    assert blocked.ip is None and blocked.error is not None  # 拒 → 带错误,无 IP 可 pin
