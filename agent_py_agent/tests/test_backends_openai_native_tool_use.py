from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends.base import (
    BackendOptions,
    OpenAICompatibleBackend,
    ProviderRequestOptions,
)
from agent_py_agent.agent.backends.errors import ProviderResponseError
from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt

_OPTIONS = BackendOptions(
    api_base="https://api.example.com/v1",
    api_key="key",
    model_name="gpt-4.1",
    max_tokens=1024,
    stream_enabled=False,
)

_TOOLS = [
    {
        "name": "read_file",
        "description": "读取文件",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }
]


@pytest.mark.parametrize("endpoint,model,required", [
    ("https://api.deepseek.com/v1", "deepseek-v4-flash", True),
    ("https://opencode.ai/zen/go/v1", "deepseek-v4-flash", True),
    ("https://opencode.ai/zen/v1", "deepseek-v4-pro", True),
    ("https://opencode.ai/zen/go/v1", "minimax-m2.7", False),
    ("https://api.example.com/v1", "deepseek-v4-flash", False),
    ("https://api.deepseek.com.example.org/v1", "deepseek-v4-flash", False),
])
def test_known_reasoning_dialect_replays_legacy_history_without_mutating_it(endpoint, model, required):
    backend = OpenAICompatibleBackend(replace(_OPTIONS, api_base=endpoint, model_name=model))
    messages = [
        {"role": "assistant", "content": "旧文本答复"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "真实原始思考"},
            {"type": "tool_use", "id": "c", "name": "read_file", "input": {"path": "a"}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c", "content": "a"}]},
    ]
    before = json.dumps(messages, ensure_ascii=False)
    captured = {}

    def request_json(path, payload, headers):
        captured.update(payload)
        return {"choices": [{"message": {"content": "继续"}, "finish_reason": "stop"}]}

    backend.request_json = request_json
    backend.generate("继续", messages=messages, tools=_TOOLS)
    assistants = [m for m in captured["messages"] if m["role"] == "assistant"]
    assert ("reasoning_content" in assistants[0]) is required
    assert assistants[0].get("reasoning_content", "") == ""
    assert assistants[1]["reasoning_content"] == "真实原始思考"
    assert json.dumps(messages, ensure_ascii=False) == before


def test_openai_non_stream_extracts_native_tool_calls_and_translates_schema() -> None:
    backend = OpenAICompatibleBackend(_OPTIONS)
    captured = {}

    def request_json(path, payload, headers):
        captured["payload"] = payload
        return {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"README.md"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

    backend.request_json = request_json
    response = backend.generate("read it", tools=_TOOLS)

    assert response.text == ""
    assert response.tool_use_blocks == [
        {"id": "call_1", "name": "read_file", "input": {"path": "README.md"}}
    ]
    assert captured["payload"]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读取文件",
                "parameters": _TOOLS[0]["input_schema"],
            },
        }
    ]


def test_openai_native_history_translates_tool_calls_and_results() -> None:
    backend = OpenAICompatibleBackend(_OPTIONS)
    captured = {}
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "I should inspect the file."},
                {"type": "text", "text": "checking"},
                {
                    "type": "tool_use",
                    "id": "call_1",
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
                    "tool_use_id": "call_1",
                    "content": "file contents",
                    "is_error": False,
                }
            ],
        },
    ]

    def request_json(path, payload, headers):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}

    backend.request_json = request_json
    response = backend.generate(
        "original user prompt",
        tools=_TOOLS,
        messages=messages,
        request_options=ProviderRequestOptions(
            system_instruction="host authorization policy"
        ),
    )

    assert response.text == "done"
    sent = captured["payload"]["messages"]
    assert sent[0] == {"role": "system", "content": "host authorization policy"}
    assert sent[1] == {"role": "user", "content": "original user prompt"}
    assert sent[2]["reasoning_content"] == "I should inspect the file."
    assert sent[2]["tool_calls"][0]["function"]["arguments"] == '{"path": "README.md"}'
    assert sent[3] == {"role": "tool", "tool_call_id": "call_1", "content": "file contents"}


@pytest.mark.parametrize("reasoning", ["本轮已确认文件位置。", ""])
def test_openai_replays_reasoning_on_final_before_followup_with_tools(reasoning) -> None:
    backend = OpenAICompatibleBackend(_OPTIONS)
    captured = {}
    messages = [{"role": "assistant", "content": [
        {"type": "thinking", "thinking": reasoning},
        {"type": "text", "text": "已定位，接下来继续。"},
    ]}, {"role": "user", "content": [{"type": "text", "text": "现在进展如何？"}]}]

    def request_json(path, payload, headers):
        captured.update(payload)
        return {"choices": [{"message": {"content": "继续"}, "finish_reason": "stop"}]}

    backend.request_json = request_json
    backend.generate("原任务", messages=messages, tools=_TOOLS)
    sent = captured["messages"][1]
    assert sent == {"role": "assistant", "content": "已定位，接下来继续。",
                    "reasoning_content": reasoning}
    assert messages[0]["content"][0]["thinking"] == reasoning


def test_openai_plain_assistant_does_not_invent_reasoning_metadata() -> None:
    from agent_py_agent.agent.backends.base import _openai_assistant_messages

    assert _openai_assistant_messages([{"type": "text", "text": "hello"}]) == [
        {"role": "assistant", "content": "hello"}
    ]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reasoning", [None, "", "已经确认。"])
