"""主模型 HTTP 复用的本地行为回归：EOF、独立重试记账与重定向边界。"""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import gateway_helpers as gateway
from agent_py_agent.agent.backends import keepalive_transport as transport
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.errors import ProviderResponseError, ProviderTransientError
from agent_py_agent.agent.backends.factory import get_backend
from agent_py_agent.agent.backends.http import HttpBackend, StreamCall
from agent_py_agent.agent.settings.config import AgentConfig


# LLM: 只监听回环，记录真实 accept、请求体和连接头；事件控制终止块及空闲关闭的交错。
# 类用途: 提供可观测的 HTTP/1.1 JSON、chunked SSE 和重定向响应。
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        state = self.server.state
        body = self.rfile.read(int(self.headers["Content-Length"]))
        with state.lock:
            state.requests.append((self.client_address, self.path, self.headers.get("Connection"), body))
            number = len(state.requests)
        try:
            if self.path == "/redirect":
                self.send_response(307)
                self.send_header("Location", state.redirect_url)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                data = b'data: {"ok":true}\n\ndata: [DONE]\n\n'
                packet = f"{len(data):x}\r\n".encode() + data + b"\r\n"
                self.wfile.write(packet + (b"" if state.delay_chunk else b"0\r\n\r\n"))
                self.wfile.flush()
                if state.delay_chunk:
                    state.release_chunk.wait(2)
                    self.wfile.write(b"0\r\n\r\n")
                return
            data = json.dumps({"ok": True, "value": "x" * 128}).encode()
            self.send_response(400 if self.path == "/error" else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
            if self.path == "/stale" and number == 1:
                state.close_idle.wait(2)
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                state.idle_closed.set()
        except OSError:
            pass

    def log_message(self, *_args):
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        sock, address = super().get_request()
        with self.state.lock:
            self.state.accepted.append(address)
        return sock, address


# LLM: 池在测试进程内隔离，退出关闭全部空闲连接；不改实际配置或决策池的策略。
# 函数用途: 让注册表、socket 和后台服务在线程测试后均能回收。
@pytest.fixture(autouse=True)
def isolated_pools(monkeypatch):
    monkeypatch.setattr(transport, "_MAIN_POOLS", {})
    transport.DECISION_CONNECTION_POOL.close_all()
    yield
    for pool in transport._MAIN_POOLS.values():
        pool.close_all()
    transport.DECISION_CONNECTION_POOL.close_all()


@contextmanager
def _serve():
    state = SimpleNamespace(lock=threading.Lock(), requests=[], accepted=[], delay_chunk=False,
                            release_chunk=threading.Event(), close_idle=threading.Event(),
                            idle_closed=threading.Event(), redirect_url="")
    server = _Server(("127.0.0.1", 0), _Handler)
    server.state = state
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.005), daemon=True)
    thread.start()
    try:
        yield state
    finally:
        state.release_chunk.set()
        state.close_idle.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


def _request(state, pool=None, path="/json", **changes):
    fields = dict(api_base=state.url, api_key="local-test", path=path, payload={"q": "中文"},
                  headers={"Content-Type": "application/json"}, timeout=2.0, connect_timeout=1.0,
                  connection_pool=pool)
    fields.update(changes)
    return gateway.GatewayRequest(**fields)


@pytest.mark.parametrize("capacity,connections,header", [(4, 1, "keep-alive"), (0, 2, "close")])
def test_post_json_reuses_only_an_opted_in_pool(capacity, connections, header):
    pool = transport.main_connection_pool(capacity)
    with _serve() as state:
        for _ in range(2):
            assert gateway.post_json(_request(state, pool))["ok"] is True
        assert len(state.accepted) == connections
        assert [row[2] for row in state.requests] == [header, header]
        assert len(Counter(row[0] for row in state.requests)) == connections


