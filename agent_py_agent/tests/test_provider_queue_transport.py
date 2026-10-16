from __future__ import annotations

import gc
import io
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import gateway_helpers as http
from agent_py_agent.agent.backends import request_scope as scope
from agent_py_agent.agent.backends.errors import (
    ProviderConnectionError,
    ProviderQueueTimeoutError,
    ProviderTimeoutError,
    ProviderTransientError,
)
from agent_py_agent.tests.test_provider_queue import wait_until


def request(limit=1, **options):
    req = http.GatewayRequest(api_base="http://transport.invalid", path="/v1/chat", api_key="fake",
                              payload={}, headers={}, timeout=2, **options)
    object.__setattr__(req, "provider_max_concurrent_requests", limit)
    return req


def opener(monkeypatch, operation):
    monkeypatch.setattr(http.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=operation))


def test_physical_json_requests_are_limited_through_body_read(monkeypatch):
    lock = threading.Lock()
    active = peak = 0

    class Body(io.BytesIO):
        def read(self, *args):
            nonlocal active
            time.sleep(0.015)
            result = super().read(*args)
            with lock:
                active -= 1
            return result

    def send(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        return Body(b'{"ok":true}')

    opener(monkeypatch, send)
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda i: http.post_json(request(2)), range(10)))
    assert results == [{"ok": True}] * 10
    assert peak == 2
    assert scope.provider_endpoint_counts(request()) == (0, 0)


@pytest.mark.parametrize("discard", ["close", "gc"])
def test_partial_stream_discard_returns_slot(monkeypatch, discard):
    opener(monkeypatch, lambda *a, **k: io.BytesIO(b"data: first\n\ndata: second\n\n"))
    req = request()
    stream = http.post_stream_iter(req)
    assert next(stream) == "first"
    assert scope.provider_endpoint_counts(req) == (1, 0)
    if discard == "close":
        stream.close()
    else:
        del stream
        gc.collect()
    wait_until(lambda: scope.provider_endpoint_counts(req) == (0, 0))


@pytest.mark.parametrize("failure", [ValueError("parser"), TimeoutError("timeout"), InterruptedError("stop")])
def test_body_failures_return_slot(monkeypatch, failure):
    class Body(io.BytesIO):
        def read(self, *args):
            assert scope.provider_endpoint_counts(req) == (1, 0)
            raise failure

    req = request()
    opener(monkeypatch, lambda *a, **k: Body())
    with pytest.raises(ProviderTimeoutError if isinstance(failure, TimeoutError) else type(failure)):
        http.post_json(req)
    assert scope.provider_endpoint_counts(req) == (0, 0)


def test_open_failure_returns_slot(monkeypatch):
    def send(*a, **k):
        assert scope.provider_endpoint_counts(req) == (1, 0)
        raise ValueError("open failed")

    req = request(max_retries=0)
    opener(monkeypatch, send)
    with pytest.raises(ValueError):
        http.post_json(req)
    assert scope.provider_endpoint_counts(req) == (0, 0)


def test_429_releases_before_backoff_and_requeues_each_attempt(monkeypatch):
    req = request()
    attempts = []
    waits = []

    def send(*a, **k):
        assert scope.provider_endpoint_counts(req) == (1, 0)
        attempts.append(1)
        raise urllib.error.HTTPError(req.url, 429, "busy", {}, io.BytesIO(b'{"code":2062}'))

    def backoff(*a):
        waits.append(scope.provider_endpoint_counts(req))
        assert waits[-1] == (0, 0)
        # 模拟工具/子调用：退避阶段必须允许同一端点另一物理请求获得名额。
        scope.acquire_provider_request(req).release()

    opener(monkeypatch, send)
    monkeypatch.setattr(http, "_provider_retry_wait", backoff)
    with pytest.raises(ProviderTransientError):
        http.post_json(req)
    assert len(attempts) == 4 and waits == [(0, 0)] * 3
    assert scope.provider_endpoint_counts(req) == (0, 0)


