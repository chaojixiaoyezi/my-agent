
from __future__ import annotations

"""HTTP service for gateway using standard library http.server.

这个文件实现 gateway 的 HTTP 接口：POST /ask、GET /result/<id>、GET /status、POST /stop。
用标准库 http.server + threading 实现并发。
支持多租户鉴权：外部通道请求需要 X-User-Id / X-Channel header。
"""

import json
import os
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Optional

from ..runtime_errors import runtime_error_report
from .http_handlers import (
    handle_admin_summary,
    handle_ask,
    handle_result,
    handle_session_bind,
    handle_session_channels,
    handle_status,
    handle_stop,
)
from .io import update_json_file_atomic

if TYPE_CHECKING:
    from ..agent.core import SimpleAgent
    from ..auth.middleware import AuthMiddleware
    from ..session.admin_query import AdminCrossChannelQuery
    from ..session.cross_channel import CrossChannelSession
    from .paths import GatewayPaths


# Global server instance for signal handler access
_server_instance: GatewayHTTPServer | None = None


@dataclass(frozen=True)
class GatewayHTTPServerParams:
    cross_channel: CrossChannelSession | None = None
    admin_query: AdminCrossChannelQuery | None = None
    auth_middleware: AuthMiddleware | None = None
    bind_host: str = "127.0.0.1"  # 默认仅本机可达;暴露到网络须配鉴权(见 _guard_network_exposure)


# 回环地址:仅本机可达。空串/0.0.0.0/::/LAN IP 一律判非回环(=暴露到网络,须鉴权)。默认仅监听回环地址。
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"}


def _is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower().strip("[]")
    return h in _LOOPBACK_HOSTS or h.startswith("127.")


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
        return json.loads(body.decode("utf-8", "replace"))

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
        *,
        params: GatewayHTTPServerParams | None = None,
        cross_channel: CrossChannelSession | None = None,
        admin_query: AdminCrossChannelQuery | None = None,
        auth_middleware: AuthMiddleware | None = None,
    ):
        server_params = params or GatewayHTTPServerParams(cross_channel, admin_query, auth_middleware)
        self.port = port
        self.paths = paths
        self.cross_channel = server_params.cross_channel
        self.admin_query = server_params.admin_query
        self.auth_middleware = server_params.auth_middleware
        self.bind_host = server_params.bind_host
        self.server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.last_error_report: dict[str, Any] | None = None

    def _guard_network_exposure(self) -> None:
        """fail-closed:绑非 loopback(暴露到网络)却没接鉴权中间件时拒绝启动,杜绝未认证远程入口。"""
        if not _is_loopback_host(self.bind_host) and self.auth_middleware is None:
            raise RuntimeError(
                f"网关拒绝启动:bind_host={self.bind_host!r} 非回环(暴露到网络)却未配置鉴权。"
                "请置 auth_enabled=True,或把 gateway_bind_host 设回 127.0.0.1。"
            )

    def start(self) -> None:
        self._guard_network_exposure()  # fail-closed 必须先于任何全局副作用(拒绝时不污染 _server_instance)
        global _server_instance
        _server_instance = self
        self.last_error_report = None

        self.server = ThreadingHTTPServer((self.bind_host, self.port), GatewayHTTPHandler)
        self.server.server_version = "MyAgentGateway/1.0"
        self.server.handler_class = GatewayHTTPHandler

        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        if self.server is None:
            return
        try:
            self.server.serve_forever()
        except Exception as exc:
            self._record_serve_error(exc)

    def _record_serve_error(self, exc: BaseException) -> None:
        report = runtime_error_report(exc, context="gateway.http_server.serve")
        self.last_error_report = report

        def update_state(current: dict) -> dict:
            updated = dict(current)
            updated["status"] = "http_server_error"
            updated["server_error"] = report
            updated["updated_at"] = time.time()
            return updated

        try:
            update_json_file_atomic(self.paths.state, update_state)
        except Exception as persist_exc:
            self.last_error_report = {
                **report,
                "state_persist_error": runtime_error_report(
                    persist_exc,
                    context="gateway.http_server.state.write",
                ),
            }

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
    *,
    params: GatewayHTTPServerParams | None = None,
    cross_channel: CrossChannelSession | None = None,
    admin_query: AdminCrossChannelQuery | None = None,
    auth_middleware: AuthMiddleware | None = None,
) -> GatewayHTTPServer:
    server = GatewayHTTPServer(
        port,
        paths,
        params=params or GatewayHTTPServerParams(cross_channel, admin_query, auth_middleware),
    )
    server.start()
    return server
