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


def test_partial_first_line_waits_without_worker_and_preserves_bytes(server):
    with connect(server, b'GET /sta') as client:
        eventually(lambda: bool(server._lanes._prefixes))
        assert server._request_slots._value == server.max_outstanding_requests
        assert not server._request_executor._threads
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


@pytest.mark.parametrize('winner, reserved', [('worker', False), ('expiry', False), ('worker', True), ('expiry', True)],
                         ids=['worker', 'expiry', 'ready-worker', 'ready-expiry'])
def test_expiry_worker_cas_has_one_owner_close_and_slot_release(server, monkeypatch, winner, reserved):
    blocker = request(server, 'POST', '/ask') if reserved else None
    if blocker is not None:
        eventually(lambda: server.started == 1)
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
        if reserved and ticket.lane != 'ready':
            return expired(ticket)
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
        if blocker is not None:
            server.release.set()
            blocker.close()
    eventually(lambda: server._request_slots._value == server.max_outstanding_requests)
    assert len(tickets) == 1
    assert (tickets[0].lane == 'ready') is reserved
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


def test_partial_bodies_leave_original_general_capacity_for_complete_request(server):
    """同一普通路由的部分正文不得占满原池；不用控制路由兜底。"""
    original = Handler.do_POST
    entered = threading.Event()
    calls = []

    def read_body(handler):
        entered.set()
        body = handler.rfile.read(int(handler.headers['Content-Length']))
        calls.append(body)
        handler.do_GET()

    Handler.do_POST = read_body
    clients = []
    try:
        for _ in range(server.max_request_workers + 3):
            clients.append(connect(server, b'POST /ordinary HTTP/1.1\r\nHost: localhost\r\nContent-Length: 4096\r\n\r\n{'))
        with connect(server, b'POST /ordinary HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}') as complete:
            assert b'200 OK' in response(complete)
        assert calls == [b'{}']
        assert not server._lanes._control_executor._threads
        assert len(server._request_executor._threads) + len(server._lanes._ready_executor._threads) <= server.max_request_workers
    finally:
        Handler.do_POST = original
        for client in clients:
            client.close()


def test_segmented_body_under_saturation_waits_for_exact_completion(server, monkeypatch):
    seen = []
    blocker_entered = threading.Event()

    def read_body(handler):
        if handler.path == '/blocker':
            blocker_entered.set()
        body = handler.rfile.read(int(handler.headers['Content-Length']))
        if handler.path != '/blocker':
            seen.append(body)
        handler.do_GET()

    monkeypatch.setattr(Handler, 'do_POST', read_body)
    blocker = connect(server, b'POST /blocker HTTP/1.1\r\nContent-Length: 9000\r\n\r\nx')
    try:
        assert blocker_entered.wait(1)
        with connect(server, b'POST /ordinary HTTP/1.1\r\nHost: localhost\r\nContent-Length: 6\r\n\r\nab') as client:
            eventually(lambda: any(p.endswith(b'ab') for p in server._lanes._prefixes.copy().values()))
            assert len(server._request_executor._threads) == 1
            assert not server._lanes._ready_executor._threads
            client.sendall(b'cd')
            eventually(lambda: any(p.endswith(b'abcd') for p in server._lanes._prefixes.copy().values()))
            assert not server._lanes._ready_executor._threads
            client.sendall(b'ef')
            assert b'200 OK' in response(client)
        assert seen == [b'abcdef']
        eventually(lambda: not server._lanes._prefixes and not server._lanes._frame_lengths)
    finally:
        blocker.close()


