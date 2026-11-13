"""ChatGPT 订阅 Responses 的 WebSocket 传输：订阅账号走 WebSocket、其它登录走 SSE；事件原样交给 collect_response；
握手失败沿 HTTP 同一分类与重试；握手阶段的超时（TLS/升级握手，response.create 还没发出）照 SSE 归 first_event 并交给
回合层退避重试；中途断开是可恢复错误；空闲超时、用户停止、发送许可拒绝。网络全部用假连接。"""
import importlib
import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.backends import responses_websocket as ws
from agent_py_agent.agent.backends.cache_diagnostics import compare_request_surfaces
from agent_py_agent.agent.backends.errors import (
    ProviderRequestRejectedError,
    ProviderTimeoutError,
    ProviderTransientError,
    ProviderUsageLimitError,
)
from agent_py_agent.agent.backends.gateway_helpers import GatewayRequest, provider_attempt_observer
from agent_py_agent.agent.backends.provider_headers import provider_session_scope
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

    # LLM: Model the immediate abort primitive used by the real websockets connection during cancellation.
    # 函数用途: 让假连接覆盖不等待关闭握手的 socket 收回路径。
    def close_socket(self):
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



# LLM: 订阅接口按 session-id 头决定提示缓存亲和（官方 Codex 每个请求都带）；10-05 生产统计订阅模型命中只有 10%–71%，
#   DeepSeek 98%，缺的就是这个头。这里走真实登录配置 → get_backend → 真实握手函数（只替换 websockets 的 connect），
#   核对握手头里的 session-id 与首包 prompt_cache_key 相同、同线程稳定、跨线程不同、未绑定会话不带；非订阅登录不加头。
# 函数用途: 钉住订阅登录每个请求都带会话亲和头，且与缓存键同值。
def test_subscription_handshake_carries_session_affinity_matching_cache_key(tmp_path, monkeypatch):
    from uuid import UUID

    from agent_py_agent.agent.backends.provider_headers import provider_session_scope

    alice = host(tmp_path / "affinity")
    key = setup(alice, "chatgpt")
    login(alice, monkeypatch)
    execute_model_profile_operation(alice, "set_default", {"profile_id": key})
    config = selected_model_config(alice)
    config.stream_enabled = True
    backend = get_backend(config.model_backend, config)
    handshakes = []

    def fake_connect(url, **kwargs):
        connection = FakeConnection(completed_events())
        handshakes.append((kwargs["additional_headers"], connection))
        return connection

    monkeypatch.setattr("websockets.sync.client.connect", fake_connect)

    def send_once(thread_id):
        if thread_id is None:
            backend.generate("你好")
        else:
            with provider_session_scope(("local", "main"), thread_id):
                backend.generate("你好")
        headers, connection = handshakes[-1]
        return headers.get("session-id"), connection.sent[0].get("prompt_cache_key")

    first, first_key = send_once("thread-a")
    again, again_key = send_once("thread-a")
    other, _ = send_once("thread-b")
    assert first == first_key == again == again_key and str(UUID(first)) == first
    assert other != first
    assert send_once(None) == (None, None)


