from __future__ import annotations

"""LLM: HTTP service for gateway using standard library http.server.

给人看的解释：
这个文件实现 gateway 的 HTTP 接口：POST /ask、GET /result/<id>、GET /status、POST /stop。
用标准库 http.server + threading 实现并发。
支持多租户鉴权：外部通道请求需要 X-User-Id / X-Channel header。
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Optional

from .http_handlers import (
    handle_admin_summary,
    handle_ask,
    handle_result,
    handle_session_bind,
    handle_session_channels,
    handle_status,
    handle_stop,
)

if TYPE_CHECKING:
    from ..agent.core import SimpleAgent
    from ..auth.middleware import AuthMiddleware
    from ..session.admin_query import AdminCrossChannelQuery
    from ..session.cross_channel import CrossChannelSession
    from .paths import GatewayPaths


# Global server instance for signal handler access
_server_instance: GatewayHTTPServer | None = None


def _generate_request_id() -> str:
    return f"req_{int(time.time() * 1000)}_{os.getpid()}"


class GatewayHTTPHandler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _inject_auth_middleware(self) -> None:
        server = _server_instance
        if server is not None and server.auth_middleware is not None:
            self._auth_middleware = server.auth_middleware

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        body_str = json.dumps(body, ensure_ascii=False)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_str.encode("utf-8"))))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body_str.encode("utf-8"))

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        return json.loads(body.decode("utf-8"))

    def do_GET(self) -> None:
        self._inject_auth_middleware()
        if self.path == "/status":
            self._handle_status()
            return
        if self.path.startswith("/result/"):
            self._handle_result()
            return
        if self.path.startswith("/sessions/") and self.path.endswith("/channels"):
            self._handle_session_channels()
            return
        if self.path == "/admin/summary":
            self._handle_admin_summary()
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        self._inject_auth_middleware()
        if self.path == "/ask":
            self._handle_ask()
            return
        if self.path == "/stop":
            self._handle_stop()
            return
        if self.path.startswith("/sessions/") and self.path.endswith("/bind"):
            self._handle_session_bind()
            return
        self._send_json(404, {"error": "not found"})

    def _handle_status(self) -> None:
        handle_status(self, _server_instance)

    def _handle_result(self) -> None:
        handle_result(self, _server_instance)

    def _handle_ask(self) -> None:
        handle_ask(self, _server_instance, _generate_request_id)

    def _handle_stop(self) -> None:
        handle_stop(self, _server_instance)

    def _handle_session_channels(self) -> None:
        handle_session_channels(self, _server_instance)

    def _handle_session_bind(self) -> None:
        handle_session_bind(self, _server_instance)

    def _handle_admin_summary(self) -> None:
        handle_admin_summary(self, _server_instance)


class GatewayHTTPServer:

    def __init__(
        self,
        port: int,
        paths: GatewayPaths,
        cross_channel: CrossChannelSession | None = None,
        admin_query: AdminCrossChannelQuery | None = None,
        auth_middleware: AuthMiddleware | None = None,
    ):
        self.port = port
        self.paths = paths
        self.cross_channel = cross_channel
        self.admin_query = admin_query
        self.auth_middleware = auth_middleware
        self.server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        global _server_instance
        _server_instance = self

        self.server = ThreadingHTTPServer(("", self.port), GatewayHTTPHandler)
        self.server.server_version = "MyAgentGateway/1.0"
        self.server.handler_class = GatewayHTTPHandler

        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        if self.server is None:
            return
        try:
            self.server.serve_forever()
        except Exception:
            pass

    def stop(self, timeout: float = 5.0) -> None:
        global _server_instance
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None
        _server_instance = None


def start_http_server(
    port: int,
    paths: GatewayPaths,
    cross_channel: CrossChannelSession | None = None,
    admin_query: AdminCrossChannelQuery | None = None,
    auth_middleware: AuthMiddleware | None = None,
) -> GatewayHTTPServer:
    server = GatewayHTTPServer(port, paths, cross_channel, admin_query, auth_middleware)
    server.start()
    return server
