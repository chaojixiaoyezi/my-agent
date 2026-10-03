"""ChatGPT 订阅 Responses 的 WebSocket 传输：订阅账号走 WebSocket、其它登录走 SSE；事件原样交给 collect_response；
握手失败沿 HTTP 同一分类与重试；握手阶段的超时（TLS/升级握手，response.create 还没发出）照 SSE 归 first_event 并交给
回合层退避重试；中途断开是可恢复错误；空闲超时、用户停止、发送许可拒绝。网络全部用假连接。"""
import importlib
import json
import sys
from pathlib import Path

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


# ---------------------------------------------------------------- 握手超时归 first_event（2026-10-03）

# LLM: 假连接记录 send 次数和 close 次数；recv 永远抛 TimeoutError（调用方给多少 timeout 都算超时），
#   不联网、不依赖真实 websockets 传输。sent 列表是对外可见的“response.create 有没有发出去”证据。
class CountingConnection:
    # 函数用途: 建一个一次都没发过的假连接，用来观察 send 是否被调用。
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, text):
        self.sent.append(text)

    def recv(self, timeout=None):
        raise TimeoutError()

    def close(self):
        self.closed = True


def test_handshake_timeout_is_a_first_event_stream_timeout_and_never_sends(monkeypatch):
    """握手超时（连接还没打开）按 SSE 归 first_event，且这次尝试里 response.create 一次都没发出去。

    抓的是“不会重复计费”的前提：send 只在连接打开之后发生；握手阶段超时时连接没建成，
    也就没有任何采样请求被发出去。重试交给回合层（传输层的握手重试次数不变）。
    """
    attempts = []
    sent = []

    def connect(req):
        attempts.append(1)
        sent.append("no-send")
        raise TimeoutError("_ssl.c:993: The handshake operation timed out")

    monkeypatch.setattr(ws, "_connect", connect)
    with pytest.raises(ProviderTimeoutError) as caught:
        list(ws.iter_responses_websocket(request(max_retries=0)))
    assert caught.value.stage == "first_event"
    assert attempts == [1] and sent == ["no-send"]  # 建连尝试过，但没有任何连接可 send


def test_handshake_timeout_sends_nothing_when_no_retry_is_allowed(monkeypatch):
    """不重试时握手超时直接上抛 first_event；全程没有连接，send 次数必须是 0。"""
    sent = []

    def failing(req):
        sent.append("connect")
        raise TimeoutError("timed out while waiting for handshake response")

    monkeypatch.setattr(ws, "_connect", failing)
    with pytest.raises(ProviderTimeoutError) as caught:
        list(ws.iter_responses_websocket(request(max_retries=0)))
    assert caught.value.stage == "first_event"
    assert str(caught.value).startswith("模型接口等待首个流式事件超时")
    assert sent == ["connect"]  # 只有建连尝试，没有任何 send


def test_handshake_timeout_round_ends_before_any_request_is_sent(monkeypatch):
    """握手超时这一轮不会发出任何采样请求：连接没建成，就不会有 connection.send。"""
    sent = []

    def failing(req):
        sent.append("attempted-connect")
        raise TimeoutError("timed out while waiting for handshake response")

    monkeypatch.setattr(ws, "_connect", failing)
    with pytest.raises(ProviderTimeoutError) as caught:
        list(ws.iter_responses_websocket(request(max_retries=0)))
    assert caught.value.stage == "first_event"
    assert sent == ["attempted-connect"]  # 只尝试建连，从未进入发送阶段


def test_handshake_non_timeout_failures_keep_their_classification(monkeypatch):
    """非超时握手失败分类不变：带状态码走 HTTP 分类，连接被拒走网络分类。"""
    monkeypatch.setattr(ws, "_connect", lambda req: (_ for _ in ()).throw(invalid_status(503)))
    with pytest.raises(ProviderTransientError):
        list(ws.iter_responses_websocket(request(max_retries=0)))
    monkeypatch.setattr(ws, "_connect", lambda req: (_ for _ in ()).throw(ConnectionRefusedError(61, "refused")))
    with pytest.raises(ProviderTransientError):
        list(ws.iter_responses_websocket(request(max_retries=0)))
    monkeypatch.setattr(ws, "_connect",
                        lambda req: (_ for _ in ()).throw(OSError("nodename nor servname provided")))
    with pytest.raises(Exception) as caught:
        list(ws.iter_responses_websocket(request(max_retries=0)))
    assert not isinstance(caught.value, ProviderTimeoutError)


