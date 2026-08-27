from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends import anthropic_prompt_cache
from agent_py_agent.agent.backends.base import (
    AnthropicCompatibleBackend,
    BackendOptions,
    ProviderRequestOptions,
)
from agent_py_agent.agent.backends.errors import ProviderResponseError
from agent_py_agent.agent.backends.stream_parsers import anthropic_stream_events
from agent_py_agent.agent.backends.usage_metadata import (
    collect_anthropic_stream,
    collect_anthropic_stream_with_completion,
    collect_anthropic_stream_with_tools,
)
from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt

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


def _count_cache_markers(messages: list[dict[str, object]]) -> int:
    count = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        count += sum(
            isinstance(block, dict) and "cache_control" in block for block in content
        )
    return count


# --- non-stream tool_use parsing -------------------------------------------


def test_non_stream_extracts_tool_use_blocks():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=False))
    captured: dict[str, object] = {}

    def fake_request_json(path, payload, headers):
        captured["payload"] = payload
        return {
            "content": [
                {"type": "text", "text": "let me read it"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "read_file",
                    "input": {"path": "README.md"},
                },
            ],
            "usage": {"input_tokens": 5, "output_tokens": 9},
        }

    backend.request_json = fake_request_json
    resp = backend.generate("read README", tools=_TOOLS)

    assert resp.text == "let me read it"
    assert resp.tool_use_blocks == [
        {"id": "toolu_1", "name": "read_file", "input": {"path": "README.md"}}
    ]
    # 没有 native messages 的单次请求保持旧形态，不额外创建主动缓存。
    assert captured["payload"]["tools"] == _TOOLS


def test_native_prompt_cache_marks_prompt_and_latest_history_without_mutating_inputs():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=False))
    captured: dict[str, object] = {}
    tools = deepcopy(_TOOLS)
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "inspect", "signature": "sig"},
                {"type": "text", "text": "reading"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "read_file",
                    "input": {"path": "README.md"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "done",
                    "is_error": False,
                }
            ],
        },
    ]
    original_messages = deepcopy(messages)
    original_tools = deepcopy(tools)

    def fake_request_json(path, payload, headers):
        del path, headers
        captured["payload"] = payload
        return {"content": [{"type": "text", "text": "continue"}]}

    backend.request_json = fake_request_json
    backend.generate(
        "root prompt",
        tools=tools,
        messages=messages,
        request_options=ProviderRequestOptions(
            system_instruction="host authorization policy",
            thinking_disabled=True,
        ),
    )

    payload = captured["payload"]
    assert payload["system"] == "host authorization policy"
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert payload["messages"][0] == {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": "root prompt",
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }
    assert payload["messages"][-1]["content"][-1]["cache_control"] == {
        "type": "ephemeral"
    }
    assert "cache_control" not in payload["messages"][1]["content"][-1]
    assert messages == original_messages
    assert tools == original_tools


def test_native_prompt_cache_uses_only_typed_stable_prefix_for_split_prompt():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=False))
    captured: dict[str, object] = {}
    messages = [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "prior assistant"}],
        },
        {
            "role": "user",
            "content": [{"type": "text", "text": "volatile runtime guidance"}],
        },
    ]
    original_messages = deepcopy(messages)

    def fake_request_json(path, payload, headers):
        del path, headers
        captured["payload"] = payload
        return {"content": [{"type": "text", "text": "continue"}]}

    backend.request_json = fake_request_json
    backend.generate(
        CacheStructuredPrompt(
            "stable instructions",
            "changing conversation tail",
            stable_user_prefix="stable task snapshot",
        ),
        tools=_TOOLS,
        messages=messages,
        request_options=ProviderRequestOptions(
            system_instruction="host authorization policy",
        ),
    )

    payload = captured["payload"]
    assert payload["system"] == [
        {"type": "text", "text": "host authorization policy"},
        {
            "type": "text",
            "text": "stable instructions",
            "cache_control": {"type": "ephemeral"},
        },
    ]
    assert payload["messages"][0] == {
        "role": "user",
        "content": [{"type": "text", "text": "stable task snapshot"}],
    }
    assert payload["messages"][1] == messages[0]
    assert payload["messages"][2] == {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": "volatile runtime guidance",
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": "changing conversation tail"},
        ],
    }
    assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert messages == original_messages


