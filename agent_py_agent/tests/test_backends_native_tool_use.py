from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends.base import (
    AnthropicCompatibleBackend,
    BackendOptions,
)
from agent_py_agent.agent.backends.errors import ProviderResponseError
from agent_py_agent.agent.backends.stream_parsers import anthropic_stream_events
from agent_py_agent.agent.backends.usage_metadata import (
    collect_anthropic_stream,
    collect_anthropic_stream_with_completion,
    collect_anthropic_stream_with_tools,
)

_DEFAULT_OPTIONS = BackendOptions(
    api_base="https://api.example.com",
    api_key="key",
    model_name="claude-3",
    request_timeout=60,
    max_tokens=1024,
    temperature=0.2,
    stream_enabled=True,
)

_TOOLS = [
    {
        "name": "read_file",
        "description": "读取文件",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
]


def _options(**overrides) -> BackendOptions:
    return replace(_DEFAULT_OPTIONS, **overrides)


# --- non-stream tool_use parsing -------------------------------------------


def test_non_stream_extracts_tool_use_blocks():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=False))
    captured: dict[str, object] = {}

    def fake_request_json(path, payload, headers):
        captured["payload"] = payload
        return {
            "content": [
                {"type": "text", "text": "let me read it"},
                {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "README.md"}},
            ],
            "usage": {"input_tokens": 5, "output_tokens": 9},
        }

    backend.request_json = fake_request_json
    resp = backend.generate("read README", tools=_TOOLS)

    assert resp.text == "let me read it"
    assert resp.tool_use_blocks == [
        {"id": "toolu_1", "name": "read_file", "input": {"path": "README.md"}}
    ]
    # tools must be forwarded into the payload
    assert captured["payload"]["tools"] == _TOOLS


def test_non_stream_preserves_ordered_thinking_text_and_tool_blocks_for_replay():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=False))
    backend.request_json = lambda path, payload, headers: {
        "content": [
            {
                "type": "thinking",
                "thinking": "先检查文件",
                "signature": "sig-1",
                "output_only": "drop-me",
            },
            {"type": "text", "text": "我先读取。", "citations": None},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "read_file",
                "input": {"path": "README.md"},
                "caller": {"type": "direct"},
            },
        ],
        "stop_reason": "tool_use",
    }

    response = backend.generate("read README", tools=_TOOLS)

    assert response.text == "我先读取。"
    assert response.assistant_content_blocks == [
        {
            "type": "thinking",
            "thinking": "先检查文件",
            "signature": "sig-1",
        },
        {"type": "text", "text": "我先读取。"},
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "read_file",
            "input": {"path": "README.md"},
        },
    ]


def test_non_stream_tool_use_with_empty_text_does_not_raise():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=False))
    backend.request_json = lambda path, payload, headers: {
        "content": [
            {"type": "tool_use", "id": "toolu_2", "name": "read_file", "input": {"path": "a.txt"}},
        ],
    }

    resp = backend.generate("go", tools=_TOOLS)

    assert resp.text == ""
    assert resp.tool_use_blocks[0]["name"] == "read_file"


def test_non_stream_no_text_and_no_tool_use_still_raises():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=False))
    backend.request_json = lambda path, payload, headers: {"content": []}

    with pytest.raises(ProviderResponseError) as exc_info:
        backend.generate("go", tools=_TOOLS)
    assert exc_info.value.error_code == "MODEL_EMPTY_RESPONSE"


def test_non_stream_without_tools_omits_tools_key_and_keeps_blocks_empty():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=False))
    captured: dict[str, object] = {}

    def fake_request_json(path, payload, headers):
        captured["payload"] = payload
        return {"content": [{"type": "text", "text": "hi"}]}

    backend.request_json = fake_request_json
    resp = backend.generate("hi")

    assert resp.text == "hi"
    assert resp.tool_use_blocks == []
    assert "tools" not in captured["payload"]


def test_non_stream_malformed_input_falls_back_to_empty_dict():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=False))
    backend.request_json = lambda path, payload, headers: {
        "content": [{"type": "tool_use", "id": "t", "name": "read_file", "input": "not-a-dict"}]
    }

    resp = backend.generate("go", tools=_TOOLS)

    assert resp.tool_use_blocks == [{"id": "t", "name": "read_file", "input": {}}]


# --- streaming input_json_delta accumulation -------------------------------


def _tool_use_sse_lines() -> list[str]:
    return [
        json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 7, "output_tokens": 0}}}),
        json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok "}}),
        json.dumps({"type": "content_block_stop", "index": 0}),
        json.dumps(
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "toolu_9", "name": "read_file", "input": {}},
            }
        ),
        json.dumps({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}}),
        json.dumps({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ' "READ'}}),
        json.dumps({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": 'ME.md"}'}}),
        json.dumps({"type": "content_block_stop", "index": 1}),
        json.dumps({"type": "message_delta", "usage": {"output_tokens": 4}}),
        json.dumps({"type": "message_stop"}),
    ]


