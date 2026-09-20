from __future__ import annotations

"""NET-01 回归：系统级拒绝连接和 typed DNS 失败可退避，非 typed 配置错误仍快速报错。

2026-08-15 C3 真机：api_base 指向死端口时 CLI 打印完整 Python traceback（RC=1）。
2026-08-18 长任务：远端 MiniMax 在 127 个工具轮后短暂拒绝连接，旧分类没有进入任何退避。
"""

import errno
import http.client
import json
import socket
import urllib.error
from io import BufferedReader, BytesIO
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import gateway_helpers
from agent_py_agent.agent.backends.errors import (
    ProviderConnectionError,
    ProviderRecoverableError,
    ProviderTransientError,
)
from agent_py_agent.agent.backends.gateway_helpers import (
    GatewayRequest,
    _runtime_network_error,
)


def _request() -> GatewayRequest:
    return GatewayRequest(
        api_base="http://127.0.0.1:9",
        api_key="test",
        path="/v1/messages",
        payload={},
        headers={},
        timeout=10,
    )


# LLM: typed ECONNREFUSED 是远端端点的瞬时供应事实，必须进入现有 ProviderTransientError 双层退避。
# 函数用途: 验证 urllib 包装后的系统级拒绝连接可重试，避免长任务因一次供应抖动直接终止。
def test_connection_refused_errno_classified_as_provider_transient_error():
    exc = urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"))
    err = _runtime_network_error(exc, _request())
    assert isinstance(err, ProviderTransientError)
    assert isinstance(err, ProviderRecoverableError)
    assert "拒绝连接" in str(err)


# LLM: 错误文案没有机器权威；缺少 errno/type 的同名字符串不得扩大重试面。
# 函数用途: 验证仅写着 connection refused 的普通字符串仍按未知连接配置错误处理。
def test_connection_refused_text_without_errno_is_not_machine_retry_fact():
    err = _runtime_network_error(urllib.error.URLError("Connection refused"), _request())
    assert isinstance(err, ProviderConnectionError)
    assert not isinstance(err, ProviderRecoverableError)


# LLM: typed gaierror 可能是 resolver 瞬断；与 会话运行时 ConnectionFailed 一样进入有界恢复，不能一跳杀死长任务。
# 函数用途: 验证 DNS 解析错误保留可恢复类型，物理重试次数由 gateway helper 的独立回归约束。
def test_dns_failure_is_provider_transient_error():
    exc = urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, "Name or service not known"))
    err = _runtime_network_error(exc, _request())
    assert isinstance(err, ProviderTransientError)
    assert isinstance(err, ProviderRecoverableError)


# LLM: 瞬时网络错误（连接重置）仍归 ProviderTransientError，重试语义不变。
# 函数用途: 验证分类边界：可重试网络错误不被误归连接错误。
def test_transient_network_error_stays_transient():
    exc = urllib.error.URLError("Connection reset by peer")
    err = _runtime_network_error(exc, _request())
    from agent_py_agent.agent.backends.errors import ProviderTransientError

    assert isinstance(err, ProviderTransientError)


# LLM: 超时仍归 ProviderTimeoutError。
# 函数用途: 验证超时分类不因本次改动回退。
def test_timeout_stays_timeout():
    err = _runtime_network_error(TimeoutError("timed out"), _request())
    from agent_py_agent.agent.backends.errors import ProviderTimeoutError

    assert isinstance(err, ProviderTimeoutError)


@pytest.mark.parametrize("wrapped", [False, True])
def test_incomplete_http_body_is_typed_transient_without_string_matching(wrapped):
    error = http.client.IncompleteRead(b"uncommitted model bytes", 100)
    exc = urllib.error.URLError(error) if wrapped else error
    assert isinstance(_runtime_network_error(exc, _request()), ProviderTransientError)
    # 普通文案不能借用异常类名获得重试权。
    assert isinstance(
        _runtime_network_error(urllib.error.URLError("IncompleteRead(0 bytes read)"), _request()),
        ProviderConnectionError,
    )


# LLM: 使用真实 stdlib HTTPResponse 的 chunked 解析，不把真实断流只替换成手工异常；响应不会访问网络或落盘。
# 函数用途: 构造一条完整 SSE data 后下个 chunk 缺失内容的响应，复现长任务中途 IncompleteRead。
def _truncated_chunked_response(data: bytes = b'data: {"delta":"partial"}\n\n'):
    wire = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        + f"{len(data):x}\r\n".encode()
        + data + b"\r\n10\r\n"
    )
    response = http.client.HTTPResponse(
        SimpleNamespace(makefile=lambda *_args: BufferedReader(BytesIO(wire)))
    )
    response.begin()
    return response


@pytest.mark.parametrize("entry", ["post_json", "get_json", "post_stream", "post_stream_iter"])
def test_truncated_http_response_normalized_at_every_transport_entry(monkeypatch, entry):
    response = _truncated_chunked_response()
    monkeypatch.setattr(gateway_helpers, "_gateway_urlopen", lambda *_args: response)
    with pytest.raises(ProviderTransientError) as raised:
        result = getattr(gateway_helpers, entry)(_request())
        if entry == "post_stream_iter":
            assert next(result) == '{"delta":"partial"}'
            next(result)
    assert isinstance(raised.value.__cause__, http.client.IncompleteRead)
    assert response.closed


