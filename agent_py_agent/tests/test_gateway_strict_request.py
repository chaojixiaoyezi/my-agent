"""严格非流式模型请求的期限、响应上限及零重试合同。"""

from __future__ import annotations

import errno
import hashlib
import http.client
import json
import socket
import threading
import time
import urllib.error
from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.backends import gateway_helpers as gateway
from agent_py_agent.agent.backends import gateway_request_limits as request_limits
from agent_py_agent.agent.backends.errors import (
    ProviderRequestRejectedError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderTransientError,
    ProviderUsageLimitError,
)
from agent_py_agent.agent.backends.provider_send_gate import (
    ProviderSendAttempt,
    ProviderSendRefused,
)


def _request(**overrides):
    values = dict(
        api_base="http://127.0.0.1:1", api_key="test", path="/decide",
        payload={}, headers={}, timeout=10, deadline=time.monotonic() + 2,
        max_retries=0, max_response_bytes=4096, allow_redirects=False,
    )
    values.update(overrides)
    return gateway.GatewayRequest(**values)


@pytest.mark.parametrize("field,value", [
    ("deadline", True), ("deadline", float("nan")), ("deadline", float("inf")),
    pytest.param("deadline", 10 ** 1000, id="deadline-overflow"),
    ("max_retries", True), ("max_retries", -1), ("max_retries", 1.5),
    ("max_response_bytes", True), ("max_response_bytes", 0),
])
def test_invalid_limits_rejected_before_io(field, value):
    with pytest.raises(ValueError):
        _request(**{field: value})


def test_expired_request_never_opens_connection(monkeypatch):
    open_call = Mock()
    monkeypatch.setattr(gateway, "_gateway_urlopen", open_call)
    with pytest.raises(ProviderTimeoutError) as caught:
        gateway.post_json(_request(deadline=time.monotonic() - 1))
    assert caught.value.stage == "wall_clock"
    open_call.assert_not_called()


def test_remaining_subsecond_deadline_is_not_raised_to_one_second():
    request = _request(deadline=time.monotonic() + 0.08)
    assert 0 < gateway._request_initial_read_timeout(request) <= 0.08
    assert 0 < gateway._bounded_connect_timeout(request) <= 0.08


@pytest.mark.parametrize("status", [400, 401, 403, 429, 503])
def test_zero_retries_never_reads_error_body(status, monkeypatch):
    body = Mock()
    body.read.side_effect = AssertionError("错误正文不能阻塞零重试请求")
    error = urllib.error.HTTPError("http://provider.invalid", status, "error", {}, body)
    opener = Mock(side_effect=error)
    monkeypatch.setattr(gateway, "_gateway_urlopen", opener)
    monkeypatch.setattr(gateway, "_provider_retry_wait", Mock(side_effect=AssertionError("不可退避")))
    expected = ProviderRequestRejectedError if status < 429 else ProviderTransientError
    with pytest.raises(expected) as caught:
        gateway.post_json(_request())
    if status == 429:
        assert type(caught.value) is ProviderUsageLimitError
    assert opener.call_count == 1
    body.read.assert_not_called()
    body.close.assert_called_once()


def test_zero_retries_keeps_http_status_when_close_fails(monkeypatch):
    body = Mock()
    body.close.side_effect = OSError("连接清理噪声")
    error = urllib.error.HTTPError("http://provider.invalid", 401, "error", {}, body)
    monkeypatch.setattr(gateway, "_gateway_urlopen", Mock(side_effect=error))
    with pytest.raises(ProviderRequestRejectedError) as caught:
        gateway.post_json(_request())
    assert caught.value.status_code == 401
    body.read.assert_not_called()


