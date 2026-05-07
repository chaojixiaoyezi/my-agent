"""后端适配器请求载荷约定检查。"""

import json
from dataclasses import replace

from agent_py_agent.agent.backend import (
    AnthropicCompatibleBackend,
    BackendOptions,
    OpenAICompatibleBackend,
)

_OPENAI_OPTIONS = BackendOptions(api_base="http://fake/v1", api_key="k", model_name="m")
_ANTHROPIC_OPTIONS = BackendOptions(api_base="http://fake/anthropic", api_key="k", model_name="m")


class FakeOpenAI(OpenAICompatibleBackend):
    def __init__(self, *, stream_enabled=False):
        super().__init__(replace(_OPENAI_OPTIONS, stream_enabled=stream_enabled))
        self.seen = None

    def request_json(self, path, payload, headers):
        self.seen = (path, payload, headers)
        return {"choices": [{"message": {"content": "openai ok"}}]}

    def request_stream(self, path, payload, headers):
        self.seen = (path, payload, headers)
        return [
            json.dumps({"choices": [{"delta": {"content": "openai "}}]}),
            json.dumps({"choices": [{"delta": {"content": "stream ok"}}]}),
            "[DONE]",
        ]


class FakeAnthropic(AnthropicCompatibleBackend):
    def __init__(self, *, stream_enabled=False):
        super().__init__(replace(_ANTHROPIC_OPTIONS, stream_enabled=stream_enabled))
        self.seen = None

    def request_json(self, path, payload, headers):
        self.seen = (path, payload, headers)
        return {"content": [{"type": "text", "text": "anthropic ok"}]}

    def request_stream(self, path, payload, headers):
        self.seen = (path, payload, headers)
        return [
            json.dumps({"type": "content_block_delta", "delta": {"text": "anthropic "}}),
            json.dumps({"type": "content_block_delta", "delta": {"text": "stream ok"}}),
            json.dumps({"type": "message_stop"}),
        ]


def test_openai_payload():
    backend = FakeOpenAI()
    response = backend.generate("hello")
    assert response.text == "openai ok"
    path, payload, headers = backend.seen
    assert path == "/chat/completions"
    assert payload["messages"][0]["content"] == "hello"
    assert headers["Authorization"] == "Bearer k"


def test_anthropic_payload():
    backend = FakeAnthropic()
    response = backend.generate("hello")
    assert response.text == "anthropic ok"
    path, payload, headers = backend.seen
    assert path == "/v1/messages"
    assert payload["messages"][0]["content"] == "hello"
    assert headers["Authorization"] == "Bearer k"
    assert "anthropic-version" in headers


def test_openai_streaming_parses_sse_chunks():
    backend = FakeOpenAI(stream_enabled=True)
    response = backend.generate("hello")
    assert response.text == "openai stream ok"
    path, payload, headers = backend.seen
    assert path == "/chat/completions"


def test_anthropic_streaming_parses_sse_events():
    backend = FakeAnthropic(stream_enabled=True)
    response = backend.generate("hello")
    assert response.text == "anthropic stream ok"
    path, payload, headers = backend.seen
    assert path == "/v1/messages"


def test_streaming_fallback_to_non_stream():
    """stream_enabled=False 时走 request_json 路径。"""
    backend = FakeOpenAI(stream_enabled=False)
    response = backend.generate("hello")
    assert response.text == "openai ok"


def test_streaming_skips_malformed_json():
    """流式解析跳过无法解析的 JSON 行。"""

    class PartialBroken(OpenAICompatibleBackend):
        def __init__(self):
            super().__init__(replace(_OPENAI_OPTIONS, stream_enabled=True))

        def request_stream(self, path, payload, headers):
            return [
                json.dumps({"choices": [{"delta": {"content": "ok "}}]}),
                "not-json",
                json.dumps({"choices": [{"delta": {"content": "fine"}}]}),
                "[DONE]",
            ]

    backend = PartialBroken()
    response = backend.generate("hello")
    assert response.text == "ok fine"
