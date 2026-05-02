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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..agent.core import SimpleAgent
    from ..auth.middleware import AuthMiddleware
    from .paths import GatewayPaths
    from ..session.cross_channel import CrossChannelSession
    from ..session.admin_query import AdminCrossChannelQuery


# Global server instance for signal handler access
_server_instance: Optional["GatewayHTTPServer"] = None


def _generate_request_id() -> str:
    """Generate a unique request ID."""
    return f"req_{int(time.time() * 1000)}_{os.getpid()}"


class GatewayHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for gateway。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        """Override to reduce noise。"""
        pass

    def _inject_auth_middleware(self) -> None:
        """每个请求进来时，从 server 注入 auth_middleware 到 handler 实例。"""
        server = _server_instance
        if server is not None and server.auth_middleware is not None:
            self._auth_middleware = server.auth_middleware

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        """Send JSON response."""
        body_str = json.dumps(body, ensure_ascii=False)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_str.encode("utf-8"))))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body_str.encode("utf-8"))

    def _read_json(self) -> dict[str, Any]:
        """Read JSON body from request."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        return json.loads(body.decode("utf-8"))

    def do_GET(self) -> None:
        """Handle GET requests。"""
        self._inject_auth_middleware()
        if self.path == "/status":
            self._handle_status()
        elif self.path.startswith("/result/"):
            self._handle_result()
        elif self.path.startswith("/sessions/") and self.path.endswith("/channels"):
            self._handle_session_channels()
        elif self.path == "/admin/summary":
            self._handle_admin_summary()
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        """Handle POST requests。"""
        self._inject_auth_middleware()
        if self.path == "/ask":
            self._handle_ask()
        elif self.path == "/stop":
            self._handle_stop()
        elif self.path.startswith("/sessions/") and self.path.endswith("/bind"):
            self._handle_session_bind()
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_status(self) -> None:
        """GET /status - return gateway status."""
        server = _server_instance
        if server is None:
            self._send_json(500, {"error": "server not initialized"})
            return

        paths = server.paths
        state = {}
        if paths.state.exists():
            try:
                state = json.loads(paths.state.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        counts = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
        for name, dir_path in [
            ("pending", paths.inbox),
            ("processing", paths.processing),
            ("done", paths.done),
            ("failed", paths.failed),
        ]:
            if dir_path.exists():
                counts[name] = len(list(dir_path.iterdir()))

        response = {
            "status": state.get("status", "unknown"),
            "pid": state.get("pid"),
            "uptime": time.time() - state.get("started_at", time.time()),
            "requests": counts,
        }
        self._send_json(200, response)

    def _handle_result(self) -> None:
        """GET /result/<request_id> - return request result。"""
        request_id = self.path[len("/result/"):]
        server = _server_instance
        if server is None:
            self._send_json(500, {"error": "server not initialized"})
            return

        # 从 header 提取当前用户身份
        mw = getattr(self, "_auth_middleware", None)
        if mw is not None:
            user_id, _ = mw.extract_identity(dict(self.headers))
            permission = mw.get_permission(dict(self.headers))
        else:
            user_id = "admin"
            permission = None

        # Check responses directory
        response_path = server.paths.responses / f"{request_id}.json"
        if not response_path.exists():
            # Check if still processing
            processing_path = server.paths.processing / f"{request_id}.json"
            if processing_path.exists():
                # 读取 processing 文件检查 user_id
                try:
                    proc_data = json.loads(processing_path.read_text(encoding="utf-8"))
                    req_user = proc_data.get("user_id", "")
                    if permission and not permission.can_access_all_users and req_user != user_id:
                        self._send_json(403, {"error": "forbidden", "request_id": request_id})
                        return
                except (json.JSONDecodeError, OSError):
                    pass
                self._send_json(202, {"status": "processing", "request_id": request_id})
                return
            # Check if queued in inbox
            inbox_path = server.paths.inbox / f"{request_id}.json"
            if inbox_path.exists():
                try:
                    inbox_data = json.loads(inbox_path.read_text(encoding="utf-8"))
                    req_user = inbox_data.get("user_id", "")
                    if permission and not permission.can_access_all_users and req_user != user_id:
                        self._send_json(403, {"error": "forbidden", "request_id": request_id})
                        return
                except (json.JSONDecodeError, OSError):
                    pass
                self._send_json(202, {"status": "queued", "request_id": request_id})
                return
            self._send_json(404, {"error": "not found", "request_id": request_id})
            return

        # 读取响应文件，检查 user_id 是否匹配
        try:
            result = json.loads(response_path.read_text(encoding="utf-8"))
            req_user = result.get("user_id", result.get("metadata", {}).get("user_id", ""))
            if permission and not permission.can_access_all_users and req_user != user_id:
                self._send_json(403, {"error": "forbidden", "request_id": request_id})
                return
            self._send_json(200, result)
        except (json.JSONDecodeError, OSError) as e:
            self._send_json(500, {"error": f"failed to read result: {e}"})

    def _handle_ask(self) -> None:
        """POST /ask - submit a new request。"""
        try:
            body = self._read_json()
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"invalid JSON: {e}"})
            return

        # 支持 kind=ask + prompt（adapter 用） 或 goal（兼容旧格式）
        kind = body.get("kind", "ask")
        if kind != "ask":
            self._send_json(400, {"error": f"unsupported kind: {kind}"})
            return

        prompt = body.get("prompt", "")
        goal = body.get("goal", prompt)
        if not goal:
            self._send_json(400, {"error": "goal is required"})
            return

        server = _server_instance
        if server is None:
            self._send_json(500, {"error": "server not initialized"})
            return

        # 从 header 提取 user_id 和 channel，纳入 metadata
        mw = getattr(self, "_auth_middleware", None)
        if mw is not None:
            user_id, channel = mw.extract_identity(dict(self.headers))
        else:
            user_id, channel = "admin", "chat"

        # Generate request ID and write to pending queue
        request_id = _generate_request_id()
        metadata = body.get("metadata", {})
        metadata["user_id"] = user_id
        metadata["channel"] = channel
        request_data = {
            "id": request_id,
            "request_id": request_id,
            "kind": "ask",
            "goal": goal,
            "metadata": metadata,
            "submitted_at": time.time(),
            "user_id": user_id,
        }
        pending_path = server.paths.inbox / f"{request_id}.json"
        try:
            pending_path.write_text(
                json.dumps(request_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            self._send_json(500, {"error": f"failed to write request: {e}"})
            return

        self._send_json(202, {"request_id": request_id, "status": "queued"})

    def _handle_stop(self) -> None:
        """POST /stop - request graceful shutdown."""
        server = _server_instance
        if server is None:
            self._send_json(500, {"error": "server not initialized"})
            return

        # Write stop request file
        import json as json_module

        server.paths.root.mkdir(parents=True, exist_ok=True)
        server.paths.stop_request.write_text(
            json_module.dumps(
                {"requested_at": time.time(), "reason": "http stop request"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._send_json(200, {"status": "stopping"})

    def _handle_session_channels(self) -> None:
        """GET /sessions/{session_id}/channels - query session channel bindings。"""
        # 需要管理员权限
        from ..auth.middleware import require_admin_handler

        if require_admin_handler(self):
            return

        parts = self.path.split("/")
        if len(parts) >= 4:
            session_id = parts[2]
            server = _server_instance
            if server is None or server.cross_channel is None:
                self._send_json(500, {"error": "cross channel not initialized"})
                return
            bound = server.cross_channel.get_bound_sessions(session_id)
            primary = server.cross_channel.get_primary_channel(session_id)
            self._send_json(200, {"session_id": session_id, "bound_channels": bound, "primary_channel": primary})
        else:
            self._send_json(400, {"error": "invalid path"})

    def _handle_session_bind(self) -> None:
        """POST /sessions/{session_id}/bind - bind session to new channel。"""
        # 需要管理员权限
        from ..auth.middleware import require_admin_handler

        if require_admin_handler(self):
            return

        parts = self.path.split("/")
        if len(parts) >= 4:
            session_id = parts[2]
            try:
                body = self._read_json()
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": f"invalid JSON: {e}"})
                return

            channel = body.get("channel")
            user_id = body.get("user_id", "admin")
            if not channel:
                self._send_json(400, {"error": "channel is required"})
                return

            server = _server_instance
            if server is None or server.cross_channel is None:
                self._send_json(500, {"error": "cross channel not initialized"})
                return

            success = server.cross_channel.bind_session(session_id, channel, user_id)
            self._send_json(200, {"success": success, "session_id": session_id, "channel": channel})
        else:
            self._send_json(400, {"error": "invalid path"})

    def _handle_admin_summary(self) -> None:
        """GET /admin/summary - admin global summary。"""
        # 需要管理员权限
        from ..auth.middleware import require_admin_handler

        if require_admin_handler(self):
            return

        server = _server_instance
        if server is None or server.admin_query is None:
            self._send_json(500, {"error": "admin query not initialized"})
            return

        summary = server.admin_query.format_admin_summary("admin")
        self._send_json(200, {"summary": summary})


class GatewayHTTPServer:
    """HTTP server for gateway。"""

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
        """Start HTTP server in background thread."""
        global _server_instance
        _server_instance = self

        self.server = ThreadingHTTPServer(("", self.port), GatewayHTTPHandler)
        self.server.server_version = "MyAgentGateway/1.0"
        self.server.handler_class = GatewayHTTPHandler

        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        """Serve HTTP requests until stopped."""
        if self.server is None:
            return
        try:
            self.server.serve_forever()
        except Exception:
            pass

    def stop(self, timeout: float = 5.0) -> None:
        """Stop HTTP server."""
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
    """Start HTTP server and return handle."""
    server = GatewayHTTPServer(port, paths, cross_channel, admin_query, auth_middleware)
    server.start()
    return server