def test_native_prompt_cache_marks_stable_user_snapshot_before_first_volatile_tail():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=False))
    captured: dict[str, object] = {}

    def fake_request_json(path, payload, headers):
        del path, headers
        captured["payload"] = payload
        return {"content": [{"type": "text", "text": "continue"}]}

    backend.request_json = fake_request_json
    backend.generate(
        CacheStructuredPrompt(
            "stable instructions",
            "first execution facts",
            stable_user_prefix="stable task snapshot",
        ),
        tools=_TOOLS,
        messages=[],
        request_options=ProviderRequestOptions(
            system_instruction="host authorization policy",
        ),
    )

    assert captured["payload"]["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "stable task snapshot",
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": "first execution facts"},
            ],
        }
    ]


def test_native_canonical_user_is_sent_once_through_structured_messages():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=False))
    captured: dict[str, object] = {}

    def fake_request_json(path, payload, headers):
        del path, headers
        captured["payload"] = payload
        return {"content": [{"type": "text", "text": "continue"}]}

    backend.request_json = fake_request_json
    canonical = "# User Task\nsecond task"
    backend.generate(
        CacheStructuredPrompt(
            "stable instructions",
            "changing conversation facts",
            canonical_user_turn=canonical,
        ),
        tools=_TOOLS,
        messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": canonical}],
            }
        ],
    )

    payload = captured["payload"]
    assert sum(
        block.get("text") == canonical
        for message in payload["messages"]
        for block in message.get("content", [])
    ) == 1
    assert payload["messages"][0]["content"] == [
        {
            "type": "text",
            "text": canonical,
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "changing conversation facts"},
    ]


def test_typed_prompt_projection_never_drops_stable_user_text_without_system_split():
    prompt = CacheStructuredPrompt(
        "stable instructions",
        "changing execution facts",
        stable_user_prefix="stable task snapshot",
    )

    projected = anthropic_prompt_cache._prompt_prefixed_messages(prompt, [])

    assert [block["text"] for block in projected[0]["content"]] == [
        "stable instructions",
        "stable task snapshot",
        "changing execution facts",
    ]


def test_native_prompt_cache_advances_one_message_marker_after_append_only_history():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=False))
    payloads: list[dict[str, object]] = []

    def fake_request_json(path, payload, headers):
        del path, headers
        payloads.append(payload)
        return {"content": [{"type": "text", "text": "continue"}]}

    backend.request_json = fake_request_json
    backend.generate(
        CacheStructuredPrompt(
            "stable instructions",
            "facts one",
            stable_user_prefix="stable task snapshot",
        ),
        tools=_TOOLS,
        messages=[],
    )
    history = [
        {"role": "assistant", "content": [{"type": "text", "text": "plan"}]},
        {"role": "user", "content": [{"type": "text", "text": "tool result"}]},
    ]
    original_history = deepcopy(history)
    backend.generate(
        CacheStructuredPrompt(
            "stable instructions",
            "facts two",
            stable_user_prefix="stable task snapshot",
        ),
        tools=_TOOLS,
        messages=history,
    )

    first_messages = payloads[0]["messages"]
    second_messages = payloads[1]["messages"]
    assert first_messages[0]["content"][0] == {
        "type": "text",
        "text": "stable task snapshot",
        "cache_control": {"type": "ephemeral"},
    }
    assert second_messages[0] == {
        "role": "user",
        "content": [{"type": "text", "text": "stable task snapshot"}],
    }
    assert second_messages[-1]["content"] == [
        {
            "type": "text",
            "text": "tool result",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "facts two"},
    ]
    assert _count_cache_markers(first_messages) == 1
    assert _count_cache_markers(second_messages) == 1
    assert payloads[0]["system"] == payloads[1]["system"]
    assert history == original_history


def test_native_prompt_cache_can_be_disabled_for_incompatible_endpoints():
    backend = AnthropicCompatibleBackend(
        replace(_options(stream_enabled=False), prompt_cache_enabled=False),
    )
    captured: dict[str, object] = {}

    def fake_request_json(path, payload, headers):
        del path, headers
        captured["payload"] = payload
        return {"content": [{"type": "text", "text": "ok"}]}

    backend.request_json = fake_request_json
    backend.generate("root prompt", tools=_TOOLS, messages=[])

    assert captured["payload"]["messages"] == [
        {"role": "user", "content": "root prompt"}
    ]
    assert captured["payload"]["tools"] == _TOOLS