@pytest.mark.parametrize("delayed", [False, True])
def test_sse_consumer_stop_proves_eof_without_waiting(delayed):
    pool = transport.main_connection_pool(4)
    with _serve() as state:
        state.delay_chunk = delayed
        stream = gateway.post_stream_iter(_request(state, pool, path="/stream"))
        seen = []
        for data in stream:
            seen.append(data)
            if data == "[DONE]":
                break
        stopped_at = time.monotonic()
        stream.close()
        assert time.monotonic() - stopped_at < 0.2
        assert seen == ['{"ok":true}', "[DONE]"]
        assert pool.idle_count() == (0 if delayed else 1)
        assert gateway.post_json(_request(state, pool))["ok"] is True
        assert len(state.accepted) == (2 if delayed else 1)


@pytest.mark.parametrize("decision", [False, True])
def test_oversized_json_never_checks_in_an_unread_body(decision, monkeypatch):
    pool = transport.DECISION_CONNECTION_POOL if decision else transport.main_connection_pool(4)
    opened = []
    original = transport._new_connection

    def capture(route, options):
        connection = original(route, options)
        opened.append(connection)
        return connection

    monkeypatch.setattr(transport, "_new_connection", capture)
    strict = dict(max_retries=0, allow_redirects=False, deadline=time.monotonic() + 2) if decision else {}
    with _serve() as state:
        with pytest.raises(ProviderResponseError, match="字节上限"):
            gateway.post_json(_request(state, pool, max_response_bytes=16, **strict))
        assert pool.idle_count() == 0
        assert len(opened) == 1 and opened[0].sock is None
        assert gateway.post_json(_request(state, pool, **strict))["ok"] is True
        assert len(state.accepted) == 2


@pytest.mark.parametrize("max_retries", [None, 0])
def test_peer_close_after_checkout_uses_one_accounted_fresh_retry(max_retries, monkeypatch):
    pool = transport.main_connection_pool(4)
    dropped, waited, events = [], [], []
    original_drop = pool.drop_idle
    original_quiet = transport._connection_quiet

    def drop(route):
        dropped.append(route)
        return original_drop(route)

    monkeypatch.setattr(pool, "drop_idle", drop)
    monkeypatch.setattr(gateway, "_provider_retry_wait", waited.append)
    with _serve() as state:
        request = _request(state, pool, path="/stale", max_retries=max_retries)
        assert gateway.post_json(request)["ok"] is True

        # 将对端关闭安排在真实零等待存活检查之后，确定性覆盖检查与发送之间的竞态。
        def close_after_probe(connection):
            quiet = original_quiet(connection)
            state.close_idle.set()
            assert state.idle_closed.wait(1)
            return quiet

        monkeypatch.setattr(transport, "_connection_quiet", close_after_probe)
        with gateway.provider_attempt_observer(events.append):
            if max_retries == 0:
                with pytest.raises(ProviderTransientError) as error:
                    gateway.post_json(request)
                assert error.value.__cause__.stale_reused_connection is True
            else:
                assert gateway.post_json(request)["ok"] is True
        failed = [e for e in events if e["status"] == "failed"]
        assert len(failed) == 1
        assert failed[0]["reason"] == "stale_reused_connection"
        assert failed[0]["retry_wait_seconds"] == 0.0
        assert failed[0]["retry_scheduled"] is (max_retries is None)
        assert len(dropped) == 1
        started = [e for e in events if e["status"] == "started"]
        assert len({e["attempt_id"] for e in started}) == (1 if max_retries == 0 else 2)
        assert [e["status"] for e in events] == (["started", "failed"] if max_retries == 0 else
                                                ["started", "failed", "started", "response_opened"])
        assert waited == ([] if max_retries == 0 else [0.0])
        assert len(state.requests) == (1 if max_retries == 0 else 2)
        assert len(state.accepted) == (1 if max_retries == 0 else 2)


def test_pooled_post_redirect_never_reopens_origin_or_destination():
    pool = transport.main_connection_pool(4)
    with _serve() as origin, _serve() as destination:
        origin.redirect_url = destination.url + "/json"
        request = _request(origin, pool, path="/redirect")
        with pytest.raises(urllib.error.HTTPError) as error:
            gateway._open_gateway_request(request)
        assert error.value.code == 307
        error.value.close()
        assert len(origin.requests) == 1
        assert destination.requests == [] and destination.accepted == []
        assert pool.idle_count() == 0


