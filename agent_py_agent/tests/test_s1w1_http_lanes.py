"""真实回环连接验证就绪分类、独立容量、排队过期及票据交错。"""
from __future__ import annotations

import socket
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler

import pytest

from agent_py_agent.agent.gateway_parts.bounded_http_server import GatewayBoundedHTTPServer
from agent_py_agent.agent.gateway_parts.http_lanes import (
    HTTP_CONTROL_PEEK_BYTES,
    HTTP_CONTROL_ROUTES,
    _LaneTicket,
    classify_peeked_request,
)
from agent_py_agent.agent.gateway_parts.http_routes import (
    GATEWAY_HTTP_ROUTES,
    match_gateway_http_route,
)


class TinyServer(GatewayBoundedHTTPServer):
    max_request_workers = 2
    max_outstanding_requests = 4
    request_socket_timeout = 0.5
    request_queue_timeout = 0.3


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass

    def do_GET(self):
        self.server.handled.set()
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(b'{}')

    def do_POST(self):
        if self.path == '/ask':
            with self.server.test_lock:
                self.server.started += 1
            self.server.release.wait(2)
        self.do_GET()


def eventually(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, '等待真实资源状态收敛超时'
        time.sleep(0.005)


@pytest.fixture
def server():
    result = TinyServer(('127.0.0.1', 0), Handler, control_lane_workers=2,
        pending_connection_limit=TinyServer.max_request_workers + 8)
    result.release = threading.Event()
    result.handled = threading.Event()
    result.test_lock = threading.Lock()
    result.started = 0
    thread = threading.Thread(target=result.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        yield result
    finally:
        result.release.set()
        result.shutdown()
        result.server_close()
        thread.join(1)
        assert not thread.is_alive()
        assert not result._lanes._thread.is_alive()


def connect(server, data=None):
    client = socket.create_connection(server.server_address, timeout=1)
    if data is not None:
        client.sendall(data)
    return client


def request(server, method='GET', path='/status'):
    return connect(server, f'{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n'.encode())


def response(client):
    chunks = []
    while True:
        try:
            chunk = client.recv(4096)
        except ConnectionResetError:
            assert chunks
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b''.join(chunks)


def test_status_has_independent_workers_and_outstanding_slots(server):
    clients = [request(server, 'POST', '/ask') for _ in range(server.max_request_workers)]
    try:
        eventually(lambda: server.started == server.max_request_workers)
        clients.extend(request(server, 'POST', '/ask') for _ in range(2))
        eventually(lambda: server._request_slots._value == 0)
        start = time.monotonic()
        with request(server) as status:
            assert b'200 OK' in response(status)
        assert time.monotonic() - start < 1
        assert server.started == server.max_request_workers
        assert server._lanes._control_executor is not server._request_executor
        assert all(t.daemon for t in server._lanes._control_executor._threads)
    finally:
        server.release.set()
        for client in clients:
            client.close()
    eventually(lambda: server._request_slots._value == server.max_outstanding_requests)


def test_idle_connections_use_no_workers_and_expire_silently(server):
    clients = [connect(server) for _ in range(server.max_request_workers + 5)]
    try:
        eventually(lambda: len(server._lanes._pending) == len(clients))
        assert not server._request_executor._threads
        assert server._request_slots._value == server.max_outstanding_requests
        with request(server) as status:
            assert b'200 OK' in response(status)
        server.release.set()
        with request(server, 'POST', '/ask') as ask:
            assert b'200 OK' in response(ask)
        for client in clients:
            assert client.recv(1) == b''
        eventually(lambda: not server._lanes._pending)
        assert server._request_slots._value == server.max_outstanding_requests
    finally:
        for client in clients:
            client.close()


def test_eof_does_not_submit_or_take_a_slot(server):
    client = connect(server)
    client.shutdown(socket.SHUT_WR)
    assert client.recv(1) == b''
    client.close()
    assert not server._request_executor._threads
    assert server._request_slots._value == server.max_outstanding_requests


def test_partial_first_line_uses_general_lane_and_preserves_bytes(server):
    with connect(server, b'GET /sta') as client:
        eventually(lambda: server._request_slots._value == server.max_outstanding_requests - 1)
        # 容量领取先于Thread.start及_threads发表，不能把前者当作线程已发表的证明。
        eventually(lambda: bool(server._request_executor._threads))
        assert not server._lanes._control_executor._threads
        client.sendall(b'tus HTTP/1.1\r\nHost: localhost\r\n\r\n')
        assert b'200 OK' in response(client)
    eventually(lambda: server._request_slots._value == server.max_outstanding_requests)


def test_queued_request_expires_while_workers_remain_blocked(server):
    clients = [request(server, 'POST', '/ask') for _ in range(server.max_request_workers)]
    try:
        eventually(lambda: server.started == server.max_request_workers)
        start = time.monotonic()
        with request(server, 'POST', '/ask') as queued:
            payload = response(queued)
        assert b'503 Service Unavailable' in payload and b'GATEWAY_HTTP_BUSY' in payload
        assert time.monotonic() - start < server.request_queue_timeout + 0.5
        assert not server.release.is_set()
        assert server.started == server.max_request_workers
    finally:
        server.release.set()
        for client in clients:
            client.close()
    eventually(lambda: server._request_slots._value == server.max_outstanding_requests)


@pytest.mark.parametrize('winner', ['worker', 'expiry'])
def test_expiry_worker_cas_has_one_owner_close_and_slot_release(server, monkeypatch, winner):
    worker_entered, expiry_entered = threading.Event(), threading.Event()
    worker_go, expiry_go = threading.Event(), threading.Event()
    running, expired = _LaneTicket.claim_running, _LaneTicket.claim_expired
    tickets, closes = [], Counter()
    shutdown = server.shutdown_request

    def claim_running(ticket):
        tickets.append(ticket)
        worker_entered.set()
        assert worker_go.wait(2)
        return running(ticket)

    def claim_expired(ticket):
        expiry_entered.set()
        assert expiry_go.wait(2)
        return expired(ticket)

    def close(sock):
        closes[sock] += 1
        shutdown(sock)

    monkeypatch.setattr(_LaneTicket, 'claim_running', claim_running)
    monkeypatch.setattr(_LaneTicket, 'claim_expired', claim_expired)
    monkeypatch.setattr(server, 'shutdown_request', close)
    try:
        with request(server, 'GET', '/ordinary') as client:
            assert worker_entered.wait(1)
            assert expiry_entered.wait(1)
            (worker_go if winner == 'worker' else expiry_go).set()
            payload = response(client)
            assert (b'200 OK' in payload) is (winner == 'worker')
            assert (b'GATEWAY_HTTP_BUSY' in payload) is (winner == 'expiry')
            assert server.handled.is_set() is (winner == 'worker')
    finally:
        worker_go.set()
        expiry_go.set()
    eventually(lambda: server._request_slots._value == server.max_outstanding_requests)
    assert len(tickets) == 1
    assert closes[tickets[0].request] == 1
    assert tickets[0].state == 'DONE'


def test_pending_limit_rejects_without_consuming_a_worker(server):
    server._lanes.pending_limit = 1
    with connect(server) as idle:
        eventually(lambda: len(server._lanes._pending) == 1)
        start = time.monotonic()
        with connect(server) as excess:
            assert b'GATEWAY_HTTP_BUSY' in response(excess)
        assert time.monotonic() - start < server.request_socket_timeout
        assert not server._request_executor._threads
        assert idle.recv(1) == b''


def test_zero_workers_preserves_legacy_path(monkeypatch):
    result = TinyServer(('127.0.0.1', 0), Handler, control_lane_workers=0)
    handled = threading.Event()
    client, accepted = socket.socketpair()
    monkeypatch.setattr(result, 'finish_request', lambda _sock, _addr: handled.set())
    try:
        assert result._lanes is None
        result.process_request(accepted, ('127.0.0.1', 1))
        assert handled.wait(1)
        client.settimeout(1)
        assert client.recv(1) == b''
        eventually(lambda: result._request_slots._value == result.max_outstanding_requests)
    finally:
        client.close()
        accepted.close()
        result.server_close()


def test_classification_matches_runtime_route_table():
    expected = frozenset({('GET', '/status'), ('GET', '/metrics'), ('GET', '/control-status/*'),
        ('POST', '/control'), ('POST', '/client/agent-stop'), ('POST', '/client/agent-guidance'),
        ('POST', '/client/agent-permission'), ('POST', '/stop')})
    assert expected == HTTP_CONTROL_ROUTES
    for method, routes in GATEWAY_HTTP_ROUTES.items():
        for route in routes:
            path = route.template.replace('*', 'sample')
            assert match_gateway_http_route(method, path) is route
            lane = 'control' if (method, route.template) in expected else 'general'
            assert classify_peeked_request(f'{method} {path}?q=1 HTTP/1.1\r\n'.encode()) == lane
    for raw, lane in [(b'', 'eof'), (b'GET /status', 'general'), (b'\xff /status HTTP/1.1\n', 'general'),
            (b'GET  /status HTTP/1.1\n', 'general'), (b'GET /new-route HTTP/1.1\n', 'general'),
            (b'GET /status HTTP/1.1 extra\n', 'general'),
            (b'GET /status?' + b'x' * HTTP_CONTROL_PEEK_BYTES + b' HTTP/1.1\n', 'general')]:
        assert classify_peeked_request(raw) == lane


def test_repeated_expiry_removes_cancelled_executor_work_items(server):
    clients = [request(server, 'POST', '/ask') for _ in range(server.max_request_workers)]
    try:
        eventually(lambda: server.started == server.max_request_workers)
        for _ in range(3):
            with request(server, 'POST', '/ask') as queued:
                assert b'GATEWAY_HTTP_BUSY' in response(queued)
            eventually(lambda: server._request_slots._value == server.max_outstanding_requests - server.max_request_workers)
            assert server._request_executor._work_queue.qsize() == 0
        assert server.started == server.max_request_workers
        assert not server.release.is_set()
    finally:
        server.release.set()
        for client in clients:
            client.close()


def test_worker_checks_expiry_when_selector_has_not_swept(server, monkeypatch):
    worker_entered, selector_paused = threading.Event(), threading.Event()
    worker_go, selector_go = threading.Event(), threading.Event()
    run, sweep = server._lanes._run_ticket, server._lanes._sweep

    def delay_worker(ticket):
        worker_entered.set()
        assert worker_go.wait(2)
        run(ticket)

    def delay_sweep():
        if worker_entered.is_set():
            selector_paused.set()
            assert selector_go.wait(2)
        return sweep()

    monkeypatch.setattr(server._lanes, '_run_ticket', delay_worker)
    monkeypatch.setattr(server._lanes, '_sweep', delay_sweep)
    try:
        with request(server, 'GET', '/ordinary') as client:
            assert worker_entered.wait(1)
            assert selector_paused.wait(1)
            time.sleep(server.request_queue_timeout + 0.01)
            worker_go.set()
            assert b'GATEWAY_HTTP_BUSY' in response(client)
            assert not server.handled.is_set()
    finally:
        worker_go.set()
        selector_go.set()
    eventually(lambda: server._request_slots._value == server.max_outstanding_requests)


def test_dead_selector_closes_admission_even_if_failure_observer_raises(server, monkeypatch):
    failures = []

    def submit_failure(_fn, _ticket):
        raise RuntimeError('submit_unavailable')

    def observer_failure(error):
        failures.append(error)
        raise RuntimeError('observer_unavailable')

    monkeypatch.setattr(threading, 'excepthook', lambda _args: None)
    monkeypatch.setattr(server._request_executor, 'submit', submit_failure)
    server._request_worker_failure = observer_failure
    with request(server, 'GET', '/ordinary') as client:
        assert b'GATEWAY_HTTP_BUSY' in response(client)
    eventually(lambda: not server._lanes._thread.is_alive())
    with connect(server) as later:
        assert b'GATEWAY_HTTP_BUSY' in response(later)
    assert len(failures) == 1
    assert server._lanes._closing.is_set()
    assert not server._lanes._incoming


def test_enqueue_then_thread_creation_failure_never_enters_handler(server, monkeypatch):
    executor = server._request_executor
    handled = []
    monkeypatch.setattr(server, 'finish_request', lambda _sock, _addr: handled.append(True))
    client, accepted = socket.socketpair()

    def fail_start():
        raise RuntimeError('thread_start_unavailable')

    try:
        with monkeypatch.context() as patch:
            patch.setattr(executor, '_adjust_thread_count', fail_start)
            server._lanes._submit('general', accepted, ('127.0.0.1', 1), time.monotonic())
        client.settimeout(1)
        assert b'GATEWAY_HTTP_BUSY' in response(client)
        executor.submit(lambda: None).result(1)
        assert handled == []
        assert executor._work_queue.qsize() == 0
        assert server._request_slots._value == server.max_outstanding_requests
    finally:
        client.close()
        accepted.close()


def test_close_closes_registered_and_unregistered_idle_connections(server):
    with connect(server) as idle:
        eventually(lambda: len(server._lanes._pending) == 1)
        server._lanes.close()
        assert idle.recv(1) == b''
        assert not server._lanes._pending and not server._lanes._incoming
        assert not server._lanes._thread.is_alive()
        assert server._lanes._wake_read.fileno() == -1
        assert server._lanes._wake_write.fileno() == -1
        with connect(server) as later:
            assert b'GATEWAY_HTTP_BUSY' in response(later)


def test_tls_socket_uses_legacy_worker_without_peek(server, monkeypatch):
    import ssl

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    client, accepted = socket.socketpair()
    secured = context.wrap_socket(accepted, server_side=True, do_handshake_on_connect=False)
    handled = threading.Event()

    def forbidden(_sock, _addr):
        raise AssertionError('TLS连接不得进入MSG_PEEK路径')

    monkeypatch.setattr(server._lanes, 'admit', forbidden)
    monkeypatch.setattr(server, 'finish_request', lambda _sock, _addr: handled.set())
    try:
        server.process_request(secured, ('127.0.0.1', 1))
        assert handled.wait(1)
        eventually(lambda: server._request_slots._value == server.max_outstanding_requests)
        assert secured.fileno() == -1
    finally:
        client.close()
        secured.close()


@pytest.mark.parametrize('layout', ['args', 'task', 'future'])
def test_cancel_queue_discards_original_work_item_identity(layout):
    from concurrent.futures import Future
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.http_lanes import _CancellableWorkQueue

    queue = _CancellableWorkQueue()
    ticket, future = object(), Future()
    if layout == 'args':
        work = SimpleNamespace(future=future, args=(ticket,))
    else:
        work = SimpleNamespace(future=future, task=(lambda: None, (ticket,), {}))
    retained = object()
    queue.put(work)
    queue.put(retained)
    queue.discard(future if layout == 'future' else ticket)
    assert queue.qsize() == 1
    assert queue.get_nowait() is retained