def test_zero_retries_does_not_repeat_network_failure(monkeypatch):
    opener = Mock(side_effect=urllib.error.URLError(ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")))
    monkeypatch.setattr(gateway, "_gateway_urlopen", opener)
    with pytest.raises(ProviderTransientError):
        gateway.post_json(_request())
    assert opener.call_count == 1


def test_response_byte_limit_is_exact_and_closes_response(monkeypatch):
    response = BytesIO(b'{"ok":true}')
    monkeypatch.setattr(gateway, "_gateway_urlopen", lambda *_: response)
    assert gateway.post_json(_request(max_response_bytes=11)) == {"ok": True}
    assert response.closed
    oversized = BytesIO(b'{"ok":true} ')
    monkeypatch.setattr(gateway, "_gateway_urlopen", lambda *_: oversized)
    with pytest.raises(ProviderResponseError) as caught:
        gateway.post_json(_request(max_response_bytes=11))
    assert caught.value.error_code == "PROVIDER_RESPONSE_TOO_LARGE"
    assert oversized.closed


@pytest.mark.parametrize("body", [
    b'{"score":NaN}', b'{"score":Infinity}', b'{"score":1e999}',
    b'{"selected":"a","selected":"b"}', b'{"text":"\xff"}',
    b'{"text":"\\ud800"}', pytest.param(b'[' * 1500 + b']' * 1500, id="deep-json"),
])
def test_strict_response_reuses_json_contract(body, monkeypatch):
    monkeypatch.setattr(gateway, "_gateway_urlopen", lambda *_: BytesIO(body))
    with pytest.raises(ProviderResponseError):
        gateway.post_json(_request())


@pytest.mark.parametrize("body", [b'{}', b'{invalid}'])
def test_complete_body_cannot_be_applied_after_json_decode_deadline(body, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(gateway.time, "monotonic", lambda: clock[0])
    request = _request(deadline=101.0)
    monkeypatch.setattr(gateway, "_gateway_urlopen", lambda *_: BytesIO(body))
    original = gateway.json.loads

    def decode_after_expiration(text, **kwargs):
        try:
            return original(text, **kwargs)
        finally:
            clock[0] = 102.0

    monkeypatch.setattr(gateway.json, "loads", decode_after_expiration)
    with pytest.raises(ProviderTimeoutError):
        gateway.post_json(request)


@pytest.mark.parametrize("body", [
    pytest.param(b'[' * 65 + b'0' + b']' * 65, id="array-65"),
    pytest.param(b'{"a":' * 65 + b'0' + b'}' * 65, id="object-65"),
    pytest.param(b'[' * 524287 + b'0' + b']' * 524287, id="one-mib-deep-json"),
])
def test_excessive_nesting_is_rejected_before_recursive_parser(body, monkeypatch):
    parser = Mock(side_effect=AssertionError("过深输入不能进入递归解析器"))
    monkeypatch.setattr(request_limits, "load_strict_json", parser)
    monkeypatch.setattr(gateway, "_gateway_urlopen", lambda *_: BytesIO(body))
    with pytest.raises(ProviderResponseError, match="64 层"):
        gateway.post_json(_request(max_response_bytes=1024 * 1024))
    parser.assert_not_called()


def test_depth_scan_allows_64_levels_and_ignores_quoted_brackets(monkeypatch):
    payload = {"text": '[{]}"\\' * 100}
    for _ in range(63):
        payload = [payload]
    body = json.dumps(payload).encode()
    monkeypatch.setattr(gateway, "_gateway_urlopen", lambda *_: BytesIO(body))
    assert gateway.post_json(_request()) == payload


@pytest.mark.parametrize("http_error", [False, True])
def test_response_arriving_after_open_deadline_is_closed(http_error, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(gateway.time, "monotonic", lambda: clock[0])
    body = BytesIO(b'{}')

    def open_after_expiration(*_args, **_kwargs):
        clock[0] = 102.0
        if http_error:
            raise urllib.error.HTTPError("http://provider.invalid", 503, "error", {}, body)
        return body

    opener = Mock()
    opener.open.side_effect = open_after_expiration
    monkeypatch.setattr(gateway.urllib.request, "build_opener", lambda *_: opener)
    with pytest.raises(ProviderTimeoutError):
        gateway.post_json(_request(deadline=101.0))
    assert body.closed


# LLM: 本地服务只模拟真实 socket 边界；所有连接、线程和停止事件在退出时回收，无外部网络。
# 函数用途: 发送慢响应头、持续正文或重定向，检查客户端按本请求合同关闭连接。
@contextmanager
def _slow_server(mode):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)
    stopped, peer_closed = threading.Event(), threading.Event()
    errors = []

    def serve():
        try:
            with listener.accept()[0] as connection:
                connection.settimeout(2)
                data = b""
                while b"\r\n\r\n" not in data:
                    data += connection.recv(4096)
                if mode == "error":
                    connection.sendall(b"HTTP/1.1 503 Busy\r\nContent-Length: 10000\r\n\r\n")
                elif mode == "redirect":
                    connection.sendall(b"HTTP/1.1 302 Found\r\nLocation: /redirected\r\nContent-Length: 10000\r\n\r\n")
                elif mode == "body":
                    connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n")
                else:
                    connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                while not stopped.wait(0.01):
                    connection.sendall(b" ")
        except (BrokenPipeError, ConnectionResetError):
            peer_closed.set()
        except Exception as exc:
            errors.append(exc)
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}", peer_closed
    finally:
        stopped.set()
        thread.join(3)
        assert not thread.is_alive()
        assert not errors


@pytest.mark.parametrize("mode", ["headers", "body", "error"])
def test_real_trickle_cannot_extend_absolute_deadline(mode):
    with _slow_server(mode) as (url, closed):
        request = _request(api_base=url, deadline=time.monotonic() + 0.15, max_retries=1 if mode == "error" else 0)
        started = time.monotonic()
        with pytest.raises(ProviderTimeoutError) as caught:
            gateway.post_json(request)
        elapsed = time.monotonic() - started
        assert caught.value.stage == "wall_clock"
        assert elapsed < 0.8
        assert closed.wait(1)


def test_real_error_headers_return_without_waiting_for_body():
    with _slow_server("error") as (url, closed):
        started = time.monotonic()
        with pytest.raises(ProviderTransientError):
            gateway.post_json(_request(api_base=url))
        assert time.monotonic() - started < 0.5
        assert closed.wait(1)


def test_real_strict_request_does_not_follow_redirect():
    with _slow_server("redirect") as (url, closed):
        started = time.monotonic()
        with pytest.raises(ProviderRequestRejectedError) as caught:
            gateway.post_json(_request(api_base=url))
        assert caught.value.status_code == 302
        assert time.monotonic() - started < 0.5
        assert closed.wait(1)


def test_explicit_retry_limit_is_shared_by_unlabeled_and_transient_errors(monkeypatch):
    errors = [
        urllib.error.HTTPError("http://provider.invalid", 400, "error", {}, BytesIO(b'{"object":"error"}')),
        urllib.error.HTTPError("http://provider.invalid", 503, "error", {}, BytesIO(b'{}')),
    ]
    opener = Mock(side_effect=errors)
    monkeypatch.setattr(gateway, "_gateway_urlopen", opener)
    monkeypatch.setattr(gateway, "_provider_retry_wait", lambda *_: None)
    with pytest.raises(ProviderTransientError):
        gateway.post_json(_request(max_retries=1))
    assert opener.call_count == 2


def test_incomplete_body_keeps_existing_error_taxonomy(monkeypatch):
    response = Mock()
    response.status = 200
    response.read1.side_effect = http.client.IncompleteRead(b'{"x"', 2)
    monkeypatch.setattr(gateway, "_gateway_urlopen", lambda *_: response)
    with pytest.raises(ProviderTransientError):
        gateway.post_json(_request())


def test_read1_premature_eof_preserves_incomplete_body_failure(monkeypatch):
    response = Mock(status=200, length=5)
    response.read1.side_effect = [b'{"x":', b'']
    monkeypatch.setattr(gateway, "_gateway_urlopen", lambda *_: response)
    with pytest.raises(ProviderTransientError):
        gateway.post_json(_request())


def test_retry_after_cannot_extend_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(gateway.time, "monotonic", lambda: clock[0])
    error = urllib.error.HTTPError("http://provider.invalid", 503, "error", {"Retry-After": "30"}, BytesIO(b'{}'))
    opener = Mock(side_effect=error)
    monkeypatch.setattr(gateway, "_gateway_urlopen", opener)
    waits = []

    def wait(seconds):
        waits.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(gateway, "_provider_retry_wait", wait)
    with pytest.raises(ProviderTimeoutError):
        gateway.post_json(_request(deadline=100.25, max_retries=1))
    assert waits == [0.25]
    assert opener.call_count == 1


def test_no_strict_fields_preserves_default_socket_window():
    request = replace(_request(), deadline=None, max_retries=None, max_response_bytes=None)
    assert gateway._request_initial_read_timeout(request) == request.timeout


def test_no_strict_fields_preserves_default_json_behavior(monkeypatch):
    payload = ["value"]
    for _ in range(100):
        payload = [payload]
    body = json.dumps(payload).encode()
    monkeypatch.setattr(gateway, "_gateway_urlopen", lambda *_: BytesIO(body))
    request = replace(_request(), deadline=None, max_retries=None, max_response_bytes=None)
    assert gateway.post_json(request) == payload


def test_error_response_guard_shuts_down_its_actual_socket():
    client, peer = socket.socketpair()
    with client, peer:
        peer.settimeout(1)
        peer.sendall(b"HTTP/1.1 503 Busy\r\nContent-Length: 10\r\n\r\n")
        with http.client.HTTPResponse(client) as response:
            response.begin()
            error = urllib.error.HTTPError("http://provider.invalid", 503, "error", {}, response)
            assert request_limits.stdlib_response_socket(error) is client
            gateway._GatewayResponseGuard(error).abort()
            assert peer.recv(1) == b""
            assert response.isclosed()


# LLM: 许可替身只记录传输层交来的最终发送事实并按配置拒绝；不读账本或设置，验证的是传输层硬门位置与信封约束。
# 类用途: 为发送许可合同提供可观察的 admit 实现。
class _Permit:
    def __init__(self, refuse=""):
        self.calls, self.refuse = [], refuse

    def admit(self, attempt):
        self.calls.append(attempt)
        if self.refuse:
            raise ProviderSendRefused(self.refuse)


@pytest.mark.parametrize("overrides", [{"max_retries": None}, {"max_retries": 1}, {"allow_redirects": True},
                                       {"deadline": None}, {"send_permit": object()}])
def test_send_permit_envelope_requires_zero_retry_no_redirect_deadline_and_admit(overrides):
    with pytest.raises(ValueError):
        _request(**{"send_permit": _Permit(), **overrides})


def test_refused_permit_stops_before_telemetry_and_any_connection(monkeypatch):
    opener, events, permit = Mock(), [], _Permit("experiment_revoked")
    monkeypatch.setattr(gateway, "_gateway_urlopen", opener)
    with gateway.provider_attempt_observer(events.append), pytest.raises(ProviderSendRefused) as caught:
        gateway.post_json(_request(payload={"model": "jev", "state": "材料"}, send_permit=permit))
    assert type(caught.value) is ProviderSendRefused and caught.value.code == "experiment_revoked"
    opener.assert_not_called()
    assert events == [] and len(permit.calls) == 1


def test_admitted_permit_sees_final_wire_facts_before_the_single_open(monkeypatch):
    payload = {"model": "jev", "state": {"文本": "材料"}, "questions": {"a": {"type": "noul"}}}
    permit, opened = _Permit(), []

    def open_once(req, _request):
        opened.append(req.data)
        return BytesIO(b'{"ok":true}')

    monkeypatch.setattr(gateway, "_gateway_urlopen", open_once)
    assert gateway.post_json(_request(payload=payload, send_permit=permit)) == {"ok": True}
    body = json.dumps(payload).encode("utf-8")
    assert opened == [body]
    assert permit.calls == [ProviderSendAttempt("POST", "http://127.0.0.1:1/decide", hashlib.sha256(body).hexdigest(), "jev", 0)]


def test_ordinary_request_bytes_are_unchanged_by_the_single_encoder():
    payload = {"model": "m", "messages": [{"role": "user", "content": "中文 😀"}], "n": 1.5, "flag": None}
    expected = json.dumps(payload).encode("utf-8")
    request = replace(_request(payload=payload), deadline=None, max_retries=None, max_response_bytes=None, allow_redirects=True)
    assert gateway._urllib_request(request).data == expected == gateway.gateway_request_body(payload)
    assert request.send_permit is None and "send_permit" not in repr(request)