def test_handshake_user_stop_wins_over_timeout(monkeypatch):
    """握手中用户停止优先：仍是 InterruptedError，不重试、不归超时。"""
    calls = []
    monkeypatch.setattr(ws, "_connect", lambda req: calls.append(1) or (_ for _ in ()).throw(
        TimeoutError("The handshake operation timed out")))
    monkeypatch.setattr(ws, "_provider_is_interrupted", lambda: True)
    with pytest.raises(InterruptedError):
        list(ws.iter_responses_websocket(request()))
    assert calls == [1]


def test_post_open_silence_keeps_its_original_stages(monkeypatch):
    """连接打开、response.create 发出之后的静默超时，stage 仍是 first_event / stream_idle。"""
    monkeypatch.setattr(ws, "_connect", lambda req: FakeConnection([]))
    with pytest.raises(ProviderTimeoutError) as first:
        list(ws.iter_responses_websocket(request(timeout=1, first_event_timeout=0.05)))
    assert first.value.stage == "first_event"
    monkeypatch.setattr(ws, "_connect", lambda req: FakeConnection([completed_events()[0]]))
    with pytest.raises(ProviderTimeoutError) as idle:
        list(ws.iter_responses_websocket(request(timeout=0.05)))
    assert idle.value.stage == "stream_idle"

# ---------------------------------------------------------------- 回合层退避与生成层分工（2026-10-03）

def test_handshake_timeout_round_is_retried_by_the_turn_auto_resume(monkeypatch):
    """回合层第一次握手超时、第二次成功 → 回合完成，记一次重试（有重试进度通知）。

    这里按真实链路串起来：用 WebSocket 传输真的跑一次（假连接），把传输层抛出的错误
    交给回合层退避链；证明握手超时确实能被回合层接住，而不是整轮失败。
    """
    from agent_py_agent.agent import agent_core as core_pkg

    auto_resume = importlib.import_module(f"{core_pkg.__name__}.provider_transient_auto_resume")

    connections, waits, typed = [], [], []

    class RetrySink:
        # 函数用途: 收下结构化重试进度，阻止回落到兼容文本通道。
        def __call__(self, _text):
            raise AssertionError("typed retry sink should avoid legacy text")

        # 函数用途: 记录一次模型回合级重连事件。
        def write_provider_retry(self, **payload):
            typed.append(dict(payload))
            return True

    def connect(req):
        if not connections:
            connections.append("first")
            raise TimeoutError("_ssl.c:993: The handshake operation timed out")
        return FakeConnection(completed_events())

    monkeypatch.setattr(ws, "_connect", connect)
    monkeypatch.setattr(auto_resume, "wait_interruptibly", waits.append)
    monkeypatch.setattr(auto_resume, "apply_retry_jitter", lambda value: value)
    monkeypatch.setattr(auto_resume, "provider_transient_retry_delays", lambda _policy=None: (2.0, 5.0))

    # 函数用途: 跑一次真实的 WebSocket 回合（含传输层分类），交给回合层退避链。
    def operation():
        lines = ws.iter_responses_websocket(request(max_retries=0))
        return response_fields(collect_response(lines, None, None), "m")

    result = auto_resume.run_with_provider_transient_auto_resume(operation, on_chunk=RetrySink())
    assert result["text"] == "完整回复"
    assert connections == ["first"] and waits == [2.0]
    assert [item["attempt"] for item in typed] == [1]
    assert typed[0]["error_type"] == "ProviderTimeoutError"


def test_handshake_timeout_is_left_for_the_turn_layer_by_generation():
    """生成层对这种错误返回 None：交给回合层，不在生成层立刻多打一枪。"""
    from agent_py_agent.agent.agent_core.tool_model_generation import _retry_once_after_timeout

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_timeout_gate5_retry import ModelGenerateParams, _agent, _params

    params = _params()
    request = ModelGenerateParams(agent=_agent(), params=params, prompt="hello", tool_rounds=1)
    assert _retry_once_after_timeout(
        request,
        ProviderTimeoutError("handshake", stage="first_event"),
    ) is None
