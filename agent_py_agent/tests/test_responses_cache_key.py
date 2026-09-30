"""Responses 请求体的会话级缓存键：绑定宿主会话时带 prompt_cache_key，未绑定时不带；同线程稳定、跨线程不同、不含凭据。"""
import json

from agent_py_agent.agent.backends import BackendOptions
from agent_py_agent.agent.backends.base import ProviderRequestOptions
from agent_py_agent.agent.backends.provider_headers import (
    current_provider_session,
    provider_session_scope,
)
from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend


def _non_stream_payload(monkeypatch, backend):
    sent = []
    monkeypatch.setattr(backend, "request_json", lambda path, payload, headers: sent.append(payload) or {
        "status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "好"}]}]})
    backend.generate("你好", request_options=ProviderRequestOptions())
    return sent[0]


def _stream_payload(monkeypatch, backend):
    sent = []

    class _Lines:
        def __init__(self):
            self.events = [json.dumps(event) for event in (
                {"type": "response.created", "response": {"status": "in_progress"}},
                {"type": "response.output_text.delta", "delta": "好"},
                {"type": "response.output_item.done", "item": {"type": "message", "role": "assistant",
                                                              "status": "completed",
                                                              "content": [{"type": "output_text", "text": "好"}]}},
                {"type": "response.completed", "response": {"status": "completed", "output": [],
                                                            "usage": {"input_tokens": 5, "output_tokens": 3}}},
            )]

        def __iter__(self):
            return iter(self.events)

        def close(self):
            pass

    monkeypatch.setattr(backend, "request_stream_iter", lambda path, payload, headers, first_event_timeout_seconds=None: sent.append(payload) or _Lines())
    backend.generate("你好", request_options=ProviderRequestOptions())
    return sent[0]


def test_payload_carries_cache_key_inside_session_scope(monkeypatch):
    backend = OpenAIResponsesBackend(BackendOptions("https://example.test/v1", "key", "model", stream_enabled=False))
    with provider_session_scope(("local", "main"), "thread-1"):
        expected = current_provider_session()
        payload = _non_stream_payload(monkeypatch, backend)
    assert payload["prompt_cache_key"] == expected


def test_payload_omits_cache_key_outside_session_scope(monkeypatch):
    backend = OpenAIResponsesBackend(BackendOptions("https://example.test/v1", "key", "model", stream_enabled=False))
    assert current_provider_session() == ""
    payload = _non_stream_payload(monkeypatch, backend)
    assert "prompt_cache_key" not in payload


def test_same_thread_reuses_the_same_key(monkeypatch):
    backend = OpenAIResponsesBackend(BackendOptions("https://example.test/v1", "key", "model", stream_enabled=False))
    with provider_session_scope(("local", "main"), "thread-1"):
        first = _non_stream_payload(monkeypatch, backend)["prompt_cache_key"]
        second = _non_stream_payload(monkeypatch, backend)["prompt_cache_key"]
    assert first == second


def test_different_threads_get_different_keys(monkeypatch):
    backend = OpenAIResponsesBackend(BackendOptions("https://example.test/v1", "key", "model", stream_enabled=False))
    with provider_session_scope(("local", "main"), "thread-a"):
        key_a = _non_stream_payload(monkeypatch, backend)["prompt_cache_key"]
    with provider_session_scope(("local", "main"), "thread-b"):
        key_b = _non_stream_payload(monkeypatch, backend)["prompt_cache_key"]
    assert key_a != key_b


def test_cache_key_contains_no_credentials(monkeypatch):
    backend = OpenAIResponsesBackend(BackendOptions("https://example.test/v1", "sk-secret-key", "model", stream_enabled=False))
    with provider_session_scope(("local", "main"), "thread-1"):
        key = _non_stream_payload(monkeypatch, backend)["prompt_cache_key"]
    assert "api_key" not in key and "token" not in key and "sk-" not in key
    assert "sk-secret-key" not in key


def test_subscription_mode_payload_carries_the_cache_key(monkeypatch):
    # 订阅登录走同一 Responses 组包：auth mode=chatgpt 时改 instructions/去 max_output_tokens，缓存键照常写入。
    backend = OpenAIResponsesBackend(BackendOptions("https://chatgpt.example.test/backend-api/codex", "", "m", stream_enabled=True))
    backend.auth_ref = {"mode": "chatgpt"}
    with provider_session_scope(("local", "main"), "thread-1"):
        expected = current_provider_session()
        payload = _stream_payload(monkeypatch, backend)
    assert payload["prompt_cache_key"] == expected
    assert "max_output_tokens" not in payload