def test_queue_does_not_spend_first_event_or_idle_timeout(monkeypatch):
    req = request(first_event_timeout=1)
    held = scope.acquire_provider_request(req)
    opener(monkeypatch, lambda *a, **k: io.BytesIO(b"data: first\n\n"))
    events = []

    def run():
        with http.provider_attempt_observer(events.append):
            return list(http.post_stream_iter(req))

    with ThreadPoolExecutor(max_workers=1) as pool:
        job = pool.submit(run)
        try:
            wait_until(lambda: scope.provider_endpoint_counts(req)[1] == 1)
            time.sleep(1.05)
        finally:
            held.release()
        assert job.result(timeout=2) == ["first"]


def test_configuration_reaches_backend_envelope():
    from agent_py_agent.agent.backends.factory import get_backend
    from agent_py_agent.agent.settings.config import AgentConfig

    config = AgentConfig(model_backend="openai_compatible", api_base="http://transport.invalid", api_key="fake", model_name="fake")
    config.provider_max_concurrent_requests = 2
    backend = get_backend(config.model_backend, config)
    envelope = backend._gateway_request("/chat/completions", {}, {})
    assert getattr(envelope, "provider_max_concurrent_requests", None) == 2


def test_websocket_slot_lives_until_close_and_releases_on_failed_handshake(monkeypatch):
    from agent_py_agent.agent.backends import responses_websocket as ws

    req = request(max_retries=0)
    connection = SimpleNamespace(close=lambda: None, close_socket=lambda: None, send=lambda x: None)
    monkeypatch.setattr(ws, "_connect", lambda request: connection)
    monkeypatch.setattr(ws, "_events", lambda *a: iter(['{"type":"response.completed"}']))
    stream = ws.iter_responses_websocket(req)
    assert next(stream) == '{"type":"response.completed"}'
    assert scope.provider_endpoint_counts(req) == (1, 0)
    stream.close()
    assert scope.provider_endpoint_counts(req) == (0, 0)

    def fail(request):
        assert scope.provider_endpoint_counts(req) == (1, 0)
        raise TimeoutError("handshake")

    monkeypatch.setattr(ws, "_connect", fail)
    with pytest.raises(ProviderTimeoutError):
        list(ws.iter_responses_websocket(req))
    assert scope.provider_endpoint_counts(req) == (0, 0)


