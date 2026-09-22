"""Jev 原生请求一次发送、身份头复用和迟到解析拒绝；不调用真实模型。"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import gateway_request_limits as limits
from agent_py_agent.agent.backends import typesafe_decision as adapter
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.decision_protocol import DecisionInputError, DecisionRequest
from agent_py_agent.agent.backends.errors import ProviderTimeoutError
from agent_py_agent.agent.backends.provider_headers import provider_runtime_scope
from agent_py_agent.tests.test_decision_protocol import binding, questions, response


@pytest.mark.parametrize("base", ["http://example.test", "http://example.test/v1", "http://example.test/v1/systemone"])
def test_native_decide_uses_existing_transport_once_without_generate(base, monkeypatch):
    captured = []

    def post(request):
        captured.append(request)
        return response()

    monkeypatch.setattr(adapter, "post_json", post)
    backend = adapter.TypesafeDecisionBackend(BackendOptions(base, "private-key", "jev-test"))
    deadline = time.monotonic() + 2
    request = DecisionRequest(binding(), {"fact": "材料"}, questions())
    result = backend.decide(request, deadline=deadline)
    assert len(captured) == 1 and not hasattr(backend, "generate")
    wire = captured[0]
    assert wire.url == "http://example.test/v1/systemone"
    assert wire.deadline == deadline and wire.max_retries == 0 and not wire.allow_redirects
    assert wire.timeout <= 2 and wire.connect_timeout <= 2 and wire.max_response_bytes > 0
    assert set(wire.payload) == {"model", "state", "questions"}
    assert wire.headers["Authorization"] == "Bearer private-key"
    assert result.input_digest == request.input_digest and result.model == "jev-resolved-test"


def test_host_session_headers_reused_and_input_cannot_replace_auth(monkeypatch):
    captured = []

    def post(request):
        captured.append(request)
        return response()

    monkeypatch.setattr(adapter, "post_json", post)
    backend = adapter.TypesafeDecisionBackend(BackendOptions("https://example.test", "secret", "jev-test",
        custom_headers={"X-Client": "test"}, session_header="x-session"))
    request = DecisionRequest(binding(), {"Authorization": "attacker"}, questions())
    host = SimpleNamespace(home_paths=SimpleNamespace(owner_provider="local", owner_kind="user", owner_id="a"))
    with provider_runtime_scope(host, SimpleNamespace(thread_id="thread-1")):
        backend.decide(request, deadline=time.monotonic() + 2)
        backend.decide(request, deadline=time.monotonic() + 2)
    assert captured[0].headers["x-session"] == captured[1].headers["x-session"]
    assert captured[0].headers["Authorization"] == "Bearer secret"
    assert captured[0].headers["X-Client"] == "test"


@pytest.mark.parametrize("deadline", [True, float("nan"), float("inf"), 0,
                                      pytest.param(10 ** 1000, id="oversized-integer")])
def test_invalid_or_expired_deadline_never_sends(deadline, monkeypatch):
    def post(request):
        pytest.fail("不得发送")

    monkeypatch.setattr(adapter, "post_json", post)
    backend = adapter.TypesafeDecisionBackend(BackendOptions("https://example.test", "key", "jev-test"))
    with pytest.raises((DecisionInputError, ProviderTimeoutError)):
        backend.decide(DecisionRequest(binding(), "材料", questions()), deadline=deadline)


@pytest.mark.parametrize("phase", ["response", "parse"])
def test_late_network_or_parse_result_is_not_returned(phase, monkeypatch):
    now = [100.0]
    original = adapter.parse_typesafe_response

    def post(request):
        if phase == "response":
            now[0] = 103.0
        return response()

    def parse(*args):
        result = original(*args)
        now[0] = 103.0
        return result

    monkeypatch.setattr(limits.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(adapter, "post_json", post)
    monkeypatch.setattr(adapter, "parse_typesafe_response", parse)
    backend = adapter.TypesafeDecisionBackend(BackendOptions("https://example.test", "key", "jev-test"))
    with pytest.raises(ProviderTimeoutError):
        backend.decide(DecisionRequest(binding(), {}, questions()), deadline=102.0)


def test_cancellation_propagates_without_retry(monkeypatch):
    calls = []

    def post(request):
        calls.append(request)
        raise InterruptedError("用户取消")

    monkeypatch.setattr(adapter, "post_json", post)
    backend = adapter.TypesafeDecisionBackend(BackendOptions("https://example.test", "key", "jev-test"))
    with pytest.raises(InterruptedError):
        backend.decide(DecisionRequest(binding(), {}, questions()), deadline=time.monotonic() + 2)
    assert len(calls) == 1


def test_native_adapter_over_real_local_http_records_exact_request_and_response():
    captured = []

    # LLM: 此服务器只服务本地自有协议样本，不访问凭据或外部网络；finally 关闭并回收线程。
    # 类用途: 接收原 HTTP 传输发出的真实字节，验证原生适配器没有走聊天协议。
    class Handler(BaseHTTPRequestHandler):
        # LLM: 保留实际请求体和路径供断言，返回固定测试响应，不执行请求中的任何内容。
        # 函数用途: 为一次 Jev 协议组合测试提供本地成功响应。
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append((self.path, body, self.headers["Authorization"]))
            encoded = json.dumps(response()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        # LLM: 测试服务不记录认证头或请求正文，避免虚构秘密也进入测试日志。
        # 函数用途: 静默本地 HTTP 日志，事实只进入内存断言。
        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        backend = adapter.TypesafeDecisionBackend(BackendOptions(
            f"http://127.0.0.1:{server.server_port}/v1", "local-test-key", "jev-test"))
        request = DecisionRequest(binding(), {"input": "中文原始材料"}, questions())
        result = backend.decide(request, deadline=time.monotonic() + 2)
        assert captured == [("/v1/systemone", request.payload("jev-test"), "Bearer local-test-key")]
        assert result.usage == {"input_tokens": 120, "output_tokens": 30}
        assert result.answers[0].value == "model-a"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert not worker.is_alive()
