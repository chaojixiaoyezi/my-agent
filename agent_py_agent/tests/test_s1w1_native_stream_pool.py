"""原生流池容量、线程所有权及真实HTTP移交回归。"""
from __future__ import annotations

import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.business_stream_pool import (
    BusinessStreamPool,
    NativeStreamStopped,
    stream_services_view,
)
from agent_py_agent.agent.gateway_parts.request_event_hub import RequestEventHub


def eventually(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, '流资源未在期限内释放'
        time.sleep(0.005)


@pytest.fixture
def pool():
    result = BusinessStreamPool(max_streams=2, per_owner_max=1, write_timeout_seconds=0.5)
    result.start()
    try:
        yield result
    finally:
        result.close()


def test_global_owner_limits_and_idempotent_release(pool):
    alice = pool.reserve(('alice', '/a'))
    assert alice is not None
    assert pool.reserve(('alice', '/a')) is None
    bob = pool.reserve(('bob', '/b'))
    assert bob is not None
    assert pool.reserve(('carol', '/c')) is None
    alice.release()
    alice.release()
    assert pool.active_count == 1
    replacement = pool.reserve(('alice', '/a'))
    assert replacement is not None
    replacement.release()
    bob.release()
    assert pool.active_count == 0
    pool.close()
    assert pool.reserve(('alice', '/a')) is None


def test_ticket_wait_observes_stop_and_peer_eof(pool):
    first = pool.reserve(('alice', '/a'))
    first.stop()
    with pytest.raises(NativeStreamStopped):
        first.wait(30)
    first.release()
    ticket = pool.reserve(('alice', '/a'))
    writer, peer = socket.socketpair()
    try:
        ticket.attach(writer)
        peer.close()
        with pytest.raises(NativeStreamStopped):
            ticket.wait(30)
    finally:
        writer.close()
        peer.close()
        ticket.release()


def test_submit_encodes_checks_and_closes_every_frame(pool):
    writer, peer = socket.socketpair()
    ticket = pool.reserve(('alice', '/a'))
    ticket.attach(writer)
    seen, closed, owners = [], [], []

    def events():
        try:
            yield 'one'
            yield 'two'
        finally:
            owners.append(threading.current_thread().name)
            closed.append(pool.active_count)

    pool.submit(ticket, writer, events(), lambda event: (event + '\n').encode(), seen.append)
    peer.settimeout(1)
    data = b''
    try:
        while chunk := peer.recv(1024):
            data += chunk
    finally:
        peer.close()
    eventually(lambda: pool.active_count == 0)
    assert data == b'one\ntwo\n'
    assert seen == ['one', 'two']
    assert closed == [1], '名额不能在迭代器关闭前释放'
    assert owners[0].startswith('gateway-native-stream')
    assert writer.fileno() == -1


def test_close_wakes_waiting_writer_without_advancing_from_closer(pool):
    writer, peer = socket.socketpair()
    ticket = pool.reserve(('alice', '/a'))
    ticket.attach(writer)
    entered, finished = threading.Event(), threading.Event()
    close_threads = []

    def events():
        try:
            entered.set()
            ticket.wait(30)
            yield b'not-after-stop'
        finally:
            close_threads.append(threading.current_thread().name)
            finished.set()

    pool.submit(ticket, writer, events())
    assert entered.wait(1)
    start = time.monotonic()
    pool.close()
    assert finished.wait(1)
    eventually(lambda: pool.active_count == 0)
    assert time.monotonic() - start < 1
    assert close_threads[0].startswith('gateway-native-stream')
    peer.close()


def test_services_view_does_not_mutate_shared_mapping_or_object(pool):
    ticket = pool.reserve(('alice', '/a'))
    sentinel = object()
    for shared in ({'wait': sentinel, 'event_signal': sentinel, 'other': sentinel},
            SimpleNamespace(wait=sentinel, event_signal=sentinel, other=sentinel)):
        view = stream_services_view(shared, ticket)
        get = (lambda source, name: source[name]) if isinstance(shared, dict) else getattr
        assert get(shared, 'wait') is sentinel and get(shared, 'event_signal') is sentinel
        assert get(view, 'wait') == ticket.wait
        assert get(view, 'event_signal') is None
        assert get(view, 'other') is sentinel
    ticket.release()


@pytest.mark.parametrize('as_mapping', [True, False])
def test_services_view_keeps_real_notifications_and_interrupts_wait(pool, monkeypatch, as_mapping):
    hub = RequestEventHub(ring_events=8, topic_capacity=2, subscribers_per_topic=2, idle_seconds=1)
    signal = hub.signal(('owner', 'alice'))
    ticket = pool.reserve(('alice', '/a'))
    services = {'event_signal': signal, 'wait': object()}
    shared = services if as_mapping else SimpleNamespace(**services)
    view = stream_services_view(shared, ticket)
    wrapped = view['event_signal'] if as_mapping else view.event_signal
    entered, finished = threading.Event(), threading.Event()
    outcomes = []
    original_wait = signal.wait

    def waiting(timeout, stop):
        assert stop is ticket.stop_event
        entered.set()
        return original_wait(timeout, stop)

    def consume():
        try:
            wrapped.wait(30)
        except NativeStreamStopped:
            outcomes.append('stopped')
        finally:
            finished.set()

    thread = threading.Thread(target=consume, daemon=True)
    try:
        assert wrapped is not signal
        assert hub.publish(('owner', 'alice'), 'chunk', 'r', {}) == 1
        assert wrapped.wait(0) is True
        assert wrapped.wait(0) is False
        monkeypatch.setattr(signal, 'wait', waiting)
        thread.start()
        assert entered.wait(1)
        ticket.stop()
        assert finished.wait(1), '通知等待必须感知本流停止，不能阻塞到心跳超时'
        thread.join(1)
        assert outcomes == ['stopped']
        assert (shared['event_signal'] if as_mapping else shared.event_signal) is signal
        assert not signal._subscription._closed
        assert hub.publish(('owner', 'alice'), 'chunk', 'r', {}) == 2
        assert original_wait(0) is True, '停止观察不能关闭原业务信号'
    finally:
        ticket.stop()
        hub.close()
        if thread.ident is not None:
            thread.join(1)
        ticket.release()


@pytest.mark.parametrize('as_mapping', [True, False])
def test_services_view_without_ticket_preserves_original_signal_and_wait(as_mapping):
    signal, wait = object(), object()
    services = {'event_signal': signal, 'wait': wait}
    shared = services if as_mapping else SimpleNamespace(**services)
    view = stream_services_view(shared, None)
    get = (lambda source, name: source[name]) if as_mapping else getattr
    assert get(view, 'event_signal') is signal
    assert get(view, 'wait') is wait
    assert get(shared, 'event_signal') is signal


def test_services_view_factory_creates_independent_notification_consumers(pool):
    hub = RequestEventHub(ring_events=8, topic_capacity=2, subscribers_per_topic=2, idle_seconds=1)
    sentinel = object()
    shared = SimpleNamespace(event_signal=sentinel, host=SimpleNamespace(
        business_event_signal_factory=lambda: hub.signal(('owner', 'alice'))))
    ticket = pool.reserve(('alice', '/a'))
    try:
        first = stream_services_view(shared, ticket).event_signal
        second = stream_services_view(shared, ticket).event_signal
        assert first is not second
        assert hub.publish(('owner', 'alice'), 'chunk', 'r', {}) == 1
        assert first.wait(0) is True
        assert second.wait(0) is True, '一个流消费通知不能清掉另一条流的唤醒'
        assert first.wait(0) is second.wait(0) is False
        assert shared.event_signal is sentinel
    finally:
        hub.close()
        ticket.release()


def test_stopped_queued_stream_releases_and_closes_on_worker(pool):
    block, entered = threading.Event(), threading.Event()

    def occupy():
        entered.set()
        block.wait(2)

    # 用满真正的执行器，关闭时排队流必须等待writer接管清理而非在关闭线程中关闭。
    blockers = [pool._executor.submit(occupy) for _ in range(2)]
    assert entered.wait(1)
    ticket = pool.reserve(('alice', '/a'))
    writer, peer = socket.socketpair()
    ticket.attach(writer)
    try:
        pool.submit(ticket, writer, iter([b'not-sent']))
        pool.close()
        assert pool.active_count == 1
        block.set()
        eventually(lambda: pool.active_count == 0)
        peer.settimeout(1)
        assert peer.recv(1) == b''
        assert writer.fileno() == -1
    finally:
        block.set()
        for future in blockers:
            future.result(1)
        peer.close()


@pytest.fixture
def native(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.tests.test_mega_business_api_actions import observed_native_task

    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    config = AgentConfig(model_backend='echo', enable_tools=False, business_spaces_enabled=True,
        memory_path='memory.jsonl')
    config.gateway_user_inflight_limit = 1
    agent = SimpleAgent(config, tmp_path / 'workspace')
    # Agent初始化会重建规范配置，集成前的新键须放到server实际消费的实例。
    agent.config.gateway_native_stream_max = 2
    agent.config.gateway_http_control_lane_workers = 2
    server = GatewayHTTPServer(0, gateway_paths_from_root(agent.home_paths.root / 'gateway'),
        params=GatewayHTTPServerParams(agent=agent))
    server.start()
    observed_native_task((server, agent))
    assert server.business_stream_pool is not None
    assert server.server._lanes is not None
    try:
        yield server, agent
    finally:
        server.stop()


def open_stream(native, request_id='open'):
    from agent_py_agent.tests.test_mega_business_api_actions import open_native_observation

    return open_native_observation(native, {'request_id': request_id, 'filter': {}})


def close_stream(conn, response):
    response.close()
    conn.close()


def test_http_open_stream_releases_single_general_worker(native, monkeypatch):
    from agent_py_agent.agent.gateway_parts.bounded_http_server import GatewayBoundedHTTPServer
    from agent_py_agent.tests.test_mega_business_api_actions import background_call

    server, agent = native
    server.stop()
    monkeypatch.setattr(GatewayBoundedHTTPServer, 'max_request_workers', 1)
    agent.config.gateway_http_control_lane_workers = 0
    server.start()
    conn, response = open_stream(native)
    try:
        assert response.status == 200
        assert json.loads(response.readline())['stream_item'] == 'snapshot'
        pool = server.business_stream_pool
        eventually(lambda: pool.active_count == 1)
        eventually(lambda: server.server._request_slots._value == server.server.max_outstanding_requests)
        start = time.monotonic()
        status, value = background_call(native, 'tasks.list')
        assert status == 200 and value['error'] is None
        assert time.monotonic() - start < 1
        assert pool.active_count == 1
    finally:
        close_stream(conn, response)
    eventually(lambda: pool.active_count == 0, 2)


def test_http_full_pool_rejects_before_stream_factory(native, monkeypatch):
    from agent_py_agent.agent.gateway_parts import business_event_stream

    conn, response = open_stream(native)
    assert response.status == 200
    assert json.loads(response.readline())['stream_item'] == 'snapshot'
    called = []
    original = business_event_stream.stream_business_events

    def factory(request, *, authenticated_context, services):
        called.append(request)
        return original(request, authenticated_context=authenticated_context, services=services)

    monkeypatch.setattr(business_event_stream, 'stream_business_events', factory)
    try:
        other, busy = open_stream(native, 'full')
        try:
            assert busy.status == 503
            assert json.loads(busy.read()) == {'error': {'code': 'GATEWAY_STREAM_BUSY'}}
            assert busy.getheader('Retry-After') == '1'
            assert busy.getheader('Connection') == 'close'
            assert called == []
        finally:
            close_stream(other, busy)
    finally:
        close_stream(conn, response)


def test_http_stop_ends_live_stream_within_two_seconds(native):
    server, _agent = native
    conn, response = open_stream(native)
    assert response.status == 200
    assert json.loads(response.readline())['stream_item'] == 'snapshot'
    pool = server.business_stream_pool
    start = time.monotonic()
    try:
        server.stop()
        assert response.read() == b''
        eventually(lambda: pool.active_count == 0, 2)
        assert time.monotonic() - start < 2
        assert server.business_stream_pool is None
    finally:
        close_stream(conn, response)


def test_http_disconnect_returns_reservation_within_two_seconds(native):
    server, _agent = native
    conn, response = open_stream(native)
    assert response.status == 200
    assert json.loads(response.readline())['stream_item'] == 'snapshot'
    pool = server.business_stream_pool
    assert pool.active_count == 1
    close_stream(conn, response)
    eventually(lambda: pool.active_count == 0, 2)


def test_http_ndjson_bytes_match_with_pool_enabled_and_disabled(native, tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import business_event_stream
    from agent_py_agent.tests.test_business_v12_f7_observation import fixture
    from agent_py_agent.tests.test_mega_business_stream_resume import append

    # 从原typed生产者取得同一组合法帧，再通过两种真实HTTP运输逐字节比较。
    case = fixture(tmp_path / 'frame-source')
    typed = business_event_stream.stream_business_events({'request_id': 'parity-source',
        'filter': {'space_id': 'space-a'}}, authenticated_context=case.actor,
        services=SimpleNamespace(observation=case.service, wait=lambda _seconds: None))
    source = typed.events()
    frames = [next(source)]
    append(case)
    frames.append(next(source))
    source.close()
    assert [frame['stream_item'] for frame in frames] == ['snapshot', 'event']
    expected = b''.join((json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n').encode() for event in frames)
    advances, closes = [], []

    def factory(request, *, authenticated_context, services):
        def events():
            try:
                for frame in frames:
                    advances.append(threading.current_thread().name)
                    yield frame
            finally:
                closes.append(threading.current_thread().name)
        return SimpleNamespace(events=events)

    monkeypatch.setattr(business_event_stream, 'stream_business_events', factory)
    server, agent = native
    results = []
    for enabled in (True, False):
        if not enabled:
            server.stop()
            agent.config.gateway_native_stream_max = 0
            server.start()
            assert server.business_stream_pool is None
        conn, response = open_stream(native, 'same-request')
        try:
            assert response.status == 200
            assert response.getheader('Content-Type') == 'application/x-ndjson; charset=utf-8'
            assert response.getheader('Cache-Control') == 'no-store'
            results.append(response.read())
        finally:
            close_stream(conn, response)
        if enabled:
            eventually(lambda: server.business_stream_pool.active_count == 0)
    assert results == [expected, expected]
    assert len(advances) == 4 and len(closes) == 2
    assert advances[0].startswith('gateway-http')
    assert advances[1].startswith('gateway-native-stream')
    assert closes[0].startswith('gateway-native-stream')
    assert all(name.startswith('gateway-http') for name in advances[2:] + closes[1:])


@pytest.mark.parametrize('stage', ['factory', 'first', 'submit'])
def test_http_failed_handoff_releases_iterator_fd_and_reservation(native, monkeypatch, stage):
    from agent_py_agent.agent.gateway_parts import business_event_stream
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    server, _agent = native
    pool = server.business_stream_pool
    original = business_event_stream.stream_business_events
    closed, detached = [], []

    class TrackedIterator:
        def __init__(self, iterator):
            self.iterator = iterator

        def __iter__(self):
            return self

        def __next__(self):
            if stage == 'first':
                raise HostAPIError('HOST_NOT_READY', 503)
            return next(self.iterator)

        def close(self):
            closed.append((pool.active_count, threading.current_thread().name))
            self.iterator.close()

    def factory(request, *, authenticated_context, services):
        assert pool.active_count == 1, '必须在构造流前预留额度'
        if stage == 'factory':
            raise HostAPIError('HOST_NOT_READY', 503)
        real = original(request, authenticated_context=authenticated_context, services=services)
        return SimpleNamespace(events=lambda: TrackedIterator(real.events()))

    def fail_submit(ticket, sock, iterator, encode, check):
        detached.append(sock)
        raise RuntimeError('submit_unavailable')

    with monkeypatch.context() as patch:
        patch.setattr(business_event_stream, 'stream_business_events', factory)
        if stage == 'submit':
            patch.setattr(pool, 'submit', fail_submit)
        conn, response = open_stream(native)
        try:
            if stage == 'submit':
                assert response.status == 200
                assert json.loads(response.read())['stream_item'] == 'snapshot'
            else:
                assert response.status == 503
                assert json.loads(response.read()) == {'error': {'code': 'HOST_NOT_READY'}}
        finally:
            close_stream(conn, response)
        eventually(lambda: pool.active_count == 0)
        assert len(closed) == (0 if stage == 'factory' else 1)
        assert all(count == 1 and name.startswith('gateway-http') for count, name in closed)
        assert all(sock.fileno() == -1 for sock in detached)
    conn, response = open_stream(native, 'after-failure')
    try:
        assert response.status == 200
        assert json.loads(response.readline())['stream_item'] == 'snapshot'
    finally:
        close_stream(conn, response)
    eventually(lambda: pool.active_count == 0, 2)


def test_attach_after_pool_close_preserves_stop_and_handler_reservation(pool):
    ticket = pool.reserve(('alice', '/a'))
    pool.close()
    writer, peer = socket.socketpair()
    try:
        ticket.attach(writer)
        peer.settimeout(1)
        assert peer.recv(1) == b''
        with pytest.raises(NativeStreamStopped):
            ticket.wait(0)
        assert pool.active_count == 1, '尚在handler的预留不能被close提前释放'
    finally:
        writer.close()
        peer.close()
        ticket.release()
    assert pool.active_count == 0


def test_queued_handoff_is_closed_only_by_a_writer(pool):
    block = threading.Event()
    blockers = [pool._executor.submit(block.wait, 2) for _ in range(2)]
    ticket = pool.reserve(('alice', '/a'))
    writer, peer = socket.socketpair()
    ticket.attach(writer)
    closed = []

    class Iterator:
        def __iter__(self):
            return self

        def __next__(self):
            raise AssertionError('已停止的排队流不得推进')

        def close(self):
            closed.append((threading.current_thread().name, pool.active_count))

    try:
        pool.submit(ticket, writer, Iterator())
        pool.close()
        assert ticket.stop_event.is_set()
        assert pool.active_count == 1
        assert closed == []
        block.set()
        for future in blockers:
            future.result(1)
        eventually(lambda: pool.active_count == 0)
        assert len(closed) == 1 and closed[0][0].startswith('gateway-native-stream')
        assert closed[0][1] == 1
    finally:
        block.set()
        peer.close()


def test_submit_enqueue_then_worker_start_failure_leaves_handler_as_only_cleaner(pool, monkeypatch):
    ticket = pool.reserve(('alice', '/a'))
    writer, peer = socket.socketpair()
    ticket.attach(writer)
    closed = []

    class Iterator:
        def __iter__(self):
            return self

        def __next__(self):
            raise AssertionError('提交未确认的writer不得推进')

        def close(self):
            closed.append(threading.current_thread().name)

    iterator = Iterator()

    def fail_start():
        raise RuntimeError('thread_start_unavailable')

    try:
        with monkeypatch.context() as patch:
            patch.setattr(pool._executor, '_adjust_thread_count', fail_start)
            with pytest.raises(RuntimeError, match='thread_start_unavailable'):
                pool.submit(ticket, writer, iterator)
        # 模拟未移交handler的finally，随后再让执行器处理已入队的旧WorkItem。
        iterator.close()
        writer.close()
        ticket.release()
        pool._executor.submit(lambda: None).result(1)
        assert closed == [threading.current_thread().name]
        assert pool.active_count == 0
        assert pool._executor._work_queue.qsize() == 0
    finally:
        writer.close()
        peer.close()
        ticket.release()


def test_http_socket_wrapper_failure_closes_detached_descriptor(native, monkeypatch):
    import os

    original = socket.socket
    detached = []

    def fail_wrapper(family=None, type=None, proto=None, fileno=None):
        if family is None and fileno is not None:
            detached.append(fileno)
            raise OSError('socket_wrapper_unavailable')
        return original(socket.AF_INET if family is None else family,
            socket.SOCK_STREAM if type is None else type, 0 if proto is None else proto, fileno=fileno)

    conn = response = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(socket, 'socket', fail_wrapper)
            conn, response = open_stream(native)
            assert response.status == 200
            assert json.loads(response.readline())['stream_item'] == 'snapshot'
            assert response.read() == b''
            eventually(lambda: native[0].business_stream_pool.active_count == 0)
        assert len(detached) == 1
        with pytest.raises(OSError):
            os.fstat(detached[0])
    finally:
        if conn is not None:
            close_stream(conn, response)
        for fd in detached:
            try:
                socket.close(fd)
            except OSError:
                pass


def test_http_stop_before_first_frame_is_normal_cancellation(native, monkeypatch):
    import http.client

    from agent_py_agent.agent.gateway_parts import business_event_stream

    server, _agent = native
    entered, done = threading.Event(), threading.Event()
    errors, reader_errors = [], []

    def factory(request, *, authenticated_context, services):
        def events():
            entered.set()
            services.wait(30)
            yield {'never': 'sent'}
        return SimpleNamespace(events=events)

    def reader():
        try:
            conn, response = open_stream(native)
            close_stream(conn, response)
        except http.client.RemoteDisconnected as error:
            reader_errors.append(error)
        finally:
            done.set()

    monkeypatch.setattr(business_event_stream, 'stream_business_events', factory)
    monkeypatch.setattr(server.server, 'handle_error', lambda _sock, _addr: errors.append(True))
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    assert entered.wait(1)
    server.business_stream_pool.close()
    assert done.wait(1)
    thread.join(1)
    assert errors == []
    assert len(reader_errors) == 1
    assert server.business_stream_pool.active_count == 0


def test_positive_feature_config_does_not_create_second_drain_executor(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_for_root
    from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
    from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case

    with curator_case(tmp_path, monkeypatch) as (agent, _service, _backend, gate, _claim, _close):
        control = coordinator_for_root(gate.paths.root, context=gate.paths).control_plane
        runtime = GatewayDrainRuntime(control)
        agent.config.gateway_native_stream_max = 2
        agent.config.gateway_http_control_lane_workers = 2
        server = GatewayHTTPServer(0, gate.paths, params=GatewayHTTPServerParams(
            agent=agent, control_plane=control, drain_runtime=runtime))
        try:
            server.start()
            assert server.business_stream_pool is None
            assert server.server._lanes is None
            assert server.server._request_executor is runtime._resources['http/requests'].owner
            runtime.require_original()
            runtime.stop_producers()
        finally:
            server.stop()
        assert all(group.observe(time.monotonic() + 2) == 'settled' for group in runtime._resources.values())


def test_stop_between_frames_does_not_advance_iterator_again(pool):
    writer, peer = socket.socketpair()
    ticket = pool.reserve(('alice', '/a'))
    ticket.attach(writer)
    sent, proceed = threading.Event(), threading.Event()
    advanced, closed = [], []

    class GatedSocket:
        def sendall(self, data):
            writer.sendall(data)
            sent.set()
            assert proceed.wait(1)

        def shutdown(self, how):
            writer.shutdown(how)

        def close(self):
            writer.close()

    class Iterator:
        def __iter__(self):
            return self

        def __next__(self):
            advanced.append(True)
            assert len(advanced) == 1, '停止后不得推进下一帧'
            return b'one\n'

        def close(self):
            closed.append((threading.current_thread().name, pool.active_count))

    try:
        pool.submit(ticket, GatedSocket(), Iterator())
        assert sent.wait(1)
        pool.close()
        proceed.set()
        eventually(lambda: pool.active_count == 0)
        assert advanced == [True]
        assert len(closed) == 1 and closed[0][0].startswith('gateway-native-stream')
        assert closed[0][1] == 1 and writer.fileno() == -1
    finally:
        proceed.set()
        peer.close()
