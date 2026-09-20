"""Provider-declared incomplete turns must never become successful replies."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends.anthropic import AnthropicCompatibleBackend
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.errors import ProviderResponseError
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend

_OPTIONS = BackendOptions(
    api_base="https://api.example.com",
    api_key="key",
    model_name="test",
    request_timeout=60,
    max_tokens=64,
    temperature=0.2,
    stream_enabled=True,
)


def _sse(events: list[dict]) -> list[str]:
    return [json.dumps(event, ensure_ascii=False) for event in events]


def _anthropic_text_stream(text: str, stop_reason: str) -> list[str]:
    return _sse(
        [
            {"type": "content_block_delta", "delta": {"text": text}},
            {"type": "message_delta", "delta": {"stop_reason": stop_reason}},
            {"type": "message_stop"},
        ]
    )


def test_anthropic_stream_max_tokens_returns_truncated_response() -> None:
    # 长任务真机(2026-08-16): stop_reason=max_tokens 不再 raise 致命错误终止 run;
    # 保留已收文本、丢弃工具块、标记 truncated, 工具循环下一轮继续(harness 同款)。
    backend = AnthropicCompatibleBackend(_OPTIONS)
    calls: list[dict] = []

    def fake_stream(path: str, payload: dict, headers: dict) -> list[str]:
        del path, headers
        calls.append(payload)
        return _anthropic_text_stream("半截产出", "max_tokens")

    backend.request_stream = fake_stream  # type: ignore[method-assign]

    response = backend.generate("prompt")

    assert response.text == "半截产出"
    assert response.truncated is True
    assert response.stop_reason == "max_tokens"
    assert response.tool_use_blocks == []
    assert len(calls) == 1


def test_anthropic_end_turn_remains_a_normal_response() -> None:
    backend = AnthropicCompatibleBackend(_OPTIONS)
    backend.request_stream = (  # type: ignore[method-assign]
        lambda path, payload, headers: _anthropic_text_stream("完整答案", "end_turn")
    )

    response = backend.generate("prompt")

    assert response.text == "完整答案"
    assert response.stop_reason == "end_turn"


def test_anthropic_incomplete_tool_use_is_not_executed_as_a_valid_turn() -> None:
    lines = _sse(
        [
            {"type": "content_block_delta", "delta": {"text": "先说两句"}},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "t1", "name": "write_file"},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"pa'},
            },
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
            {"type": "message_stop"},
        ]
    )
    backend = AnthropicCompatibleBackend(_OPTIONS)
    backend.request_stream = lambda path, payload, headers: lines  # type: ignore[method-assign]

    response = backend.generate("prompt", tools=[{"name": "write_file"}])

    assert response.truncated is True
    assert response.stop_reason == "max_tokens"
    assert response.tool_use_blocks == [], "半截的 tool_use 块不得当作有效调用执行"
    assert response.text == "先说两句"


def test_anthropic_tool_use_stop_reason_remains_valid() -> None:
    lines = _sse(
        [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "t1", "name": "read_file"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":"a"}'},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ]
    )
    backend = AnthropicCompatibleBackend(_OPTIONS)
    backend.request_stream = lambda path, payload, headers: lines  # type: ignore[method-assign]

    response = backend.generate("prompt", tools=[{"name": "read_file"}])

    assert response.tool_use_blocks == [
        {"id": "t1", "name": "read_file", "input": {"path": "a"}}
    ]
    assert response.stop_reason == "tool_use"


def test_anthropic_non_stream_max_tokens_returns_truncated_response() -> None:
    backend = AnthropicCompatibleBackend(replace(_OPTIONS, stream_enabled=False))
    backend.request_json = lambda path, payload, headers: {  # type: ignore[method-assign]
        "content": [{"type": "text", "text": "答案"}],
        "stop_reason": "max_tokens",
        "usage": {"output_tokens": 64},
    }

    response = backend.generate("prompt")

    assert response.text == "答案"
    assert response.truncated is True
    assert response.stop_reason == "max_tokens"


def test_openai_stream_length_returns_truncated_response() -> None:
    backend = OpenAICompatibleBackend(_OPTIONS)
    backend.request_stream = lambda path, payload, headers: [  # type: ignore[method-assign]
        json.dumps({"choices": [{"delta": {"content": "partial"}}]}),
        json.dumps({"choices": [{"delta": {}, "finish_reason": "length"}]}),
        "[DONE]",
    ]

    response = backend.generate("prompt")

    assert response.text == "partial"
    assert response.truncated is True
    assert response.stop_reason == "length"
    assert response.tool_use_blocks == []


def test_openai_non_stream_length_returns_truncated_response() -> None:
    backend = OpenAICompatibleBackend(replace(_OPTIONS, stream_enabled=False))
    backend.request_json = lambda path, payload, headers: {  # type: ignore[method-assign]
        "choices": [
            {
                "message": {"content": "partial"},
                "finish_reason": "length",
            }
        ]
    }

    response = backend.generate("prompt")

    assert response.text == "partial"
    assert response.truncated is True
    assert response.stop_reason == "length"