def test_large_body_keeps_original_payload_and_general_worker(server, monkeypatch):
    from agent_py_agent.agent.gateway_parts.http_body_readiness import HTTP_READY_PREFIX_BYTES

    entered = threading.Event()
    seen = []
    body = b'x' * (HTTP_READY_PREFIX_BYTES + 100)

    def read_body(handler):
        entered.set()
        seen.append(handler.rfile.read(int(handler.headers['Content-Length'])))
        handler.do_GET()

    monkeypatch.setattr(Handler, 'do_POST', read_body)
    with connect(server, f'POST /ordinary HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body[:1]) as client:
        assert entered.wait(1)
        assert not server._lanes._ready_executor._threads
        client.sendall(body[1:])
        assert b'200 OK' in response(client)
    assert seen == [body]


@pytest.mark.parametrize('framing', [
    b'Content-Length: 2\r\nContent-Length: 3',
    b'Content-Length: +2',
    b'Content-Length: ' + b'9' * 5000,
    b'Transfer-Encoding: chunked',
    b'Expect: 100-continue\r\nContent-Length: 2',
])
def test_ambiguous_framing_never_signs_complete(framing):
    from agent_py_agent.agent.gateway_parts.http_body_readiness import complete_request_length

    prefix = bytearray(b'POST /ordinary HTTP/1.1\r\n' + framing + b'\r\n\r\n{}')
    assert complete_request_length(prefix, single_request=True) == 0


def test_keepalive_second_partial_request_cannot_take_reserved_worker(monkeypatch):
    import http.client

    class PersistentHandler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.send_header('Content-Length', '2')
            if self.headers.get('Connection') == 'close':
                self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(b'{}')

    server = TinyServer(('127.0.0.1', 0), PersistentHandler, control_lane_workers=2)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
    thread.start()
    client = connect(server, b'POST /ordinary HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}')
    try:
        first = http.client.HTTPResponse(client)
        first.begin()
        assert first.status == 200 and first.read() == b'{}'
        assert not server._lanes._ready_executor._threads
        client.sendall(b'POST /ordinary HTTP/1.1\r\nHost: localhost\r\nContent-Length: 6\r\n\r\na')
        with connect(server, b'POST /ordinary HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Length: 2\r\n\r\n{}') as complete:
            assert b'200 OK' in response(complete)
        client.sendall(b'bcdef')
        second = http.client.HTTPResponse(client)
        second.begin()
        assert second.status == 200 and second.read() == b'{}'
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(1)
        assert not thread.is_alive() and not server._lanes._thread.is_alive()


def test_pending_prefixes_are_bounded_do_not_spin_and_release_on_eof(server, monkeypatch):
    from agent_py_agent.agent.gateway_parts.http_body_readiness import HTTP_READY_PREFIX_BYTES

    observed = []
    original = server._lanes._read_ready

    def observe(*args):
        observed.append(1)
        return original(*args)

    monkeypatch.setattr(server._lanes, '_read_ready', observe)
    clients = [connect(server, b'POST /ordinary HTTP/1.1\r\nContent-Length: 4000\r\nX-Fixture: x') for _ in range(5)]
    try:
        eventually(lambda: len(server._lanes._prefixes) == len(clients))
        count = len(observed)
        threading.Event().wait(0.03)
        assert len(observed) == count  # 已消费的前缀不再让level-trigger selector反复就绪。
        assert all(len(p) <= HTTP_READY_PREFIX_BYTES for p in server._lanes._prefixes.values())
        assert sum(map(len, server._lanes._prefixes.values())) <= server._lanes.pending_limit * HTTP_READY_PREFIX_BYTES
        assert len(server._request_executor._threads) <= server._lanes._mixed_workers
        assert not server._lanes._ready_executor._threads
    finally:
        for client in clients:
            client.close()
    eventually(lambda: not server._lanes._pending and not server._lanes._prefixes)
    eventually(lambda: server._request_slots._value == server.max_outstanding_requests)


def test_real_gateway_complete_request_still_requires_authentication(tmp_path):
    import json
    from pathlib import Path

    from agent_py_agent.tests.slo.fake_provider import FakeProvider
    from agent_py_agent.tests.slo.load import GatewayProcess, durable_receipt, json_request
    from agent_py_agent.tests.support.ci_authenticated_gateway import authenticated_headers

    parameters = json.loads((Path(__file__).parent / 'slo' / 'slo_targets.json').read_text())['fixture']
    with FakeProvider(tmp_path / 'provider', teardown_seconds=parameters['teardown_seconds']) as provider:
        gateway = GatewayProcess(tmp_path / 'gateway', provider, parameters, {'gateway_http_control_lane_workers': 4})
        clients = []
        try:
            gateway.start()
            for _ in range(52):
                endpoint = gateway.base_url.rsplit(':', 1)
                client = socket.create_connection(('127.0.0.1', int(endpoint[1])), timeout=2)
                client.sendall(b'POST /ask HTTP/1.1\r\nHost: localhost\r\nContent-Length: 4096\r\n\r\n{')
                clients.append(client)
            body = {'prompt': 'isolated-auth-boundary', 'conversation_id': 'ready-auth',
                    'delivery_intent': 'queue', 'metadata': {'message_id': 'ready-auth'}}
            denied, response_body = json_request(gateway.base_url, '/ask', body, headers={'X-Gateway-Token': 'invalid-fixture-token'})
            assert denied == 403 and not durable_receipt(response_body)
            accepted, response_body = json_request(gateway.base_url, '/ask', body, headers=authenticated_headers(gateway.home))
            assert accepted == 202 and durable_receipt(response_body)
        finally:
            for client in clients:
                client.close()
            gateway.close()


def test_prefix_replay_preserves_pipeline_bytes_and_peek_without_consuming():
    from agent_py_agent.agent.gateway_parts.http_body_readiness import PrefetchedSocket

    left, right = socket.socketpair()
    prefix = bytearray(b'first\r\nsecond\r\n')
    wrapped = PrefetchedSocket(left, prefix)
    try:
        assert left.fileno() == wrapped.fileno() and wrapped._socket is left
        assert wrapped.recv(5, socket.MSG_PEEK) == b'first'
        right.sendall(b'kernel\r\n')
        with wrapped.makefile('rb') as reader:
            assert reader.readline() == b'first\r\n'
            assert reader.readline() == b'second\r\n'
            assert reader.readline() == b'kernel\r\n'
        assert wrapped._prefix is None
    finally:
        wrapped.close()
        right.close()


def test_ready_framing_does_not_guess_keepalive_close_tokens():
    from agent_py_agent.agent.gateway_parts.http_body_readiness import complete_request_length

    for value in (b'keep-alive', b'close, keep-alive', b'close '):
        prefix = bytearray(b'POST /ordinary HTTP/1.1\r\nConnection: ' + value + b'\r\nContent-Length: 0\r\n\r\n')
        assert complete_request_length(prefix, single_request=False) == 0


def test_replay_setup_failure_closes_original_socket_without_gc(server, monkeypatch):
    from agent_py_agent.agent.gateway_parts.http_body_readiness import PrefetchedSocket

    failed = threading.Event()
    retained = []

    def fail_timeout(wrapped, _timeout):
        retained.append(wrapped)
        failed.set()
        raise OSError('isolated-replay-initialization')

    with monkeypatch.context() as scoped:
        scoped.setattr(PrefetchedSocket, 'settimeout', fail_timeout)
        with request(server, 'GET', '/ordinary') as client:
            assert b'GATEWAY_HTTP_BUSY' in response(client)
        assert failed.wait(1) and retained[0].fileno() == -1
    assert server._lanes._thread.is_alive()
    assert server._request_slots._value == server.max_outstanding_requests
    with request(server, 'GET', '/ordinary') as client:
        assert b'200 OK' in response(client)


def test_closed_replay_releases_prefix_even_with_strong_socket_reference():
    from agent_py_agent.agent.gateway_parts.http_body_readiness import PrefetchedSocket

    left, right = socket.socketpair()
    wrapped = PrefetchedSocket(left, bytearray(b'not-yet-read'))
    try:
        wrapped.close()
        assert wrapped.fileno() == -1 and wrapped._prefix is None
    finally:
        wrapped.close()
        right.close()


def test_fragmented_header_search_only_revisits_boundary_suffix(server, monkeypatch):
    from queue import Queue
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import http_lanes

    original = http_lanes.complete_request_length
    work, progress = [], Queue()
    epoch, ticks, clock_lock = time.monotonic(), [0], threading.Lock()

    def budget_now():
        # 资格只证明逐段解析的线性工作量，不要求20次TCP往返挤进TinyServer的0.3s。
        # 仍是有界单调时钟；原真实截止/迟到header/过载由下方独立用例验证。
        with clock_lock:
            ticks[0] += 1
            return epoch + ticks[0] / 1_000_000

    monkeypatch.setattr(http_lanes, 'time', SimpleNamespace(**{**vars(time), 'monotonic': budget_now}))

    def observe(prefix, **options):
        work.append(len(prefix) - options['search_from'])
        result = original(prefix, **options)
        progress.put(len(prefix))
        return result

    def acknowledged(expected):
        # 每段仍须真实selector/parser确认；不靠sleep猜处理完成，不让TCP合并吞掉分段覆盖。
        deadline, received = time.monotonic() + 1, 0
        while received < expected:
            received = progress.get(timeout=max(0, deadline - time.monotonic()))
            assert received <= expected

    monkeypatch.setattr(http_lanes, 'complete_request_length', observe)
    head = b'GET /ordinary HTTP/1.1\r\nX-Fixture: '
    with connect(server, head) as client:
        acknowledged(len(head))
        sent = len(head)
        for _ in range(20):
            client.sendall(b'x' * 100)
            sent += 100
            acknowledged(sent)
        client.sendall(b'\r\n\r\n')
        assert b'200 OK' in response(client), (len(work), sum(work), ticks[0])
    assert len(work) >= 22
    assert sum(work) <= sent + 4 + 3 * len(work)


@pytest.fixture
def patient_body_server():
    class PatientServer(TinyServer):
        request_socket_timeout = 5.0
        request_queue_timeout = 2.0

    class BodyHandler(Handler):
        def do_POST(self):
            self.server.body_entered.set()
            self.server.received.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.do_GET()

    result = PatientServer(('127.0.0.1', 0), BodyHandler, control_lane_workers=2)
    result.body_entered = threading.Event()
    result.handled = threading.Event()
    result.received = []
    thread = threading.Thread(target=result.serve_forever, kwargs={'poll_interval': 0.01})
    thread.start()
    try:
        yield result
    finally:
        result.shutdown()
        result.server_close()
        thread.join(1)
        assert not thread.is_alive() and not result._lanes._thread.is_alive()


def test_early_headers_with_original_mixed_capacity_keep_slow_small_body_success(patient_body_server):
    server = patient_body_server
    with connect(server, b'POST /ordinary HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{') as client:
        assert server.body_entered.wait(1), '原混合席位可用时应及时进入原body reader'
        # 故意跨原queue=2s，但在原socket=5s内完整；不是提高产品预算或成功重试。
        threading.Event().wait(2.1)
        client.sendall(b'}')
        assert b'200 OK' in response(client)
    assert server.received == [b'{}']


@pytest.mark.parametrize('late_headers', [False, True])
def test_original_queue_deadline_is_not_refreshed_for_overload_or_late_headers(patient_body_server, monkeypatch, late_headers):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import http_lanes

    server = patient_body_server
    clock = [time.monotonic()]
    monkeypatch.setattr(http_lanes, 'time', SimpleNamespace(**{**vars(time), 'monotonic': lambda: clock[0]}))
    blocker = None
    try:
        if not late_headers:
            blocker = connect(server, b'POST /ordinary HTTP/1.1\r\nContent-Length: 9000\r\n\r\nx')
            assert server.body_entered.wait(1)
        prefix = b'POST /ordinary HTTP/1.1\r\nContent-Length: 2\r\n'
        if not late_headers:
            prefix += b'\r\n{'
        with connect(server, prefix) as client:
            eventually(lambda: bool(server._lanes._prefixes))
            if not late_headers:
                eventually(lambda: bool(server._lanes._headers_ready_at))
            clock[0] += 2.1
            if late_headers:
                client.sendall(b'\r\n{}')
            server._lanes._wake()
            assert b'GATEWAY_HTTP_BUSY' in response(client)
        assert server.received == []
        assert not server._lanes._ready_executor._threads or late_headers
    finally:
        if blocker is not None:
            blocker.close()


def test_body_waiting_before_original_deadline_uses_newly_freed_mixed_capacity(patient_body_server):
    server = patient_body_server
    blocker = connect(server, b'POST /ordinary HTTP/1.1\r\nContent-Length: 9000\r\n\r\nx')
    try:
        assert server.body_entered.wait(1)
        server.body_entered.clear()
        with connect(server, b'POST /ordinary HTTP/1.1\r\nContent-Length: 2\r\n\r\n{') as client:
            eventually(lambda: bool(server._lanes._headers_ready_at))
            blocker.close()
            assert server.body_entered.wait(1), '原截止前释放席位须唤醒并推进已完整header的责任'
            client.sendall(b'}')
            assert b'200 OK' in response(client)
        assert server.received[-1] == b'{}'
    finally:
        blocker.close()


def test_replay_factory_never_reconstructs_or_detaches_native_socket(monkeypatch):
    from agent_py_agent.agent.gateway_parts.http_body_readiness import PrefetchedSocket

    left, right = socket.socketpair()
    original_descriptor = left.fileno()

    def forbidden_constructor(*_args, **_options):
        raise AssertionError('回放不应重建原生socket')

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(socket, 'socket', forbidden_constructor)
            wrapped = PrefetchedSocket(left, bytearray(b'prefix'))
            assert wrapped._socket is left and left.fileno() == original_descriptor
            assert wrapped.recv(6) == b'prefix'
        right.sendall(b'native')
        assert wrapped.recv(6) == b'native'
        wrapped.close()
        assert left.fileno() == -1
        # 再次close只作用于同一已关闭原生对象，不对旧descriptor数字做盲close。
        other_left, other_right = socket.socketpair()
        try:
            wrapped.close()
            other_left.sendall(b'live')
            assert other_right.recv(4) == b'live'
        finally:
            other_left.close()
            other_right.close()
    finally:
        left.close()
        right.close()


def test_replay_file_reference_preserves_original_native_lifetime():
    from agent_py_agent.agent.gateway_parts.http_body_readiness import PrefetchedSocket

    left, right = socket.socketpair()
    wrapped = PrefetchedSocket(left, bytearray(b'prefix\n'))
    reader = wrapped.makefile('rb')
    try:
        wrapped.close()
        assert left.fileno() >= 0
        assert reader.readline() == b'prefix\n'
        right.sendall(b'native\n')
        assert reader.readline() == b'native\n'
        reader.close()
        assert left.fileno() == -1 and wrapped._prefix is None
    finally:
        reader.close()
        wrapped.close()
        right.close()
