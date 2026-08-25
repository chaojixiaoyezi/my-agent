from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

from agent_py_agent.agent.gateway_parts.bounded_http_server import (
    GatewayBoundedHTTPServer,
)


class _TinyBoundedServer(GatewayBoundedHTTPServer):
    max_request_workers = 2
    max_outstanding_requests = 3


class _SingleWorkerServer(GatewayBoundedHTTPServer):
    max_request_workers = 1
    max_outstanding_requests = 2


class _BlockingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        server = self.server
        with server.test_lock:
            server.test_active += 1
            server.test_worker_ids.add(threading.get_ident())
            if server.test_active >= server.max_request_workers:
                server.test_workers_started.set()
        server.test_release.wait(5)
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        with server.test_lock:
            server.test_active -= 1


def _raw_request(port: int) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    return client


def _wait_until_slots_are_full(server: _TinyBoundedServer) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if server._request_slots._value == 0:
            return
        time.sleep(0.01)
    raise AssertionError("bounded HTTP request slots did not fill")


def test_gateway_http_pool_reuses_workers_and_rejects_overload() -> None:
    server = _TinyBoundedServer(("127.0.0.1", 0), _BlockingHandler)
    server.test_lock = threading.Lock()
    server.test_active = 0
    server.test_worker_ids = set()
    server.test_workers_started = threading.Event()
    server.test_release = threading.Event()
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    clients: list[socket.socket] = []
    try:
        port = int(server.server_address[1])
        clients.extend([_raw_request(port), _raw_request(port)])
        assert server.test_workers_started.wait(2)
        clients.append(_raw_request(port))
        _wait_until_slots_are_full(server)

        request = urllib.request.Request(f"http://127.0.0.1:{port}/")
        try:
            urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
            assert exc.headers.get("Retry-After") == "1"
            payload = json.loads(exc.read().decode("utf-8"))
            assert payload["error_code"] == "GATEWAY_HTTP_BUSY"
        else:
            raise AssertionError("fourth request should be rejected by capacity=3")

        assert len(server._request_executor._threads) == 2
        assert len(server.test_worker_ids) == 2
        assert all(thread.daemon for thread in server._request_executor._threads)
    finally:
        server.test_release.set()
        for client in clients:
            try:
                client.recv(4096)
            except OSError:
                pass
            client.close()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)


def test_gateway_http_pool_closes_cancelled_queued_request_on_shutdown() -> None:
    server = _SingleWorkerServer(("127.0.0.1", 0), _BlockingHandler)
    server.test_lock = threading.Lock()
    server.test_active = 0
    server.test_worker_ids = set()
    server.test_workers_started = threading.Event()
    server.test_release = threading.Event()
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    clients: list[socket.socket] = []
    try:
        port = int(server.server_address[1])
        clients.append(_raw_request(port))
        assert server.test_workers_started.wait(2)
        queued = _raw_request(port)
        clients.append(queued)
        _wait_until_slots_are_full(server)

        server.shutdown()
        server.server_close()
        serving.join(timeout=2)
        queued.settimeout(1)
        try:
            closed_payload = queued.recv(4096)
        except ConnectionResetError:
            closed_payload = b""
        assert closed_payload == b""
    finally:
        server.test_release.set()
        for client in clients:
            try:
                client.recv(4096)
            except OSError:
                pass
            client.close()