def test_disabled_cache_keeps_structured_prompt_as_one_complete_user_message():
    backend = AnthropicCompatibleBackend(
        replace(_options(stream_enabled=False), prompt_cache_enabled=False),
    )
    captured: dict[str, object] = {}

    def fake_request_json(path, payload, headers):
        del path, headers
        captured["payload"] = payload
        return {"content": [{"type": "text", "text": "ok"}]}

    backend.request_json = fake_request_json
    backend.generate(
        CacheStructuredPrompt(
            "stable instructions",
            "changing conversation tail",
            stable_user_prefix="stable task snapshot",
        ),
        tools=_TOOLS,
        messages=[],
        request_options=ProviderRequestOptions(
            system_instruction="host authorization policy",
        ),
    )

    assert captured["payload"]["system"] == (
        "host authorization policy\n\nstable instructions"
    )
    assert captured["payload"]["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "stable task snapshot"},
                {"type": "text", "text": "changing conversation tail"},
            ],
        }
    ]
    assert captured["payload"]["tools"] == _TOOLS


def test_native_prompt_cache_marks_prompt_and_tools_on_empty_first_turn():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=False))
    captured: dict[str, object] = {}

    def fake_request_json(path, payload, headers):
        del path, headers
        captured["payload"] = payload
        return {"content": [{"type": "text", "text": "ok"}]}

    backend.request_json = fake_request_json
    backend.generate("root prompt", tools=_TOOLS, messages=[])

    assert captured["payload"]["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "root prompt",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    ]
    assert captured["payload"]["tools"][-1]["cache_control"] == {
        "type": "ephemeral"
    }


def test_native_prompt_cache_keeps_empty_first_request_shape():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=False))
    captured: dict[str, object] = {}

    def fake_request_json(path, payload, headers):
        del path, headers
        captured["payload"] = payload
        return {"content": [{"type": "text", "text": "ok"}]}

    backend.request_json = fake_request_json
    backend.generate("", messages=[])

    assert captured["payload"]["messages"] == [{"role": "user", "content": ""}]


def test_anthropic_structured_generation_uses_forced_schema_tool_and_non_stream_transport():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=True))
    captured: dict[str, object] = {}

    def fake_request_json(path, payload, headers):
        captured["payload"] = payload
        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_structured",
                    "name": "my_agent_structured_output",
                    "input": {"requires_action": False, "actions": []},
                }
            ],
            "stop_reason": "tool_use",
        }

    backend.request_json = fake_request_json
    backend.request_stream = lambda *args, **kwargs: pytest.fail(
        "structured output must use the deterministic non-stream transport"
    )
    schema = {
        "type": "object",
        "properties": {
            "requires_action": {"type": "boolean"},
            "actions": {"type": "array"},
        },
        "required": ["requires_action", "actions"],
        "additionalProperties": False,
    }
    response = backend.generate_structured("classify", response_schema=schema)

    payload = captured["payload"]
    assert payload["tool_choice"] == {
        "type": "tool",
        "name": "my_agent_structured_output",
    }
    assert payload["tools"][0]["input_schema"] == schema
    assert payload["temperature"] == 0.0
    assert payload["messages"][0]["content"].startswith(
        "MANDATORY OUTPUT CONTRACT: Call the provided my_agent_structured_output tool exactly once."
    )
    assert json.loads(response.text) == {"requires_action": False, "actions": []}


def test_anthropic_structured_generation_retries_forced_channel_without_parsing_prose():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=True))
    responses = iter(
        [
            {
                "content": [
                    {
                        "type": "text",
                        "text": '{"requires_action": false, "actions": []}',
                    }
                ],
                "stop_reason": "end_turn",
            },
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_structured_retry",
                        "name": "my_agent_structured_output",
                        "input": {"requires_action": False, "actions": []},
                    }
                ],
                "stop_reason": "tool_use",
            },
        ]
    )
    prompts: list[str] = []

    def fake_request_json(path, payload, headers):
        prompts.append(payload["messages"][0]["content"])
        return next(responses)

    backend.request_json = fake_request_json
    response = backend.generate_structured(
        "classify",
        response_schema={
            "type": "object",
            "properties": {
                "requires_action": {"type": "boolean"},
                "actions": {"type": "array"},
            },
            "required": ["requires_action", "actions"],
        },
    )

    assert json.loads(response.text) == {"requires_action": False, "actions": []}
    assert len(prompts) == 2
    assert "ignored the mandatory structured channel" in prompts[1]


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
        json.dumps(
            {"type": "message_start", "message": {"usage": {"input_tokens": 7, "output_tokens": 0}}}
        ),
        json.dumps(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }
        ),
        json.dumps(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "ok "},
            }
        ),
        json.dumps({"type": "content_block_stop", "index": 0}),
        json.dumps(
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_9",
                    "name": "read_file",
                    "input": {},
                },
            }
        ),
        json.dumps(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
            }
        ),
        json.dumps(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": ' "READ'},
            }
        ),
        json.dumps(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": 'ME.md"}'},
            }
        ),
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