def test_openai_response_reasoning_presence_survives_native_replay(stream, reasoning):
    from agent_py_agent.agent.backends.base import _openai_assistant_messages

    backend = OpenAICompatibleBackend(replace(_OPTIONS, stream_enabled=stream))
    message = {"content": "已完成这一步。"}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if stream:
        backend.request_stream = lambda *_: [
            json.dumps({"choices": [{"delta": message, "finish_reason": "stop"}]}), "[DONE]",
        ]
    else:
        backend.request_json = lambda *_: {
            "choices": [{"message": message, "finish_reason": "stop"}]
        }
    response = backend.generate("任务", tools=_TOOLS)
    replay = _openai_assistant_messages(response.assistant_content_blocks)[0]
    assert response.text == message["content"]
    assert ("reasoning_content" in replay) is (reasoning is not None)
    if reasoning is not None:
        assert replay["reasoning_content"] == reasoning


def test_openai_empty_native_history_keeps_system_before_original_user_prompt() -> None:
    backend = OpenAICompatibleBackend(_OPTIONS)
    captured = {}

    def request_json(path, payload, headers):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}

    backend.request_json = request_json
    backend.generate(
        "original user prompt",
        messages=[],
        request_options=ProviderRequestOptions(
            system_instruction="host authorization policy"
        ),
    )

    assert captured["payload"]["messages"] == [
        {"role": "system", "content": "host authorization policy"},
        {"role": "user", "content": "original user prompt"},
    ]


def test_openai_typed_cache_layout_keeps_history_before_volatile_facts() -> None:
    backend = OpenAICompatibleBackend(_OPTIONS)
    captured = {}
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "read_file",
                    "input": {"path": "README.md"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": "file contents",
                    "is_error": False,
                }
            ],
        },
    ]

    def request_json(path, payload, headers):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}

    backend.request_json = request_json
    backend.generate(
        CacheStructuredPrompt(
            "stable model rules",
            "changing execution facts",
            stable_user_prefix="stable task snapshot",
        ),
        tools=_TOOLS,
        messages=messages,
        request_options=ProviderRequestOptions(
            system_instruction="host authorization policy"
        ),
    )

    assert captured["payload"]["messages"] == [
        {
            "role": "system",
            "content": "host authorization policy\n\nstable model rules",
        },
        {"role": "user", "content": "stable task snapshot"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "README.md"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "file contents"},
        {"role": "user", "content": "changing execution facts"},
    ]


def test_openai_typed_first_turn_merges_stable_and_volatile_user_text() -> None:
    backend = OpenAICompatibleBackend(_OPTIONS)
    captured = {}

    def request_json(path, payload, headers):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}

    backend.request_json = request_json
    backend.generate(
        CacheStructuredPrompt(
            "stable model rules",
            "changing execution facts",
            stable_user_prefix="stable task snapshot",
        ),
        messages=[],
    )

    assert captured["payload"]["messages"] == [
        {"role": "system", "content": "stable model rules"},
        {
            "role": "user",
            "content": "stable task snapshot\n\nchanging execution facts",
        },
    ]


def test_openai_typed_canonical_user_is_sent_once_through_messages() -> None:
    backend = OpenAICompatibleBackend(_OPTIONS)
    captured = {}

    def request_json(path, payload, headers):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]}

    backend.request_json = request_json
    canonical = "# User Task\nsecond task"
    backend.generate(
        CacheStructuredPrompt(
            "stable model rules",
            "changing execution facts",
            canonical_user_turn=canonical,
        ),
        messages=[{"role": "user", "content": canonical}],
    )

    sent = captured["payload"]["messages"]
    assert sum(message.get("content", "").count(canonical) for message in sent) == 1
    assert sent == [
        {"role": "system", "content": "stable model rules"},
        {
            "role": "user",
            "content": f"{canonical}\n\nchanging execution facts",
        },
    ]


def test_openai_stream_accumulates_fragmented_native_tool_arguments() -> None:
    backend = OpenAICompatibleBackend(replace(_OPTIONS, stream_enabled=True))
    backend.request_stream = lambda path, payload, headers: [
        json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_2",
                                    "function": {"name": "read_file", "arguments": '{"path":'},
                                }
                            ]
                        }
                    }
                ]
            }
        ),
        json.dumps(
            {
                "choices": [
                    {
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}]},
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        ),
        "[DONE]",
    ]

    response = backend.generate("read", tools=_TOOLS)

    assert response.tool_use_blocks == [
        {"id": "call_2", "name": "read_file", "input": {"path": "a.py"}}
    ]
    assert response.truncated is False
    assert response.stop_reason == "tool_calls"


def test_openai_length_native_arguments_are_rejected_as_incomplete() -> None:
    backend = OpenAICompatibleBackend(_OPTIONS)
    backend.request_json = lambda path, payload, headers: {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "function": {"name": "read_file", "arguments": '{"path":'},
                        }
                    ],
                },
                "finish_reason": "length",
            }
        ]
    }

    # EXEC-31b 截断修复链: length+截断参数不再抛 ProviderResponseError,
    # 改为 truncated=True 响应, 由 native 截断续写修复(不再文本协议时代抛错)。
    response = backend.generate("read", tools=_TOOLS)
    assert response.truncated is True
    assert getattr(response, "stop_reason", "") == "length"
