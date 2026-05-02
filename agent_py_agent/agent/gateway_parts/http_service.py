from __future__ import annotations

"""LLM: HTTP service for gateway using standard library http.server.

给人看的解释：
这个文件实现 gateway 的 HTTP 接口：POST /ask、GET /result/<id>、GET /status、POST /stop。
用标准库 http.server + threading 实现并发。
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
    from .paths import GatewayPaths


# Global server instance for signal handler access
_server_instance: Optional["GatewayHTTPServer"] = None


class GatewayHTTPRequest:
    """Parsed HTTP request for gateway."""

    def __init__(self, request_id: str, goal: str, metadata: dict[str, Any] | None = None):
        self.request_id = request_id
        self.goal = goal
        self.metadata = metadata or {}


class GatewayHTTPResponse:
    """HTTP response from gateway."""

    def __init__(self, status: int, body: dict[str, Any]):
        self.status = status
        self.body = body


def _generate_request_id() -> str:
    """Generate a unique request ID."""
    return f"req_{int(time.time() * 1000)}_{os.getpid()}"


class GatewayHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for gateway."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        """Override to reduce noise."""
        pass

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
        """Handle GET requests."""
        if self.path == "/status":
            self._handle_status()
        elif self.path.startswith("/result/"):
            self._handle_result()
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        """Handle POST requests."""
        if self.path == "/ask":
            self._handle_ask()
        elif self.path == "/stop":
            self._handle_stop()
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
        """GET /result/<request_id> - return request result."""
        request_id = self.path[len("/result/"):]
        server = _server_instance
        if server is None:
            self._send_json(500, {"error": "server not initialized"})
            return

        # Check responses directory
        response_path = server.paths.responses / f"{request_id}.json"
        if not response_path.exists():
            # Check if still processing
            processing_path = server.paths.processing / f"{request_id}.json"
            if processing_path.exists():
                self._send_json(202, {"status": "processing", "request_id": request_id})
                return
            # Check if queued in inbox
            inbox_path = server.paths.inbox / f"{request_id}.json"
            if inbox_path.exists():
                self._send_json(202, {"status": "queued", "request_id": request_id})
                return
            self._send_json(404, {"error": "not found", "request_id": request_id})
            return

        try:
            result = json.loads(response_path.read_text(encoding="utf-8"))
            self._send_json(200, result)
        except (json.JSONDecodeError, OSError) as e:
            self._send_json(500, {"error": f"failed to read result: {e}"})

    def _handle_ask(self) -> None:
        """POST /ask - submit a new request."""
        try:
            body = self._read_json()
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"invalid JSON: {e}"})
            return

        goal = body.get("goal", "")
        if not goal:
            self._send_json(400, {"error": "goal is required"})
            return

        server = _server_instance
        if server is None:
            self._send_json(500, {"error": "server not initialized"})
            return

        # Generate request ID and write to pending queue
        request_id = _generate_request_id()
        request_data = {
            "request_id": request_id,
            "goal": goal,
            "metadata": body.get("metadata", {}),
            "submitted_at": time.time(),
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


class GatewayHTTPServer:
    """HTTP server for gateway."""

    def __init__(self, port: int, paths: GatewayPaths):
        self.port = port
        self.paths = paths
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


def start_http_server(port: int, paths: GatewayPaths) -> GatewayHTTPServer:
    """Start HTTP server and return handle."""
    server = GatewayHTTPServer(port, paths)
    server.start()
    return server