def _thinking_tool_sse_lines() -> list[str]:
    return [
        json.dumps(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            }
        ),
        json.dumps(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "先读"},
            }
        ),
        json.dumps(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "sig-stream"},
            }
        ),
        json.dumps({"type": "content_block_stop", "index": 0}),
        json.dumps(
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            }
        ),
        json.dumps(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "开始"},
            }
        ),
        json.dumps({"type": "content_block_stop", "index": 1}),
        json.dumps(
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_stream",
                    "name": "read_file",
                    "input": {},
                },
            }
        ),
        json.dumps(
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"path":"README.md"}',
                },
            }
        ),
        json.dumps({"type": "content_block_stop", "index": 2}),
        json.dumps(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 9},
            }
        ),
        json.dumps({"type": "message_stop"}),
    ]


def test_stream_events_accumulate_input_json_delta_into_block():
    blocks = [
        e.tool_use_block
        for e in anthropic_stream_events(_tool_use_sse_lines())
        if e.tool_use_block is not None
    ]

    assert blocks == [{"id": "toolu_9", "name": "read_file", "input": {"path": "README.md"}}]


def test_collect_with_tools_returns_text_usage_and_blocks():
    text, usage, blocks = collect_anthropic_stream_with_tools(_tool_use_sse_lines())

    assert text == "ok "
    assert usage == {"input_tokens": 7, "output_tokens": 4}
    assert blocks == [{"id": "toolu_9", "name": "read_file", "input": {"path": "README.md"}}]


def test_stream_completion_preserves_thinking_text_tool_order_without_exposing_thinking():
    chunks: list[str] = []

    text, _usage, blocks, completion = collect_anthropic_stream_with_completion(
        _thinking_tool_sse_lines(),
        chunks.append,
    )

    assert text == "开始"
    assert chunks == ["开始"]
    assert blocks == [
        {"id": "toolu_stream", "name": "read_file", "input": {"path": "README.md"}}
    ]
    assert list(completion.assistant_content_blocks) == [
        {"type": "thinking", "thinking": "先读", "signature": "sig-stream"},
        {"type": "text", "text": "开始"},
        {
            "type": "tool_use",
            "id": "toolu_stream",
            "name": "read_file",
            "input": {"path": "README.md"},
        },
    ]


def test_legacy_collect_anthropic_stream_ignores_tool_use_blocks():
    # The text-protocol collector must keep its old 2-tuple contract.
    text, usage = collect_anthropic_stream(_tool_use_sse_lines())

    assert text == "ok "
    assert usage == {"input_tokens": 7, "output_tokens": 4}


def test_stream_generate_returns_tool_use_blocks():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=True))
    captured: dict[str, object] = {}

    def request_stream(path, payload, headers):
        captured["payload"] = payload
        return _tool_use_sse_lines()

    backend.request_stream = request_stream
    resp = backend.generate("read README", tools=_TOOLS)

    assert resp.text == "ok "
    assert resp.tool_use_blocks == [
        {"id": "toolu_9", "name": "read_file", "input": {"path": "README.md"}}
    ]
    assert captured["payload"]["tools"] == _TOOLS


def test_stream_generate_carries_internal_blocks_separately_from_visible_text():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=True))
    backend.request_stream = lambda path, payload, headers: _thinking_tool_sse_lines()

    response = backend.generate("read README", tools=_TOOLS)

    assert response.text == "开始"
    assert response.assistant_content_blocks[0] == {
        "type": "thinking",
        "thinking": "先读",
        "signature": "sig-stream",
    }


def test_stream_tool_use_only_with_empty_text_does_not_raise():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=True))
    lines = [
        json.dumps(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "t0", "name": "read_file", "input": {}},
            }
        ),
        json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path":"x"}'}}),
        json.dumps({"type": "content_block_stop", "index": 0}),
        json.dumps({"type": "message_stop"}),
    ]
    backend.request_stream = lambda path, payload, headers: lines

    resp = backend.generate("go", tools=_TOOLS)

    assert resp.text == ""
    assert resp.tool_use_blocks == [{"id": "t0", "name": "read_file", "input": {"path": "x"}}]


def test_stream_streaming_iter_path_accumulates_blocks_and_streams_text():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=True))
    chunks: list[str] = []

    def request_stream_iter(path, payload, headers):
        yield from _tool_use_sse_lines()

    backend.request_stream_iter = request_stream_iter
    resp = backend.generate("read README", on_chunk=chunks.append, tools=_TOOLS)

    assert chunks == ["ok "]
    assert resp.tool_use_blocks == [
        {"id": "toolu_9", "name": "read_file", "input": {"path": "README.md"}}
    ]