def test_large_tool_input_stream_emits_batched_counters_without_partial_json():
    content = "x" * 9_000
    partial_json = json.dumps({"content": content})
    lines = [
        json.dumps(
            {
                "type": "content_block_start",
                "index": 3,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_large",
                    "name": "write_file",
                    "input": {},
                },
            }
        ),
        json.dumps(
            {
                "type": "content_block_delta",
                "index": 3,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": partial_json,
                },
            }
        ),
        json.dumps({"type": "content_block_stop", "index": 3}),
        json.dumps({"type": "message_stop"}),
    ]
    progress: list[dict[str, object]] = []

    _text, _usage, blocks, _completion = collect_anthropic_stream_with_completion(
        lines,
        on_tool_input_progress=progress.append,
    )

    assert [item["phase"] for item in progress] == ["started", "streaming", "ready"]
    assert progress[-1]["received_chars"] == len(partial_json)
    assert all(item["tool"] == "write_file" for item in progress)
    public_text = json.dumps(progress, ensure_ascii=False)
    assert content not in public_text
    assert "partial_json" not in public_text
    assert blocks == [
        {
            "id": "toolu_large",
            "name": "write_file",
            "input": {"content": content},
        }
    ]


def test_stream_completion_preserves_thinking_text_tool_order_without_exposing_thinking():
    chunks: list[str] = []

    text, _usage, blocks, completion = collect_anthropic_stream_with_completion(
        _thinking_tool_sse_lines(),
        chunks.append,
    )

    assert text == "开始"
    assert chunks == ["开始"]
    assert blocks == [{"id": "toolu_stream", "name": "read_file", "input": {"path": "README.md"}}]
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


def test_stream_thinking_completion_precedes_first_text_callback():
    """thinking block stop 必须先于后续 text delta 到达展示层。"""
    events: list[tuple[str, str]] = []

    class ThinkingObserver:
        def __call__(self, text: str) -> None:
            events.append(("thinking_delta", text))

        def complete(self, text: str) -> None:
            events.append(("thinking_completed", text))

    collect_anthropic_stream_with_completion(
        _thinking_tool_sse_lines(),
        on_chunk=lambda text: events.append(("text", text)),
        on_thinking_delta=ThinkingObserver(),
    )

    assert events == [
        ("thinking_delta", "先读"),
        ("thinking_completed", "先读"),
        ("text", "开始"),
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


def test_stream_backend_forwards_tool_input_progress_callback():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=True))
    backend.request_stream = lambda path, payload, headers: _tool_use_sse_lines()
    progress: list[dict[str, object]] = []

    response = backend.generate(
        "read README",
        tools=_TOOLS,
        on_tool_input_progress=progress.append,
    )

    assert response.tool_use_blocks
    assert [item["phase"] for item in progress] == ["started", "ready"]


def test_stream_native_request_uses_the_same_prompt_cache_projection():
    backend = AnthropicCompatibleBackend(_options(stream_enabled=True))
    captured: dict[str, object] = {}

    def request_stream(path, payload, headers):
        del path, headers
        captured["payload"] = payload
        return _tool_use_sse_lines()

    backend.request_stream = request_stream
    backend.generate("read README", tools=_TOOLS, messages=[])

    assert captured["payload"]["tools"][-1]["cache_control"] == {
        "type": "ephemeral"
    }
    assert captured["payload"]["messages"][0]["content"][-1][
        "cache_control"
    ] == {"type": "ephemeral"}


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
        json.dumps(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":"x"}'},
            }
        ),
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