def test_pool_registry_is_single_flight_per_capacity(monkeypatch):
    original = transport.KeepAlivePool
    created, results, errors = [], [], []
    barrier, lock = threading.Barrier(8), threading.Lock()

    def construct(**kwargs):
        pool = original(**kwargs)
        with lock:
            created.append(pool)
        return pool

    def worker():
        try:
            barrier.wait(timeout=2)
            pool = transport.main_connection_pool(4)
            with lock:
                results.append(pool)
        except BaseException as error:
            with lock:
                errors.append(error)

    monkeypatch.setattr(transport, "KeepAlivePool", construct)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert all(not thread.is_alive() for thread in threads) and errors == []
    assert len(results) == 8 and len(created) == 1
    assert all(pool is created[0] for pool in results)
    assert transport.main_connection_pool(4) is created[0]
    assert transport.main_connection_pool(5) is not created[0]
    assert len(created) == 2
    assert created[0].allows_retrying_requests is True
    assert created[0].max_idle_seconds == transport.KEEPALIVE_MAX_IDLE_SECONDS
    assert created[0].max_idle_per_key == 4
    assert transport.main_connection_pool(0) is None
    assert transport.main_connection_pool(-1) is None


def test_drop_idle_closes_only_its_route_outside_the_lock():
    pool = transport.KeepAlivePool()
    first = transport.KeepAliveRoute("http", "first.test", 80)
    other = transport.KeepAliveRoute("http", "other.test", 80)
    closed = []
    for number, route in enumerate((first, first, other)):
        connection = SimpleNamespace(close=lambda n=number: closed.append((n, pool._lock.locked())))
        pool.checkin(route, connection)
    assert pool.drop_idle(first) == 2
    assert closed == [(0, False), (1, False)]
    assert pool.idle_count() == 1 and pool.drop_idle(first) == 0
    assert pool.drop_idle(other) == 1 and closed[-1] == (2, False)


@pytest.mark.parametrize("marker", [None, False, 1, "true"])
def test_pool_without_explicit_retry_permission_keeps_strict_limits(marker):
    pool = SimpleNamespace(checkout=lambda route: None, checkin=lambda route, conn: None)
    if marker is not None:
        pool.allows_retrying_requests = marker
    state = SimpleNamespace(url="http://127.0.0.1")
    with pytest.raises(ValueError, match="复用长连接"):
        _request(state, pool)
    assert _request(state, pool, max_retries=0, allow_redirects=False,
                    deadline=time.monotonic() + 1).connection_pool is pool
    pool.allows_retrying_requests = True
    assert _request(state, pool).max_retries is None


@pytest.mark.parametrize("retrying", [False, True])
@pytest.mark.parametrize("missing", ["checkout", "checkin"])
def test_all_pools_require_both_callables(retrying, missing):
    pool = SimpleNamespace(allows_retrying_requests=retrying, checkout=lambda route: None,
                           checkin=lambda route, connection: None)
    setattr(pool, missing, None)
    with pytest.raises(ValueError, match="复用长连接"):
        _request(SimpleNamespace(url="http://127.0.0.1"), pool, max_retries=0,
                 allow_redirects=False, deadline=time.monotonic() + 1)


def test_main_pool_does_not_relax_send_permit_requirements():
    with pytest.raises(ValueError, match="发送许可"):
        _request(SimpleNamespace(url="http://127.0.0.1"), transport.main_connection_pool(4),
                 send_permit=SimpleNamespace(admit=lambda attempt: None))