def test_queue_respects_absolute_budget_across_model_turn_retries(monkeypatch):
    from agent_py_agent.agent.agent_core import provider_transient_auto_resume as retry

    req = request()
    held = scope.acquire_provider_request(req)
    clock = [0.0]
    sleeps = []
    sends = []
    tickets = scope._QueueTicket

    class ClockEvent:
        def set(self):
            pass

        def clear(self):
            pass

        def wait(self, seconds):
            clock[0] += seconds

    def wait(seconds):
        sleeps.append(seconds)
        assert scope.provider_endpoint_counts(req) == (1, 0)
        clock[0] += seconds

    monkeypatch.setattr(scope, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(retry, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(scope, "_QueueTicket", lambda owner: tickets(owner, wake=ClockEvent()))
    monkeypatch.setattr(scope, "PROVIDER_QUEUE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(retry, "provider_transient_retry_delays", lambda _: (0.005,) * 3)
    monkeypatch.setattr(retry, "apply_retry_jitter", lambda delay: delay)
    monkeypatch.setattr(retry, "wait_interruptibly", wait)
    opener(monkeypatch, lambda *a, **k: sends.append(1))
    with ExitStack() as stack:
        stack.callback(held.release)
        with scope.provider_request_budget(0.065):
            original_deadline = scope._DEADLINE.get()
            _assert_retry_queue_deadline(retry, req)
            assert scope._DEADLINE.get() == original_deadline
        assert len(sleeps) == 3
        assert sends == []
        # 三次排队已花光同一总预算，第四次不得获得新的两分钟窗口。
        assert 0.065 <= clock[0] <= 0.071
        assert scope.provider_endpoint_counts(req) == (1, 0)
    assert scope._DEADLINE.get() is None
    assert scope.provider_endpoint_counts(req) == (0, 0)


def _assert_retry_queue_deadline(retry, req):
    with pytest.raises(ProviderQueueTimeoutError):
        retry.run_with_provider_transient_auto_resume(lambda: http.post_json(req))


def test_explicit_envelope_deadline_queue_expiry_never_sends(monkeypatch):
    req = request(deadline=time.monotonic() + 0.03)
    held = scope.acquire_provider_request(request())
    sends = []
    opener(monkeypatch, lambda *a, **k: sends.append(1))
    try:
        with pytest.raises(ProviderQueueTimeoutError):
            http.post_json(req)
        assert sends == []
        assert scope.provider_endpoint_counts(req) == (1, 0)
    finally:
        held.release()


def test_stream_close_failure_releases_once_without_gc(monkeypatch):
    req = request()
    bodies = []

    class Body(io.BytesIO):
        def close(self):
            super().close()
            raise ValueError("fixture close failure")

    body = Body(b"data: first\n\ndata: second\n\n")
    bodies.append(body)
    opener(monkeypatch, lambda *a, **k: body)
    stream = http.post_stream_iter(req)
    assert next(stream) == "first"
    assert scope.provider_endpoint_counts(req) == (1, 0)
    with pytest.raises(ProviderConnectionError) as raised:
        stream.close()
    assert isinstance(raised.value.__cause__, ValueError)
    assert scope.provider_endpoint_counts(req) == (0, 0)
    stream.close()
    assert scope.provider_endpoint_counts(req) == (0, 0)
    assert bodies[0].closed


@pytest.mark.parametrize("failure", [False, True])
def test_zero_matches_old_physical_path_requests_exceptions_events_and_no_lock(monkeypatch, failure):
    class ForbiddenLock:
        def __enter__(self):
            pytest.fail("zero acquired limiter lock")

        def __exit__(self, *args):
            pass

    req = request(0, max_retries=0)
    sends = []
    events = []

    def send(*a, **k):
        sends.append(1)
        if failure:
            raise ValueError("fixture rejection")
        return io.BytesIO(b'{"ok":true}')

    def execute():
        try:
            with http.provider_attempt_observer(events.append):
                return http.post_json(req)
        except ValueError as exc:
            return type(exc), str(exc)

    opener(monkeypatch, send)
    monkeypatch.setattr(scope, "_LOCK", ForbiddenLock())
    actual = execute()
    actual_events = [{k: v for k, v in row.items() if k != "attempt_id"} for row in events]
    assert all(row.get("status") != "queued" for row in events)
    events.clear()
    # _gateway_urlopen_unlimited 是保留的旧物理发送路径；比较整个 post_json 的真实入口，而非只比较结果成功。
    monkeypatch.setattr(http, "_gateway_urlopen", http._gateway_urlopen_unlimited)
    old = execute()
    assert old == actual
    assert len(sends) == 2
    assert [{k: v for k, v in row.items() if k != "attempt_id"} for row in events] == actual_events


@pytest.mark.parametrize("limit", [0, 1])
@pytest.mark.parametrize("bound", ["deadline", "max_response_bytes"])
def test_cached_http_error_strict_cleanup_respects_zero_switch(monkeypatch, limit, bound):
    options = {bound: time.monotonic() + 2 if bound == "deadline" else 2048}
    req = request(limit, max_retries=1, **options)
    body = io.BytesIO(b"{}")
    exc = urllib.error.HTTPError(req.url, 503, "busy", {}, body)
    setattr(exc, http._HTTP_ERROR_DETAIL_ATTR, "cached-body")
    if limit:
        body.close()  # 开启时缓存来自已经消费/关闭的物理错误响应，不能再读。
    reads = []
    monkeypatch.setattr(http, "read_response_body", lambda response, envelope: reads.append(envelope) or b"{}")
    monkeypatch.setattr(http, "_should_retry_http_error", lambda *a: True)
    assert http._request_http_retry(exc, req, 0, 1)
    assert reads == ([req] if limit == 0 else [])
    assert body.closed  # 关闭时保留旧严格清理合同，开启时不重复清理已关闭响应。


@pytest.mark.parametrize("protocol", ["openai_compatible", "anthropic_compatible"])
def test_model_completion_closes_retained_real_stream_before_tool_or_child_wait(monkeypatch, protocol):
    from agent_py_agent.agent.backends.factory import get_backend
    from agent_py_agent.agent.settings import AgentConfig

    config = AgentConfig(model_backend=protocol, api_base="http://transport.invalid", api_key="fake", model_name="fake")
    config.provider_max_concurrent_requests = 1
    backend = get_backend(protocol, config)
    data = (b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            if protocol == "openai_compatible" else
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n'
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
            b'data: {"type":"message_stop"}\n\n')
    body = io.BytesIO(data + b'data: tail-not-consumed\n\n')
    opener(monkeypatch, lambda *a, **k: body)
    retained = []
    original = backend.request_stream_iter

    def lines(*args, **kwargs):
        stream = original(*args, **kwargs)
        retained.append(stream)
        return stream

    monkeypatch.setattr(backend, "request_stream_iter", lines)
    try:
        response = backend.generate("hello", on_chunk=lambda _: None)
        assert response.text == "ok"
        assert retained  # 外部仍持有生成器：不能靠引用计数或 GC 归还。
        assert body.closed
        assert scope.provider_endpoint_counts(request()) == (0, 0)
        # 与父回合 _FOREGROUND 区分：即使仍在整轮内，工具/子等待期间也能马上发送下一请求。
        with scope.foreground_model_scope(backend):
            assert scope.foreground_model_active(backend)
            scope.acquire_provider_request(request()).release()
    finally:
        for stream in retained:
            stream.close()


@pytest.mark.parametrize("transport", ["http", "websocket"])
def test_queue_timeout_never_starts_a_physical_attempt_or_transport_timing(monkeypatch, transport):
    from agent_py_agent.agent.backends import responses_websocket as ws

    req = request(max_retries=0)
    held = scope.acquire_provider_request(req)
    sends, events, timings = [], [], []
    monkeypatch.setattr(scope, "PROVIDER_QUEUE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(scope, "PROVIDER_QUEUE_NOTICE_SECONDS", 0.005)
    monkeypatch.setattr(http, "_attempt_timing", lambda *args: timings.append(args))
    opener(monkeypatch, lambda *args, **kwargs: sends.append("http"))
    monkeypatch.setattr(ws, "_connect", lambda *args: sends.append("websocket"))
    try:
        with http.provider_attempt_observer(events.append, transport_timing=True):
            with pytest.raises(ProviderQueueTimeoutError):
                if transport == "http":
                    http.post_json(req)
                else:
                    list(ws.iter_responses_websocket(req))
        assert events and all(event["status"] == "queued" for event in events)
        assert [event["provider_queue"]["phase"] for event in events] == ["waiting", "cancelled"]
        assert not sends and not timings
        assert scope.provider_endpoint_counts(req) == (1, 0)
    finally:
        held.release()


def test_queue_admission_precedes_attempt_timing_and_started_event(monkeypatch):
    req = request(max_retries=0)
    held = scope.acquire_provider_request(req)
    events, timing_counts = [], []
    monkeypatch.setattr(scope, "PROVIDER_QUEUE_NOTICE_SECONDS", 0.005)
    original_timing = http._attempt_timing

    def timing(*args):
        timing_counts.append(scope.provider_endpoint_counts(req))
        return original_timing(*args)

    def execute():
        with http.provider_attempt_observer(events.append, transport_timing=True):
            return http.post_json(req)

    monkeypatch.setattr(http, "_attempt_timing", timing)
    opener(monkeypatch, lambda *args, **kwargs: io.BytesIO(b'{"ok":true}'))
    with ThreadPoolExecutor(max_workers=1) as pool:
        job = pool.submit(execute)
        try:
            wait_until(lambda: bool(events))
            assert [event["status"] for event in events] == ["queued"]
            assert not timing_counts
        finally:
            held.release()
        assert job.result(timeout=2) == {"ok": True}
    assert timing_counts == [(1, 0)]
    assert [(event["status"], event.get("provider_queue", {}).get("phase")) for event in events] == [
        ("queued", "waiting"), ("queued", "admitted"), ("started", None), ("response_opened", None)]
    assert scope.provider_endpoint_counts(req) == (0, 0)


def test_local_preparation_failure_after_admission_returns_slot(monkeypatch):
    req = request(max_retries=0)

    def fail(_request):
        assert scope.provider_endpoint_counts(req) == (1, 0)
        raise ValueError("fixture request preparation failed")

    monkeypatch.setattr(http, "_urllib_request", fail)
    with pytest.raises(ValueError, match="fixture request preparation"):
        http.post_json(req)
    assert scope.provider_endpoint_counts(req) == (0, 0)


@pytest.mark.parametrize("http_error", [False, True])
def test_late_open_response_is_closed_and_slot_released(monkeypatch, http_error):
    req = request(max_retries=0, deadline=time.monotonic() + 0.025)
    body = io.BytesIO(b"{}")

    def send(*args, **kwargs):
        time.sleep(0.04)
        if http_error:
            raise urllib.error.HTTPError(req.url, 503, "fixture late error", {}, body)
        return body

    opener(monkeypatch, send)
    with pytest.raises(ProviderTimeoutError):
        http.post_json(req)
    assert body.closed
    assert scope.provider_endpoint_counts(req) == (0, 0)


@pytest.mark.parametrize("protocol", ["openai_compatible", "anthropic_compatible"])
def test_disabled_limiter_preserves_retained_stream_lifecycle(monkeypatch, protocol):
    from agent_py_agent.agent.backends.factory import get_backend
    from agent_py_agent.agent.settings import AgentConfig

    config = AgentConfig(model_backend=protocol, api_base="http://transport.invalid", api_key="fake", model_name="fake")
    backend = get_backend(protocol, config)
    data = (b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            if protocol == "openai_compatible" else
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n'
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
            b'data: {"type":"message_stop"}\n\n')
    body = io.BytesIO(data + b'data: retained-tail\n\n')
    opener(monkeypatch, lambda *args, **kwargs: body)
    retained = []
    original = backend.request_stream_iter

    def lines(*args, **kwargs):
        stream = original(*args, **kwargs)
        retained.append(stream)
        return stream

    monkeypatch.setattr(backend, "request_stream_iter", lines)
    try:
        assert backend.generate("hello", on_chunk=lambda _: None).text == "ok"
        assert retained and not body.closed
        assert scope.provider_endpoint_counts(request()) == (0, 0)
    finally:
        for stream in retained:
            stream.close()


def test_websocket_close_failure_releases_retained_slot_without_gc(monkeypatch):
    from agent_py_agent.agent.backends import responses_websocket as ws

    req = request(max_retries=0)

    def close():
        raise RuntimeError("fixture websocket close failure")

    connection = SimpleNamespace(close=close, close_socket=close, send=lambda _: None)
    monkeypatch.setattr(ws, "_connect", lambda _: connection)
    monkeypatch.setattr(ws, "_events", lambda *args: iter(['{"type":"response.completed"}']))
    stream = ws.iter_responses_websocket(req)
    assert next(stream) == '{"type":"response.completed"}'
    assert scope.provider_endpoint_counts(req) == (1, 0)
    with pytest.raises(RuntimeError, match="fixture websocket close"):
        stream.close()
    assert scope.provider_endpoint_counts(req) == (0, 0)
