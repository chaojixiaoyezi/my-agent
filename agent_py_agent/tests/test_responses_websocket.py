"""ChatGPT 订阅 Responses 的 WebSocket 传输：订阅账号走 WebSocket、其它登录走 SSE；事件原样交给 collect_response；
握手失败沿 HTTP 同一分类与重试；中途断开是可恢复错误；空闲超时、用户停止、发送许可拒绝。网络全部用假连接。"""
import json

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.backends import responses_websocket as ws
from agent_py_agent.agent.backends.errors import (
    ProviderRequestRejectedError,
    ProviderTimeoutError,
    ProviderTransientError,
    ProviderUsageLimitError,
)
from agent_py_agent.agent.backends.gateway_helpers import GatewayRequest, provider_attempt_observer
from agent_py_agent.agent.backends.responses_wire import collect_response, response_fields
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    selected_model_config,
)
from agent_py_agent.tests.test_model_oauth import host, login, setup


class FakeConnection:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout=None):
        if self.closed:
            raise ConnectionClosedError(None, None)
        if not self.messages:
            raise TimeoutError()
        item = self.messages.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True


def completed_events(text="完整回复"):
    message = {"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text}]}
    return [json.dumps(event) for event in (
        {"type": "response.created", "response": {"status": "in_progress"}},
        {"type": "codex.rate_limits", "rate_limits": {}},
        {"type": "response.output_text.delta", "delta": text},
        {"type": "response.output_item.done", "item": message},
        {"type": "response.completed", "response": {"status": "completed", "output": [], "usage": {"input_tokens": 5, "output_tokens": 3}}},
    )]


def request(**values):
    return GatewayRequest(api_base="https://chatgpt.example.test/backend-api/codex", api_key="k", path="/responses",
                          payload={"model": "m", "stream": True, "input": []},
                          headers={"Authorization": "Bearer private-token", "Content-Type": "application/json",
                                   "Accept": "text/event-stream", "ChatGPT-Account-ID": "acct"},
                          timeout=values.pop("timeout", 30), **values)


def invalid_status(code, body=b'{"error": {"message": "limit"}}'):
    return InvalidStatus(Response(code, "status", Headers({"Content-Type": "application/json"}), body))


def test_events_flow_into_the_existing_collector_and_close_the_connection(monkeypatch):
    connection = FakeConnection(completed_events())
    seen = {}
    monkeypatch.setattr(ws, "_connect", lambda req: seen.setdefault("request", req) and connection)
    lines = ws.iter_responses_websocket(request())
    result = response_fields(collect_response(lines, None, None), "m")
    lines.close()
    assert result["text"] == "完整回复" and result["truncated"] is False and result["usage"]["output_tokens"] == 3
    assert connection.sent == [{"type": "response.create", "model": "m", "stream": True, "input": []}]
    assert connection.closed


def test_headers_keep_auth_add_beta_and_drop_http_only_fields(monkeypatch):
    captured = {}

    def fake_connect(url, **kwargs):
        captured.update(url=url, **kwargs)
        return FakeConnection(completed_events())

    monkeypatch.setattr("websockets.sync.client.connect", fake_connect)
    list(ws.iter_responses_websocket(request()))
    headers = captured["additional_headers"]
    assert captured["url"] == "wss://chatgpt.example.test/backend-api/codex/responses"
    assert headers["Authorization"] == "Bearer private-token" and headers["ChatGPT-Account-ID"] == "acct"
    assert headers["OpenAI-Beta"] == ws.BETA_HEADER_VALUE
    assert not {"Content-Type", "Accept"} & set(headers)
    assert captured["user_agent_header"].startswith("my-agent/")
    # 与官方 Codex 一致不主动发心跳 ping（服务端 ping 仍由库自动回应）。
    assert captured["ping_interval"] is None and captured["ping_timeout"] is None


def test_disconnect_before_completion_is_a_recoverable_error(monkeypatch):
    connection = FakeConnection([completed_events()[2], ConnectionClosedError(None, None)])
    monkeypatch.setattr(ws, "_connect", lambda req: connection)
    with pytest.raises(ProviderTransientError, match="回复完成前断开") as caught:
        collect_response(ws.iter_responses_websocket(request()), None, None)
    assert connection.closed
    assert "阶段 stream_idle" in str(caught.value) and "无关闭帧" in str(caught.value)


def test_disconnect_diagnostics_name_the_closing_side_code_and_stage(monkeypatch):
    server_close = ConnectionClosedError(Close(1011, "keepalive ping timeout"), None)
    monkeypatch.setattr(ws, "_connect", lambda req: FakeConnection([server_close]))
    with pytest.raises(ProviderTransientError) as caught:
        list(ws.iter_responses_websocket(request()))
    text = str(caught.value)
    assert "阶段 first_event" in text and "服务端关闭码 1011" in text and "原因 keepalive ping timeout" in text
    local_close = ConnectionClosedError(None, Close(1001, ""))
    monkeypatch.setattr(ws, "_connect", lambda req: FakeConnection([completed_events()[0], local_close]))
    with pytest.raises(ProviderTransientError) as caught:
        list(ws.iter_responses_websocket(request()))
    assert "阶段 stream_idle" in str(caught.value) and "本端关闭码 1001）" in str(caught.value)  # 原因为空时不写“原因”


def test_silence_times_out_with_the_same_stages_as_sse(monkeypatch):
    monkeypatch.setattr(ws, "_connect", lambda req: FakeConnection([]))
    with pytest.raises(ProviderTimeoutError) as first:
        list(ws.iter_responses_websocket(request(timeout=1, first_event_timeout=0.05)))
    assert first.value.stage == "first_event"
    monkeypatch.setattr(ws, "_connect", lambda req: FakeConnection([completed_events()[0]]))
    with pytest.raises(ProviderTimeoutError) as idle:
        list(ws.iter_responses_websocket(request(timeout=0.05)))
    assert idle.value.stage == "stream_idle"


def test_user_stop_closes_the_stream(monkeypatch):
    connection = FakeConnection([completed_events()[0], completed_events()[2]])
    monkeypatch.setattr(ws, "_connect", lambda req: connection)
    lines = ws.iter_responses_websocket(request())
    assert json.loads(next(lines))["type"] == "response.created"
    monkeypatch.setattr(ws, "_provider_is_interrupted", lambda: True)
    with pytest.raises(InterruptedError):
        next(lines)
    assert connection.closed


def test_handshake_failures_reuse_http_classification_and_retries(monkeypatch):
    attempts, events = [], []
    monkeypatch.setattr(ws, "_request_retry_wait", lambda req, seconds: None)

    def flaky(req):
        attempts.append(1)
        if len(attempts) == 1:
            raise invalid_status(503)
        return FakeConnection(completed_events())

    monkeypatch.setattr(ws, "_connect", flaky)
    with provider_attempt_observer(events.append):
        list(ws.iter_responses_websocket(request()))
    assert len(attempts) == 2
    assert [(e["status"], e.get("http_status"), e.get("retry_scheduled")) for e in events] == [
        ("started", None, None), ("failed", 503, True), ("started", None, None), ("response_opened", 101, None)]
    assert all(e["method"] == "WEBSOCKET" for e in events)
    for code, error in ((401, ProviderRequestRejectedError), (429, ProviderUsageLimitError)):
        monkeypatch.setattr(ws, "_connect", lambda req, code=code: (_ for _ in ()).throw(invalid_status(code)))
        with pytest.raises(error):
            list(ws.iter_responses_websocket(request(max_retries=0)))


def test_send_permit_is_refused_before_any_connection(monkeypatch):
    import time

    monkeypatch.setattr(ws, "_connect", lambda req: pytest.fail("有发送许可时不能建连"))
    from types import SimpleNamespace

    permit = SimpleNamespace(admit=lambda attempt: None)
    permitted = request(send_permit=permit, max_retries=0, allow_redirects=False, deadline=time.monotonic() + 30)
    with pytest.raises(ValueError, match="WebSocket 传输不支持实验发送许可"):
        list(ws.iter_responses_websocket(permitted))


def test_only_the_subscription_login_uses_websocket(tmp_path, monkeypatch):
    for mode, expected in (("chatgpt", "websocket"), ("oauth_device", "sse")):
        alice = host(tmp_path / mode)
        key = setup(alice, mode)
        login(alice, monkeypatch)
        execute_model_profile_operation(alice, "set_default", {"profile_id": key})
        config = selected_model_config(alice)
        config.stream_enabled = True
        backend = get_backend(config.model_backend, config)
        used = []
        monkeypatch.setattr(ws, "_connect", lambda req: used.append(("websocket", req)) or FakeConnection(completed_events()))
        monkeypatch.setattr("agent_py_agent.agent.backends.http.post_stream_iter",
                            lambda req: used.append(("sse", req)) or iter(completed_events()))
        response = backend.generate("你好")
        assert response.text == "完整回复" and [kind for kind, _ in used] == [expected]
        assert used[0][1].headers["Authorization"] == "Bearer private-access"