@pytest.mark.parametrize("name", ["openai_compatible", "anthropic_compatible", "openai_responses"])
def test_factory_wires_capacity_and_reuses_the_registry_object(name):
    config = AgentConfig()
    config.api_base, config.api_key, config.model_name = "http://127.0.0.1", "local-test", "test"
    config.model_http_max_idle_connections = 4
    backend = get_backend(name, config)
    call = StreamCall("/json", {"q": 1}, {"Content-Type": "application/json"})
    assert backend.model_http_max_idle_connections == 4
    first, second = backend._gateway_request(call), backend._gateway_request(call)
    assert first.connection_pool is second.connection_pool is transport.main_connection_pool(4)


def test_zero_capacity_preserves_the_legacy_envelope_and_missing_attribute_default():
    options = BackendOptions(api_base="http://127.0.0.1", api_key="local-test", model_name="test")
    assert options.model_http_max_idle_connections == 0
    backend = HttpBackend(options)
    call = StreamCall("/json", {"q": 1}, {"Content-Type": "application/json"})
    expected = gateway.GatewayRequest(api_base=backend.api_base, api_key=backend.api_key, path=call.path,
                                      payload=call.payload, headers={"User-Agent": "my-agent/1.0", **call.headers},
                                      timeout=backend.request_timeout,
                                      connect_timeout=backend.connect_timeout)
    actual = backend._gateway_request(call)
    assert actual.connection_pool is None and actual == replace(expected, connection_pool=None)
    del backend.model_http_max_idle_connections
    assert backend._gateway_request(call).connection_pool is None
    config = AgentConfig()
    config.api_base, config.api_key, config.model_name = backend.api_base, "local-test", "test"
    assert get_backend("openai_compatible", config).model_http_max_idle_connections == 0


@pytest.mark.parametrize("value,expected", [(-1, 0), (None, 0), (0, 0), (4, 4), ("4", 4)])
def test_backend_normalizes_explicit_idle_capacity(value, expected):
    options = BackendOptions("http://127.0.0.1", "local-test", "test", model_http_max_idle_connections=value)
    backend = HttpBackend(options)
    assert backend.model_http_max_idle_connections == expected
    assert backend._gateway_request(StreamCall("/models", None, {})).connection_pool is None


@pytest.mark.parametrize("reused", [False, True])
@pytest.mark.parametrize("has_drop", [False, True])
@pytest.mark.parametrize("failure", [BrokenPipeError, ConnectionResetError, http.client.RemoteDisconnected])
def test_pooled_open_marks_only_stale_reused_failures_without_resending(reused, has_drop, failure, monkeypatch):
    sent, closed, dropped = [], [], []

    def send(*args):
        sent.append(args)
        if failure is not http.client.RemoteDisconnected:
            raise failure("connection ended")

    def receive():
        raise failure("connection ended")

    connection = SimpleNamespace(request=send, getresponse=receive, close=lambda: closed.append(True))
    pool = SimpleNamespace(checkout=lambda route: connection if reused else None)
    if has_drop:
        pool.drop_idle = dropped.append
    route = transport.KeepAliveRoute("http", "127.0.0.1", 80)
    target = transport.KeepAliveTarget(pool, route)
    monkeypatch.setattr(transport, "_new_connection", lambda route, options: connection)
    monkeypatch.setattr(transport, "_prepare_reused", lambda *args: None)
    error_type = http.client.RemoteDisconnected if failure is http.client.RemoteDisconnected else urllib.error.URLError
    with pytest.raises(error_type) as error:
        transport.pooled_urlopen(target, urllib.request.Request("http://127.0.0.1/json", data=b"{}"), None)
    assert getattr(error.value, "stale_reused_connection", False) is reused
    assert len(sent) == 1 and closed == [True]
    assert dropped == ([route] if reused and has_drop else [])


@pytest.mark.parametrize("failure", [None, BlockingIOError, OSError, ValueError, TypeError, http.client.HTTPException])
def test_drain_reads_once_in_nonblocking_mode_and_restores_timeout(failure):
    left, right = socket.socketpair()
    left.settimeout(1.25)
    calls, complete = [], [False]

    def read():
        calls.append(left.gettimeout())
        if failure is not None:
            raise failure("not complete")
        complete[0] = True
        return b""

    response = SimpleNamespace(fp=SimpleNamespace(raw=SimpleNamespace(_sock=left)),
                               read=read, isclosed=lambda: complete[0])
    try:
        assert transport.drain_completed_response(response) is (failure is None)
        assert calls == [0.0] and left.gettimeout() == 1.25
    finally:
        left.close()
        right.close()


