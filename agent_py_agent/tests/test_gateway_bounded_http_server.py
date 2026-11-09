from __future__ import annotations

import http.client
import json
import select
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

import pytest

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


def test_bound_listener_serves_http_without_reverse_dns(monkeypatch):
    def unavailable(*_):
        raise AssertionError("绑定监听端口不应依赖反向 DNS")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(204)
            self.end_headers()

    monkeypatch.setattr(socket, "getfqdn", unavailable)
    with _TinyBoundedServer(("127.0.0.1", 0), Handler) as server:
        assert (server.server_name, server.server_port) == server.socket.getsockname()
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        serving.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=2) as response:
                assert response.status == 204
        finally:
            server.shutdown()
            serving.join(timeout=2)
        assert not serving.is_alive()


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


def test_incomplete_client_request_releases_worker_for_status():
    server = _SingleWorkerServer(("127.0.0.1", 0), _BlockingHandler)
    server.request_socket_timeout = 0.1
    server.test_lock = threading.Lock()
    server.test_active = 0
    server.test_worker_ids = set()
    server.test_workers_started = threading.Event()
    server.test_release = threading.Event()
    server.test_release.set()
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    client = socket.create_connection(server.server_address, timeout=2)
    try:
        client.sendall(b"GET / HTTP/1.1\r\nHost: unfinished")
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=2) as response:
            assert response.status == 200
        assert client.recv(4096) == b""
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        serving.join(2)


def test_expired_queue_never_enters_product_handler():
    server = _SingleWorkerServer(("127.0.0.1", 0), _BlockingHandler)
    server.request_queue_timeout = 0.05
    client, accepted = socket.socketpair()
    try:
        server._process_request_worker(accepted, ("127.0.0.1", 1), time.monotonic() - 1)
        assert b"GATEWAY_HTTP_BUSY" in client.recv(4096)
        assert client.recv(1) == b""
    finally:
        client.close()
        accepted.close()
        server.server_close()

@pytest.mark.parametrize("entry", ["_reject_overloaded_request", "_reject_without_wait"])
def test_overload_half_close_preserves_response_for_late_request_bytes(entry):
    # 真实 TCP：先接受并拒绝，客户端读到完整 503 后才把请求尾部送达。
    # Win32 立即完全关闭会让在途字节产生 RST，客户端甚至读不到状态行。
    with _TinyBoundedServer(("127.0.0.1", 0), _BlockingHandler) as server:
        with socket.create_connection(server.server_address, timeout=2) as client:
            accepted, _ = server.get_request()
            try:
                getattr(server, entry)(accepted)
                with http.client.HTTPResponse(client) as response:
                    response.begin()
                    assert response.status == 503 and response.getheader("Retry-After") == "1"
                    assert json.loads(response.read())["error_code"] == "GATEWAY_HTTP_BUSY"
                assert accepted.fileno() == -1
                tail, = server._reject_drains
                assert tail.getpeername() == client.getsockname()
                client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                client.shutdown(socket.SHUT_WR)
                for _ in range(3):
                    if tail.fileno() < 0:
                        break
                    assert select.select([tail], [], [], 2)[0]
                    server.service_actions()
                assert tail.fileno() == -1 and not server._reject_drains
            finally:
                accepted.close()


def test_overload_retirement_is_bounded_and_shutdown_releases_silent_peers():
    server = _TinyBoundedServer(("127.0.0.1", 0), _BlockingHandler)
    clients, accepted = [], []
    try:
        for _ in range(server.max_request_workers + 2):
            clients.append(socket.create_connection(server.server_address, timeout=2))
            connection, _ = server.get_request()
            accepted.append(connection)
            server._reject_without_wait(connection)
        assert all(sock.fileno() == -1 for sock in accepted)
        tails = tuple(server._reject_drains)
        assert len(tails) == server.max_request_workers and all(tail.fileno() >= 0 for tail in tails)
        assert not server._request_executor._threads
        server.server_close()
        assert all(tail.fileno() == -1 for tail in tails)
    finally:
        for sock in clients + accepted:
            sock.close()
        server.server_close()


def test_overload_silent_peer_expires_without_worker(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import bounded_http_server as transport

    with _TinyBoundedServer(("127.0.0.1", 0), _BlockingHandler) as server:
        with socket.create_connection(server.server_address, timeout=2) as client:
            client.settimeout(2)
            accepted, _ = server.get_request()
            try:
                now = [10.0]
                monkeypatch.setattr(transport, "time", SimpleNamespace(monotonic=lambda: now[0]))
                server._reject_without_wait(accepted)
                tail, = server._reject_drains
                server.service_actions()
                assert accepted.fileno() == -1 and tail.fileno() >= 0
                now[0] += server.request_queue_timeout + 1
                server.service_actions()
                assert tail.fileno() == -1 and not server._request_executor._threads
            finally:
                accepted.close()