# LLM: 17k Linux 车道实测：cabfix 把 HttpBackend 的 _gateway_request/request_stream_iter 改成 (StreamCall, options) 后，
#   OAuth 的两个覆盖仍是旧签名，账号登录的非流式请求和非订阅流式请求一调就 TypeError。这里经基类公开入口
#   （request_json、request_stream_iter）走到 OAuth 覆盖，核对账号头、禁重定向、首包预算和绝对期限都进了信封。
# 函数用途: 钉住 OAuth 覆盖与基类同签名，两种登录模式的流式与非流式都带上期限选项。
def test_oauth_backends_accept_the_base_stream_call_and_deadline_options(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends.http import StreamCallOptions

    for mode in ("chatgpt", "oauth_device"):
        alice = host(tmp_path / f"options-{mode}")
        key = setup(alice, mode)
        login(alice, monkeypatch)
        execute_model_profile_operation(alice, "set_default", {"profile_id": key})
        config = selected_model_config(alice)
        backend = get_backend(config.model_backend, config)
        seen = []
        monkeypatch.setattr(ws, "_connect", lambda req, seen=seen: seen.append(req) or FakeConnection(completed_events()))
        monkeypatch.setattr("agent_py_agent.agent.backends.http.post_stream_iter",
                            lambda req, seen=seen: seen.append(req) or iter(completed_events()))
        monkeypatch.setattr("agent_py_agent.agent.backends.http.post_json",
                            lambda req, seen=seen: seen.append(req) or {"ok": True})
        before = time.monotonic()
        options = StreamCallOptions(first_event_timeout_seconds=7.0, total_deadline_seconds=60.0)
        list(backend.request_stream_iter("/responses", {"model": "m"}, {}, options=options))
        assert backend.request_json("/responses", {"model": "m"}, {}, total_deadline_seconds=30.0) == {"ok": True}
        stream_request, json_request = seen
        for request_seen in seen:
            assert request_seen.headers["Authorization"] == "Bearer private-access"
            assert request_seen.allow_redirects is False
        assert stream_request.first_event_timeout == 7.0
        assert before + 60.0 <= stream_request.deadline <= time.monotonic() + 60.0
        assert json_request.first_event_timeout is None
        assert before + 30.0 <= json_request.deadline <= time.monotonic() + 30.0


# LLM: fix3ar 实测：OAuth 的 _gateway_request 原先用裸 request_timeout，后台预算（provider_request_budget）在
#   订阅账号上既收不紧也放不宽单次 HTTP 超时；现在与基类一样走 provider_request_timeout。
# 函数用途: 钉住 OAuth 信封的单次超时跟随后台预算的剩余时长。
def test_oauth_envelope_timeout_follows_provider_request_budget(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends.http import StreamCall
    from agent_py_agent.agent.backends.request_scope import provider_request_budget

    alice = host(tmp_path / "budget")
    key = setup(alice, "oauth_device")
    login(alice, monkeypatch)
    execute_model_profile_operation(alice, "set_default", {"profile_id": key})
    config = selected_model_config(alice)
    backend = get_backend(config.model_backend, config)
    call = StreamCall(path="/responses", payload={"model": "m"}, headers={})
    assert backend._gateway_request(call).timeout == backend.request_timeout
    with provider_request_budget(3.0):
        budgeted = backend._gateway_request(call).timeout
    assert 0 < budgeted <= 3.0 and budgeted != backend.request_timeout

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


# LLM: WebSocket 首包是模型请求体的一部分，必须走同一个唯一编码器（保持调用方顺序），
#   不能自己另写一份 json.dumps 而与 HTTP 路径口径漂移。历史重放的键序稳定性由消息层负责，
#   不在编码器里排序。这里直接抓原始文本，不经 json.loads（会丢键序）。
# 函数用途: 钉住 response.create 首包字节与唯一编码器一致、且保持调用方给的键序。
def test_create_message_bytes_are_stable_across_key_insertion_order():
    from agent_py_agent.agent.backends.gateway_helpers import gateway_request_body

    class RawConnection:
        def __init__(self):
            self.raw = []

        def send(self, text):
            self.raw.append(text)

    payload = {"model": "m", "input": [{"type": "function_call", "call_id": "c1",
                                       "name": "read_file", "arguments": '{"path":"a"}'}]}
    request = GatewayRequest(api_base="http://127.0.0.1:1", api_key="<redacted>",
                             path="/v1/responses", payload=payload, headers={}, timeout=10)
    connection = RawConnection()
    ws._send_create_message(connection, request)

    expected = gateway_request_body({"type": "response.create", **payload}).decode("utf-8")
    assert connection.raw == [expected]
    assert expected == json.dumps({"type": "response.create", **payload},
                                  ensure_ascii=False), "首包必须保持调用方给的键序"


# ---------------------------------------------------------------- 信封绝对期限在 WebSocket 流上的收口（wsdeadline，2026-10-05）

# LLM: 滴流假连接：每次 recv 间隔 0.05 秒返回一条非终止事件；记录发送、关闭路径与滴数。
#   覆盖"服务端持续有事件"这一条路径（绝对期限此前只在连接前检查，idle 每次事件刷新）。
# 函数用途: 为绝对期限收口用例提供持续滴事件的假连接。
class DripConnection:
    def __init__(self, interval=0.05):
        self.interval = interval
        self.count = 0
        self.sent = []
        self.closed = False
        self.closed_socket = False

    def send(self, text):
        self.sent.append(text)

    def recv(self, timeout=None):
        time.sleep(self.interval)
        self.count += 1
        return json.dumps({"type": "response.in_progress"})

    def close(self):
        self.closed = True

    def close_socket(self):
        self.closed_socket = True


# LLM: wsdeadline：idle 阶段的截止取「常规空闲窗口」与「信封绝对期限」更早者（与 HTTP 路径
#   _StreamIdleWatchdog 的 hard_deadline 同口径）。服务端持续滴非终止事件时，绝对期限必须终止流：
#   结构化 ProviderTimeoutError（stage=stream_idle，与 HTTP 硬期限在见过数据后的取值一致）、连接已关、
#   不把半截事件当完成。3 秒上限保护：实现回退时用例变红而不是挂起。
# 函数用途: 钉住 WebSocket 流在信封绝对期限处收口。
def test_websocket_stream_stops_at_envelope_deadline_despite_live_events(monkeypatch):
    connection = DripConnection()
    monkeypatch.setattr(ws, "_connect", lambda request: connection)
    request = GatewayRequest(
        api_base="https://chatgpt.example.test/backend-api/codex", api_key="k", path="/responses",
        payload={"model": "m", "stream": True, "input": []},
        headers={"Authorization": "Bearer private-token"},
        timeout=10, first_event_timeout=1.0, deadline=time.monotonic() + 0.5)
    started = time.monotonic()
    hit_stage = None
    generator = ws.iter_responses_websocket(request)
    try:
        while time.monotonic() - started <= 3.0:
            next(generator)
    except ProviderTimeoutError as exc:
        hit_stage = exc.stage
    except StopIteration:
        pass
    finally:
        generator.close()
    elapsed = time.monotonic() - started
    assert hit_stage == "stream_idle", f"绝对期限未按 stream_idle 收口（hit={hit_stage!r}，{elapsed:.2f}s）"
    assert elapsed < 2.0, f"应在 0.5s 期限附近收口，实际 {elapsed:.2f}s"
    assert connection.closed_socket, "到期后必须关 socket，不能泄漏连接"
    assert connection.count < 30, f"不得跑满 3 秒的滴数（实际 {connection.count}）"


# LLM: 对照：信封没有绝对期限时（主模型路径），持续有效事件照旧按 request.timeout 滚动，不被截断——
#   本次改动只影响带 deadline 的调用（辅助调用/探针），主模型长流语义不变。
# 函数用途: 证明无 deadline 时 WebSocket 流不被新逻辑截断。
def test_websocket_stream_without_deadline_keeps_live_events():
    connection = DripConnection()
    request = GatewayRequest(
        api_base="https://chatgpt.example.test/backend-api/codex", api_key="k", path="/responses",
        payload={"model": "m", "stream": True, "input": []},
        headers={"Authorization": "Bearer private-token"},
        timeout=10, first_event_timeout=1.0, deadline=None)
    started = time.monotonic()
    produced = 0
    generator = ws._events(connection, request)
    try:
        while time.monotonic() - started < 1.0:
            next(generator)
            produced += 1
    except Exception as exc:  # noqa: BLE001 - 无期限时不应有任何超时
        raise AssertionError(f"无 deadline 的流不应被截断: {exc!r}") from exc
    finally:
        generator.close()
    assert produced >= 10, f"1 秒内应持续产出事件，实际 {produced}"


# LLM: rejectdiag：WebSocket 握手 403 与 HTTP 403 走同一分类与诊断（_handshake_error 把状态码/头/正文
#   交给 _runtime_http_error），回执投影必须能给出 forbidden_unknown 而不是“原因未定”。
# 函数用途: 钉住握手拒绝也携带结构化诊断（含白名单头）。
def test_handshake_403_records_structured_diagnostic(monkeypatch):
    def failing_connect(request):
        raise InvalidStatus(Response(403, "status", Headers({
            "Content-Type": "application/json", "X-Request-Id": "ws-req-1"}), b""))

    monkeypatch.setattr(ws, "_connect", failing_connect)
    with pytest.raises(ProviderRequestRejectedError) as caught:
        list(ws.iter_responses_websocket(request()))
    diagnostic = caught.value.details["rejection_diagnostic"]
    assert diagnostic["status_code"] == 403
    assert diagnostic["cause_code"] == "forbidden_unknown"
    assert diagnostic["headers"]["x-request-id"] == "ws-req-1"


# ---------------------------------------------------------------- 发送阶段断开与增量续接诊断（v14 ws-media-fix）

# 增量链用例的工具调用及其结果；与连接池用例同形，上一轮回复按原生重放后逐字节等于 CALL。
CALL = {"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": json.dumps({"path": "a"}, sort_keys=True)}
RESULT = {"type": "function_call_output", "call_id": "c1", "output": "content"}


# LLM: plans 每项对应一次 send：事件列表正常排入回复，异常则在 send 处抛出（模拟写 socket 时连接已被对端关闭）。
#   recv(timeout=0) 无数据时抛 TimeoutError，与真实库“没有未读帧”一致，因此能通过复用前的干净检查。不联网。
# 类用途: 模拟可跨多次请求复用、并可在发送阶段断开的 WebSocket 连接。
class ScriptedSocket:
    # 函数用途: 记录每次 send 对应的脚本结果。
    def __init__(self, *plans):
        self.plans, self.messages, self.sent, self.closed = list(plans), [], [], False

    # 函数用途: 记下首包内容，再按脚本排入回复或抛出关闭异常。
    def send(self, text):
        self.sent.append(json.loads(text))
        plan = self.plans.pop(0)
        if isinstance(plan, BaseException):
            raise plan
        self.messages.extend(json.dumps(event) for event in plan)

    # 函数用途: 返回下一帧；无数据时超时，已关闭时报连接关闭。
    def recv(self, timeout=None):
        if self.closed:
            raise ConnectionClosedError(None, None)
        if not self.messages:
            raise TimeoutError()
        return self.messages.pop(0)

    # 函数用途: 记录正常关闭。
    def close(self):
        self.closed = True

    # 函数用途: 记录不等关闭握手的 socket 收回。
    def close_socket(self):
        self.closed = True


# LLM: 计时器到期由 _expire_websocket_send 调 close_socket 唤醒阻塞的发送，真实库随后以 ConnectionClosed 报出写失败。
# 类用途: 模拟发送卡住、直到本端关 socket 才失败的连接。
class BlockedSendSocket:
    # 函数用途: 初始化发送阻塞开关。
    def __init__(self):
        self.released, self.closed = threading.Event(), False

    # 函数用途: 阻塞到 socket 被关，再按真实库报连接关闭。
    def send(self, text):
        self.released.wait(5)
        raise ConnectionClosedError(None, None)

    # 函数用途: 记录关闭并唤醒发送。
    def close_socket(self):
        self.closed = True
        self.released.set()


# 函数用途: 生成带响应 ID 的完成终态，使干净连接能回池并留下增量锚点。
def completed_with_id(response_id, output):
    return [{"type": "response.completed", "response": {"id": response_id, "status": "completed", "output": output,
                                                         "usage": {"input_tokens": 5, "output_tokens": 1}}}]


# 函数用途: 构造会话内可比较的请求，只替换 input，其余字段与键序保持一致以便命中增量链。
def session_request(items):
    return replace(request(), payload={"model": "m", "stream": True, "input": items})


# LLM: 生产全局池只在本组用例内换成新池，结束时关闭，避免与其它用例共享空闲连接或增量锚点。
# 函数用途: 为会话复用用例提供隔离的连接池。
@pytest.fixture
def fresh_pool(monkeypatch):
    pool = type(ws._POOL)()
    monkeypatch.setattr(ws, "_POOL", pool)
    yield pool
    pool.close()


# LLM: 生产事故形态：新建连接上发送 response.create 时对端断开。必须是可恢复的 ProviderTransientError（阶段 send_request，
#   带关闭帧信息），原关闭异常保留为 __cause__，连接被收回；不能把库的原始异常漏给上层变成未知错误。
# 函数用途: 钉住非复用连接发送阶段断开的错误分类与诊断。
@pytest.mark.parametrize(("close", "detail"), [
    (ConnectionClosedError(None, None), "无关闭帧"),
    (ConnectionClosedError(Close(1009, "message too big"), None), "服务端关闭码 1009，原因 message too big"),
])
def test_send_phase_close_is_a_recoverable_transient_error(monkeypatch, close, detail):
    connection = ScriptedSocket(close)
    monkeypatch.setattr(ws, "_connect", lambda req: connection)
    with pytest.raises(ProviderTransientError) as caught:
        list(ws.iter_responses_websocket(request()))
    assert caught.value.__cause__ is close
    assert "阶段 send_request" in str(caught.value) and detail in str(caught.value)
    assert connection.closed and len(connection.sent) == 1


# LLM: 复用连接（17 分钟空闲后半死的旧连接）发送时断开：首轮仍被识别为可恢复的复用失效，换新连接全量重发一次；
#   新连接发送也断开时，最终交给上层的是 send_request 阶段的 ProviderTransientError，而不是原始关闭异常。
# 函数用途: 钉住复用连接发送失败后的一次全量回退及回退失败后的错误分类。
def test_reused_send_close_falls_back_once_then_surfaces_transient_error(monkeypatch, fresh_pool):
    pooled = ScriptedSocket(completed_with_id("r1", [CALL]), ConnectionClosedError(None, None))
    fallback = ScriptedSocket(ConnectionClosedError(None, None))
    sockets, opened = iter([pooled, fallback]), []
    monkeypatch.setattr(ws, "_connect", lambda req: opened.append(req) or next(sockets))
    with provider_session_scope(("local", "owner-a"), "thread-send"):
        list(ws.iter_responses_websocket(session_request([{"role": "user", "content": "你好"}])))
        with pytest.raises(ProviderTransientError) as caught:
            list(ws.iter_responses_websocket(session_request([{"role": "user", "content": "你好"}, CALL, RESULT])))
    assert isinstance(caught.value.__cause__, ConnectionClosedError)
    assert "阶段 send_request" in str(caught.value)
    assert len(opened) == 2 and pooled.closed and fallback.closed
    assert pooled.sent[1]["previous_response_id"] == "r1" and pooled.sent[1]["input"] == [RESULT]
    assert "previous_response_id" not in fallback.sent[0]
    assert fallback.sent[0]["input"] == [{"role": "user", "content": "你好"}, CALL, RESULT]


# LLM: 用户停止优先于任何分类：发送中被停止关掉 socket 时报 InterruptedError，不当作服务商故障重试。
# 函数用途: 钉住发送阶段的用户停止语义。
def test_user_stop_during_send_is_an_interruption(monkeypatch):
    close = ConnectionClosedError(None, None)
    monkeypatch.setattr(ws, "_provider_is_interrupted", lambda: True)
    with pytest.raises(InterruptedError) as caught:
        ws._send_create_message(ScriptedSocket(close), request())
    assert caught.value.__cause__ is close


# LLM: 首包预算到期由计时器关 socket，真实库随后报 ConnectionClosed；这仍是 first_event 超时，不能被新的断开分支改判。
#   无绝对期限时发送预算取 timeout 与 first_event_timeout 的较大者且最少 1 秒，所以这里用 timeout=1。
# 函数用途: 钉住发送阶段超时的分类不变。
def test_send_budget_expiry_stays_a_first_event_timeout():
    connection = BlockedSendSocket()
    with pytest.raises(ProviderTimeoutError) as caught:
        ws._send_create_message(connection, request(timeout=1))
    assert caught.value.stage == "first_event"
    assert isinstance(caught.value.__cause__, ConnectionClosedError) and connection.closed


# LLM: 复用连接线上只发 previous_response_id 加差量，但请求尝试的缓存诊断必须按完整历史计算：
#   32 条（对齐检查点）历史后追加一轮，应报 history_appended、共享前缀 32，且不能因锚点或差量切片误报 options/前缀变化。
# 函数用途: 钉住增量续接的请求面诊断使用完整 input。
def test_reused_incremental_send_reports_full_prefix_diagnostics(monkeypatch, fresh_pool):
    history = [{"role": "user", "content": f"m{index}"} for index in range(32)]
    connection = ScriptedSocket(completed_with_id("r1", [CALL]), completed_with_id("r2", []))
    monkeypatch.setattr(ws, "_connect", lambda req: connection)
    observed = []
    with provider_session_scope(("local", "owner-a"), "thread-diag"), \
            provider_attempt_observer(observed.append, cache_diagnostics=True):
        list(ws.iter_responses_websocket(session_request(history)))
        list(ws.iter_responses_websocket(session_request([*history, CALL, RESULT])))
    assert connection.sent[1]["previous_response_id"] == "r1" and connection.sent[1]["input"] == [RESULT]
    first, second = [row["request_surface"] for row in observed if row["status"] == "started"]
    assert second["message_count"] == 34
    result = compare_request_surfaces(first, second)
    assert result["changes"] == ["history_appended"]
    assert result["shared_message_prefix"] == 32 and result["comparable"] is True


def test_main_tool_chain_survives_interim_auxiliary_reply_and_reset_closes_both(monkeypatch, fresh_pool, tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.agent.backends.provider_headers import (
        current_provider_session,
        provider_runtime_scope,
    )
    from agent_py_agent.agent.backends.request_scope import _OWNER
    from agent_py_agent.agent.conversation.auxiliary_model_call import (
        AuxiliaryModelCallRequest,
        generate_auxiliary_model_response,
    )
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    main = ScriptedSocket(completed_with_id('main-r1', [CALL]), completed_with_id('main-r2', []))
    interim = ScriptedSocket(completed_with_id('interim-r1', []))
    sockets, opened = iter([main, interim]), []
    def connect(req):
        connection = next(sockets)
        opened.append(connection)
        return connection
    monkeypatch.setattr(ws, '_connect', connect)
    observed = []
    class Backend:
        name = 'wire-test'
        def generate(self, prompt, **kwargs):
            observed.append((current_provider_session(), _OWNER.get()))
            list(ws.iter_responses_websocket(replace(session_request([{'role': 'user', 'content': prompt}]),
                payload={'model': 'm', 'stream': True, 'input': [{'role': 'user', 'content': prompt}], 'tools': []})))
            return ModelResponse(text='临时回复', backend=self.name)
    agent.backend = Backend()
    user = {'role': 'user', 'content': 'main original input'}
    tools = [{'type': 'function', 'name': 'read_file', 'parameters': {'type': 'object'}}]
    def main_request(items):
        return replace(session_request(items), payload={'model': 'm', 'stream': True, 'input': items, 'tools': tools})
    with provider_runtime_scope(agent, SimpleNamespace(thread_id='real-thread')):
        session, owner = current_provider_session(), _OWNER.get()
        list(ws.iter_responses_websocket(main_request([user])))
        response = generate_auxiliary_model_response(AuxiliaryModelCallRequest(agent=agent,
            prompt='当前进度', thread_id='real-thread', request_id='original-request', run_id='original-run',
            task_id='original-task', attempt_id='original-attempt', purpose='active_turn_input_reply', tools=[]))
        assert response.text == '临时回复'
        list(ws.iter_responses_websocket(main_request([user, CALL, RESULT])))
        assert opened == [main, interim]
        assert main.sent[1]['previous_response_id'] == 'main-r1'
        assert main.sent[1]['input'] == [RESULT]
        assert main.sent[1]['tools'] == tools
        assert 'previous_response_id' not in interim.sent[0]
        assert observed == [(session, owner)]
        ws.reset_responses_session()
        assert main.closed and interim.closed
        assert not fresh_pool._states and not fresh_pool._idle
    record = agent._model_call_ledger.records()[0]
    assert record.request_id == 'original-request' and record.run_id == 'original-run'
    assert record.metadata['thread_id'] == 'real-thread' and record.metadata['attempt_id'] == 'original-attempt'


def test_transport_purpose_affinity_is_stable_scoped_and_not_owner_identity():
    from agent_py_agent.agent.backends.provider_headers import (
        chatgpt_session_uuid,
        current_provider_affinity,
        current_provider_session,
        provider_transport_scope,
        request_headers,
    )
    from agent_py_agent.agent.backends.request_scope import _OWNER
    from agent_py_agent.agent.backends.responses import _session_cache_key

    seen = []
    for thread in ('one', 'one', 'two'):
        with provider_session_scope(('local', 'main', 'owner'), thread):
            base, owner = current_provider_session(), _OWNER.get()
            assert current_provider_affinity() == base
            with pytest.raises(RuntimeError), provider_transport_scope('active_turn_input_reply'):
                affinity = current_provider_affinity()
                seen.append(affinity)
                assert current_provider_session() == base and _OWNER.get() == owner
                assert affinity != base
                assert _session_cache_key()['prompt_cache_key'] == affinity
                assert request_headers({}, {}, 'x-session') == {'User-Agent': 'my-agent/1.0', 'x-session': affinity}
                assert _session_cache_key(subscription=True)['prompt_cache_key'] == chatgpt_session_uuid()
                raise RuntimeError('cancelled example')
            assert current_provider_affinity() == base
            assert _session_cache_key()['prompt_cache_key'] == base
    assert seen[0] == seen[1] and seen[0] != seen[2]
    assert current_provider_session() == '' and current_provider_affinity() == ''