def test_drain_shortcuts_do_not_read_or_accept_an_unrelated_lease():
    response = SimpleNamespace(isclosed=lambda: True)
    assert transport.drain_completed_response(response) is True
    response.isclosed = lambda: False
    assert transport.drain_completed_response(response) is False
    response._my_agent_keepalive_lease = object()
    assert transport.has_pooled_lease(response) is False


@pytest.mark.parametrize("ending", ["normal", "consumer_stopped", "error", "aborted"])
def test_response_scope_proves_eof_before_close_and_never_reuses_error_or_abort(ending, monkeypatch):
    operations, closed = [], [False]
    connection = SimpleNamespace(sock=object(), close=lambda: operations.append("connection_close"))
    pool = SimpleNamespace(checkin=lambda route, conn: operations.append("checkin"))
    target = transport.KeepAliveTarget(pool, transport.KeepAliveRoute("http", "127.0.0.1", 80))

    def close():
        closed[0] = True
        operations.append("close")

    def drain(response):
        assert not closed[0]
        operations.append("drain")
        return True

    response = SimpleNamespace(close=close, isclosed=lambda: closed[0], will_close=False,
                               _my_agent_keepalive_lease=transport._Lease(target, connection))
    monkeypatch.setattr(gateway, "drain_completed_response", drain)
    monkeypatch.setattr(gateway, "release_provider_response", lambda response: operations.append("provider"))

    def consume():
        with gateway._gateway_response_scope(response) as (_, guard):
            if ending == "consumer_stopped":
                raise GeneratorExit
            if ending == "error":
                raise ValueError("consumer failed")
            if ending == "aborted":
                guard.abort()

    if ending in {"error", "consumer_stopped"}:
        with pytest.raises(ValueError if ending == "error" else GeneratorExit):
            consume()
    else:
        consume()
    assert operations == (["drain", "close", "checkin", "provider"] if ending in {"normal", "consumer_stopped"}
                          else ["close", "connection_close", "provider"])


def test_provider_slot_is_released_even_when_pool_release_fails(monkeypatch):
    released = []

    def fail(*args, **kwargs):
        raise RuntimeError("pool release failed")

    monkeypatch.setattr(gateway, "release_pooled_response", fail)
    monkeypatch.setattr(gateway, "release_provider_response", released.append)
    response = object()
    with pytest.raises(RuntimeError, match="pool release failed"):
        gateway._release_response_resources(response, False, False)
    assert released == [response]


@pytest.mark.parametrize("path,code", [("/error", 400), ("/redirect", 307)])
@pytest.mark.parametrize("consume", [True, False])
def test_error_body_remains_readable_without_retaining_a_connection_socket(path, code, consume, monkeypatch):
    opened = []
    original = transport._new_connection

    def capture(route, options):
        connection = original(route, options)
        receive = connection.getresponse

        def getresponse():
            response = receive()
            opened.append((connection, response.fp.raw._sock))
            return response

        connection.getresponse = getresponse
        return connection

    monkeypatch.setattr(transport, "_new_connection", capture)
    with _serve() as state:
        state.redirect_url = state.url + "/json"
        with pytest.raises(urllib.error.HTTPError) as caught:
            gateway._open_gateway_request(_request(state, transport.main_connection_pool(4), path=path))
        error = caught.value
        assert error.code == code and len(opened) == 1
        connection, sock = opened[0]
        assert connection.sock is None
        try:
            if consume:
                body = error.read()
                assert json.loads(body)["ok"] is True if code == 400 else body == b""
            else:
                error.close()
            assert sock.fileno() == -1
            assert len(state.requests) == 1
        finally:
            error.close()