def test_truncated_body_uses_existing_model_retry_not_a_second_body_retry(monkeypatch):
    from agent_py_agent.agent.agent_core import provider_transient_auto_resume as retry

    responses = iter([_truncated_chunked_response(), BytesIO(b"data: [DONE]\n\n")])
    opened = []
    waits = []

    def open_response(*_args):
        opened.append(True)
        return next(responses)

    monkeypatch.setattr(gateway_helpers, "_gateway_urlopen", open_response)
    monkeypatch.setattr(retry, "provider_transient_retry_delays", lambda _policy: (0.1,))
    monkeypatch.setattr(retry, "apply_retry_jitter", lambda value: value)
    monkeypatch.setattr(retry, "wait_interruptibly", waits.append)
    result = retry.run_with_provider_transient_auto_resume(
        lambda: gateway_helpers.post_stream(_request())
    )
    assert result == ["[DONE]"]
    assert waits == [0.1]
    assert len(opened) == 2


def test_incomplete_read_during_user_abort_remains_an_interrupt(monkeypatch):
    stopped = False

    def interrupted_stream(_request):
        nonlocal stopped
        yield '{"delta":"partial"}'
        stopped = True
        raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(gateway_helpers, "_stream_with_watchdog", interrupted_stream)
    monkeypatch.setattr(gateway_helpers, "_provider_is_interrupted", lambda: stopped)
    stream = gateway_helpers.post_stream_iter(_request())
    next(stream)
    with pytest.raises(InterruptedError):
        next(stream)


def test_incomplete_read_at_response_open_uses_bounded_http_retry(monkeypatch):
    attempts = []
    waits = []
    events = []

    def failed_open(*_args):
        attempts.append(True)
        raise http.client.IncompleteRead(b"")

    monkeypatch.setattr(gateway_helpers, "_gateway_urlopen", failed_open)
    monkeypatch.setattr(gateway_helpers, "_provider_retry_wait", waits.append)
    with gateway_helpers.provider_attempt_observer(events.append):
        with pytest.raises(ProviderTransientError):
            gateway_helpers.post_json(_request())
    assert len(attempts) == 4
    assert waits == [2.0, 5.0, 15.0]
    assert len([event for event in events if event["status"] == "failed"]) == 4


def test_incomplete_body_read_after_watchdog_close_keeps_timeout_phase(monkeypatch):
    from agent_py_agent.agent.backends.errors import ProviderTimeoutError

    guard = SimpleNamespace(
        timed_out=True,
        timeout_stage="stream_idle",
        start=lambda: None,
        cancel=lambda: None,
        touch=lambda: None,
    )
    monkeypatch.setattr(gateway_helpers, "_StreamIdleWatchdog", lambda *_args, **_kw: guard)
    monkeypatch.setattr(gateway_helpers, "_gateway_urlopen", lambda *_args: _truncated_chunked_response())
    with pytest.raises(ProviderTimeoutError) as raised:
        gateway_helpers.post_stream(_request())
    assert raised.value.stage == "stream_idle"


@pytest.mark.parametrize("entry", ["post_json", "get_json"])
def test_incomplete_non_streaming_body_after_user_abort_is_not_retried(monkeypatch, entry):
    stopped = False
    closed = []

    def read_body():
        nonlocal stopped
        stopped = True
        raise http.client.IncompleteRead(b"")

    response = SimpleNamespace(read=read_body, close=lambda: closed.append(True))
    monkeypatch.setattr(gateway_helpers, "_gateway_urlopen", lambda *_args: response)
    monkeypatch.setattr(gateway_helpers, "_provider_is_interrupted", lambda: stopped)
    with pytest.raises(InterruptedError):
        getattr(gateway_helpers, entry)(_request())
    assert closed == [True]


def test_incomplete_body_exhausts_only_existing_model_retry_budget(monkeypatch):
    from agent_py_agent.agent.agent_core import provider_transient_auto_resume as retry

    attempts = []
    waits = []

    def open_response(*_args):
        attempts.append(True)
        return _truncated_chunked_response()

    monkeypatch.setattr(gateway_helpers, "_gateway_urlopen", open_response)
    monkeypatch.setattr(retry, "provider_transient_retry_delays", lambda _policy: (0.1, 0.2))
    monkeypatch.setattr(retry, "apply_retry_jitter", lambda value: value)
    monkeypatch.setattr(retry, "wait_interruptibly", waits.append)
    with pytest.raises(ProviderTransientError):
        retry.run_with_provider_transient_auto_resume(
            lambda: gateway_helpers.post_stream(_request())
        )
    assert attempts == [True, True, True]
    assert waits == [0.1, 0.2]


def test_native_partial_tool_input_never_returns_a_response_on_http_disconnect(monkeypatch):
    from agent_py_agent.agent.backends.anthropic import AnthropicCompatibleBackend
    from agent_py_agent.agent.backends.base import BackendOptions

    events = [
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "partial-1", "name": "write_file"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": '{"content":"not committed'}},
    ]
    wire_data = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
    response = _truncated_chunked_response(wire_data)
    monkeypatch.setattr(gateway_helpers, "_gateway_urlopen", lambda *_args: response)
    backend = AnthropicCompatibleBackend(BackendOptions(
        api_base="http://127.0.0.1:9", api_key="test", model_name="test",
        request_timeout=10, max_tokens=64, temperature=0.2, stream_enabled=True,
    ))
    with pytest.raises(ProviderTransientError):
        backend.generate("prompt", tools=[{"name": "write_file"}])
    assert response.closed
