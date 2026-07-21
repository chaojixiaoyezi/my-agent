from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends.base import BackendOptions, OpenAICompatibleBackend
from agent_py_agent.agent.backends.errors import ProviderResponseError

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
    response = backend.generate("original user prompt", tools=_TOOLS, messages=messages)

    assert response.text == "done"
    sent = captured["payload"]["messages"]
    assert sent[0] == {"role": "user", "content": "original user prompt"}
    assert sent[1]["tool_calls"][0]["function"]["arguments"] == '{"path": "README.md"}'
    assert sent[2] == {"role": "tool", "tool_call_id": "call_1", "content": "file contents"}


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

    with pytest.raises(ProviderResponseError) as exc_info:
        backend.generate("read", tools=_TOOLS)

    assert exc_info.value.error_code == "MODEL_INCOMPLETE_RESPONSE"
