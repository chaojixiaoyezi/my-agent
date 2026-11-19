"""MAX2-GW-02 / MAX1-X4：慢 /control 占满控制池时，公开状态读与停止路由仍走独立执行池。

真实回环连接经 GatewayBoundedHTTPServer + HTTPLaneRouter 往返；处理器只按正文命令模拟慢 /compact，
不 mock 选择器、执行器或分类函数。选择器处于认证之前，只按首行路由选池，绝不读 /control 正文。
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler

import pytest

from agent_py_agent.agent.gateway_parts.bounded_http_server import GatewayBoundedHTTPServer
from agent_py_agent.agent.gateway_parts.http_lanes import (
    HTTP_FAST_LANE_WORKERS_COUNT,
    classify_peeked_request,
    control_sublane,
)


class LaneServer(GatewayBoundedHTTPServer):
    max_request_workers = 4
    max_outstanding_requests = 32
    request_socket_timeout = 3.0
    request_queue_timeout = 2.0


class SlowControlHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    single_request_connection = True

    def log_message(self, _format, *_args):
        pass

    def _reply(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply({'path': self.path, 'thread': threading.current_thread().name})

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(length) or b'{}')
        command = str(body.get('command') or '')
        thread = threading.current_thread().name
        if command.startswith('/compact'):
            # 模拟手动 /compact 在控制线程上同步调用摘要模型。
            with self.server.test_lock:
                self.server.slow_started += 1
            self.server.release.wait(10)
        self._reply({'command': command, 'thread': thread})


@pytest.fixture
def lane_server():
    server = LaneServer(('127.0.0.1', 0), SlowControlHandler, control_lane_workers=2)
    server.release = threading.Event()
    server.test_lock = threading.Lock()
    server.slow_started = 0
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()
        assert not server._lanes._thread.is_alive()


def _eventually(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, '等待真实连接状态收敛超时'
        time.sleep(0.005)


def _post_control_bytes(command: str, *, path: str = '/control') -> bytes:
    body = json.dumps({'command': command, 'conversation_id': 'c1', 'user_id': 'u1', 'channel': 'chat'}).encode()
    head = (f'POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n'
            f'Content-Length: {len(body)}\r\n\r\n').encode()
    return head + body


def _open(server, data: bytes | None = None, *, split_body: bool = False) -> socket.socket:
    client = socket.create_connection(server.server_address, timeout=5)
    if data is not None:
        if split_body:
            # 与 http.client 一样先发头、再发正文两次 send，覆盖选择器只读到头的交错。
            head, sep, body = data.partition(b'\r\n\r\n')
            client.sendall(head + sep)
            time.sleep(0.01)
            client.sendall(body)
        else:
            client.sendall(data)
    return client


def _read_all(client: socket.socket) -> bytes:
    chunks = []
    while True:
        try:
            chunk = client.recv(65536)
        except ConnectionResetError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b''.join(chunks)


def _json_reply(client: socket.socket) -> tuple[bytes, dict]:
    raw = _read_all(client)
    head, _, body = raw.partition(b'\r\n\r\n')
    return head, (json.loads(body) if body.startswith(b'{') else {})


def test_control_sublane_splits_only_control_routes_by_request_line():
    assert control_sublane(b'GET /status HTTP/1.1\r\n') == 'status'
    assert control_sublane(b'GET /metrics?x=1 HTTP/1.1\r\n') == 'status'
    assert control_sublane(b'POST /stop HTTP/1.1\r\n') == 'stop'
    assert control_sublane(b'POST /client/agent-stop HTTP/1.1\r\n') == 'stop'
    assert control_sublane(b'GET /control-status/op-1 HTTP/1.1\r\n') == 'control'
    # 认证前不看正文：任何 /control 正文（含 /stop）都留在原控制池，正文无法把请求带进更快的池。
    for command in ('/stop', '/interrupt', '/compact', '/status'):
        assert control_sublane(_post_control_bytes(command)) == 'control'
    assert control_sublane(b'POST /status HTTP/1.1\r\n') == 'control'
    assert control_sublane(b'GET /stop HTTP/1.1\r\n') == 'control'
    # 原车道分类不变：子池只在控制车道内部细分。
    assert classify_peeked_request(b'GET /status HTTP/1.1\r\n') == 'control'
    assert classify_peeked_request(b'POST /control HTTP/1.1\r\n') == 'control'


def test_status_metrics_and_stop_routes_are_served_while_control_lane_is_saturated(lane_server):
    server = lane_server
    workers = server._lanes._control_executor._max_workers
    slow = [_open(server, _post_control_bytes('/compact'), split_body=True) for _ in range(workers)]
    queued = []
    try:
        _eventually(lambda: server.slow_started == workers)
        # 再排一条慢命令：原 FIFO 控制队列里 /stop 若同池必然排在它后面。
        queued.append(_open(server, _post_control_bytes('/compact status'), split_body=True))
        _eventually(lambda: server._lanes._control_executor._work_queue.qsize() >= 1)
        timings = {}
        for name, data in (('status', b'GET /status HTTP/1.1\r\nHost: localhost\r\n\r\n'),
                           ('metrics', b'GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n'),
                           ('agent_stop', _post_control_bytes('', path='/client/agent-stop')),
                           ('admin_stop', _post_control_bytes('', path='/stop'))):
            started = time.monotonic()
            with _open(server, data, split_body=name == 'agent_stop') as client:
                head, payload = _json_reply(client)
            timings[name] = time.monotonic() - started
            assert b'200 OK' in head, (name, head)
            expected = 'gateway-http-stop' if 'stop' in name else 'gateway-http-status'
            assert payload['thread'].startswith(expected), (name, payload)
        assert max(timings.values()) < 1.0, timings
        # 慢命令仍持有全部控制线程，证明快速池没有借用或抢占它们。
        assert server.slow_started == workers and not server.release.is_set()
        assert len(server._lanes._status_executor._threads) <= HTTP_FAST_LANE_WORKERS_COUNT
        assert len(server._lanes._stop_executor._threads) <= HTTP_FAST_LANE_WORKERS_COUNT
    finally:
        server.release.set()
        for client in [*slow, *queued]:
            client.close()
    _eventually(lambda: all(slots._value == server.max_outstanding_requests
                            for slots in server._lanes._lane_slots.values()))


def test_control_stop_body_is_not_inspected_before_auth(lane_server):
    server = lane_server
    request = _post_control_bytes('/stop')
    head, sep, body = request.partition(b'\r\n\r\n')
    with _open(server, head + sep + body[:4]) as client:
        # 只到达头部即按原控制车道移交，由认证后的原 worker 读完正文；选择器不等也不解析正文。
        _eventually(lambda: server._lanes._control_executor._threads, timeout=server.request_queue_timeout / 2)
        client.sendall(body[4:])
        reply_head, payload = _json_reply(client)
    assert b'200 OK' in reply_head
    assert payload['command'] == '/stop' and payload['thread'].startswith('gateway-http-control')


def test_fast_lanes_shut_down_with_router():
    server = LaneServer(('127.0.0.1', 0), SlowControlHandler, control_lane_workers=1)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    try:
        with _open(server, b'GET /status HTTP/1.1\r\nHost: localhost\r\n\r\n') as client:
            assert b'200 OK' in _read_all(client)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    assert server._lanes._status_executor._shutdown and server._lanes._stop_executor._shutdown


def test_manual_compact_occupies_at_most_half_the_control_lane(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts import control_service as control

    release = threading.Event()
    entered = []
    entered_lock = threading.Lock()

    def slow_summary(_agent, _paths, _command, _scope):
        # 真实同步摘要：完成前不返回任何回执，回执只能是完成后的真实结果。
        with entered_lock:
            entered.append(threading.current_thread().name)
        assert release.wait(5), '测试放行超时'
        return control.ConversationControlResult('compact', True, 'Context compacted · generation 2')

    monkeypatch.setattr(control, '_execute_registered_compact_control', slow_summary)
    agent = SimpleNamespace(config=SimpleNamespace(gateway_http_control_lane_workers=4))
    command = parse_conversation_control('/compact')
    scope = control.GatewayControlScope('user', 'chat', 'conversation')
    results = []
    workers = [threading.Thread(target=lambda: results.append(control._execute_compact_control(agent, None, command, scope)))
               for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        _eventually(lambda: len(entered) == 2)
        started = time.monotonic()
        busy = control._execute_compact_control(agent, None, command, scope)
        # 第三个手动压缩不排队、不占控制线程等待，立即得到不可误读为成功的 typed 忙碌。
        assert time.monotonic() - started < 0.5
        assert busy.ok is False and busy.error_code == 'COMPACT_CONTROL_BUSY'
        assert len(entered) == 2
        from agent_py_agent.agent.contracts.error_taxonomy import error_contract
        assert error_contract('COMPACT_CONTROL_BUSY').retryable is True
    finally:
        release.set()
        for worker in workers:
            worker.join(5)
    assert [result.ok for result in results] == [True, True]
    # 名额随完成归还；未启用独立控制车道（0）时沿原行为不设上限。
    release.clear()
    release.set()
    assert control._execute_compact_control(agent, None, command, scope).ok is True
    unbounded = SimpleNamespace(config=SimpleNamespace(gateway_http_control_lane_workers=0))
    assert control._execute_compact_control(unbounded, None, command, scope).ok is True
    assert control._MANUAL_COMPACT_ADMISSION._active == 0
