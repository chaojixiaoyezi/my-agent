from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends.base import BackendOptions, ProviderRequestOptions
from agent_py_agent.agent.backends.errors import ProviderResponseError
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
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


@pytest.mark.parametrize("stream", [False, True])
def test_reasoning_only_response_keeps_history_and_usage(stream):
    backend = OpenAICompatibleBackend(replace(_OPTIONS, stream_enabled=stream))
    reasoning = "已分析输入，下一步检查统计结果。" * 1800
    calls = []
    usage = {"prompt_tokens": 40000, "completion_tokens": 12000, "total_tokens": 52000}

    def nonstream(*args):
        calls.append(args)
        return {"choices": [{"message": {"content": None, "reasoning_content": reasoning},
                             "finish_reason": "stop"}], "usage": usage}

    def events(*args):
        calls.append(args)
        yield json.dumps({"choices": [{"delta": {"reasoning_content": reasoning}}]})
        yield json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage})
        yield "[DONE]"

    backend.request_json = nonstream
    backend.request_stream = events
    response = backend.generate("继续完成任务", tools=_TOOLS)
    assert len(calls) == 1
    assert response.text == ""
    assert response.tool_use_blocks == []
    assert response.assistant_content_blocks == [{"type": "thinking", "thinking": reasoning}]
    assert response.usage["completion_tokens"] == 12000
    assert response.stop_reason == "stop"


@pytest.mark.parametrize("reasoning", ["", " \n\t"])
def test_empty_reasoning_still_reports_empty_response(reasoning):
    backend = OpenAICompatibleBackend(_OPTIONS)
    backend.request_json = lambda *_: {"choices": [{
        "message": {"content": None, "reasoning_content": reasoning}, "finish_reason": "stop",
    }]}
    with pytest.raises(ProviderResponseError, match="没有文本或工具调用"):
        backend.generate("继续", tools=_TOOLS)


def test_openai_tool_arguments_report_live_progress_before_completion():
    backend = OpenAICompatibleBackend(replace(_OPTIONS, stream_enabled=True))
    progress = []
    sequence = []
    secret = "private-file-content" * 600

    def events(path, payload, headers):
        yield json.dumps({"choices": [{"delta": {"reasoning_content": "准备写文件"}}]})
        yield json.dumps({"choices": [{"delta": {"reasoning_content": "，已准备好", "tool_calls": [
            {"index": 0, "id": "c0", "function": {"name": "write_file", "arguments": '{"content":"'}}
        ]}}]})
        assert progress and progress[0]["phase"] == "started"
        assert sequence == ["thinking-end", "progress-started"]
        yield json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": secret}}
        ]}}]})
        assert progress[-1]["received_chars"] > 8192
        yield json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 1, "id": "c1", "function": {"name": "read_file", "arguments": '{"path":"a"}'}},
            {"index": 0, "function": {"arguments": '"}'}}
        ]}, "finish_reason": "tool_calls"}]})
        yield "[DONE]"

    class Thinking:
        def __call__(self, chunk):
            pass

        def complete(self, text):
            assert text == "准备写文件，已准备好"
            sequence.append("thinking-end")

    def on_progress(row):
        progress.append(row)
        sequence.append("progress-" + row["phase"])

    backend.request_stream_iter = events
    response = backend.generate("写文件", tools=_TOOLS, on_chunk=lambda _: None,
                                on_thinking_delta=Thinking(), on_tool_input_progress=on_progress)
    assert response.tool_use_blocks[0]["input"]["content"] == secret
    assert response.tool_use_blocks[1]["input"] == {"path": "a"}
    assert {p["stream_index"] for p in progress if p["phase"] == "ready"} == {0, 1}
    assert all(set(p) == {"schema", "phase", "stream_index", "tool", "received_chars"} for p in progress)
    assert secret not in json.dumps(progress)


def test_openai_tool_progress_callback_failure_does_not_drop_tool():
    backend = OpenAICompatibleBackend(replace(_OPTIONS, stream_enabled=True))
    backend.request_stream_iter = lambda *_: iter([
        json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c", "function": {"name": "read_file", "arguments": '{"path":"a"}'}}
        ]}, "finish_reason": "tool_calls"}]}), "[DONE]",
    ])

    def broken(_):
        raise RuntimeError("UI failure")

    response = backend.generate("read", tools=_TOOLS, on_chunk=lambda _: None, on_tool_input_progress=broken)
    assert response.tool_use_blocks == [{"id": "c", "name": "read_file", "input": {"path": "a"}}]


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
    # R233 真机修正：旧历史缺思考原文时，补空串不能满足思考模式，必须显式关思考。
    assert ("thinking" in captured) is required
    assert captured.get("thinking") == ({"type": "disabled"} if required else None)
    assert "reasoning_content" not in assistants[0]
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
    expected = {"role": "assistant", "content": "已定位，接下来继续。"}
    if reasoning.strip():
        expected["reasoning_content"] = reasoning
    assert sent == expected, "空思考块不得产出空 reasoning_content 字段"
    assert messages[0]["content"][0]["thinking"] == reasoning, "历史本身不得被改写"


def test_openai_plain_assistant_does_not_invent_reasoning_metadata() -> None:
    from agent_py_agent.agent.backends.openai_chat import _openai_assistant_messages

    assert _openai_assistant_messages([{"type": "text", "text": "hello"}]) == [
        {"role": "assistant", "content": "hello"}
    ]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reasoning", [None, "", "已经确认。"])
def test_openai_response_reasoning_presence_survives_native_replay(stream, reasoning):
    from agent_py_agent.agent.backends.openai_chat import _openai_assistant_messages

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
    # 归档仍保留"是否返回过思考字段"；但出站字段只在有真实思考原文时才写。
    assert ("reasoning_content" in replay) is bool(reasoning and reasoning.strip())
    if reasoning and reasoning.strip():
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


# --------------------------------------------------------------------------- #
# R233: 思考模式与历史一致性（真机 2026-09-11 OpenCode Go / deepseek-v4-flash）
# 上游原文: The `reasoning_content` in the thinking mode must be passed back to the API.
# 补空串不能满足该要求；历史不完整时必须显式关思考，完整时才回传真实原文。
# --------------------------------------------------------------------------- #


def _openai_backend_capturing(captured):
    from agent_py_agent.agent.backends.base import BackendOptions
    from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend

    backend = OpenAICompatibleBackend(
        BackendOptions(
            api_base="https://opencode.ai/zen/go/v1",
            api_key="test-key",
            model_name="deepseek-v4-flash",
            stream_enabled=False,
        )
    )

    def fake_request_json(path, payload, headers):
        del path, headers
        captured["payload"] = payload
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
        }

    backend.request_json = fake_request_json
    return backend


def _tool_call_round_messages(reasoning):
    """一条带工具调用的 assistant 历史；reasoning 为 None 表示旧历史没有思考块。"""
    blocks = []
    if reasoning is not None:
        blocks.append({"type": "thinking", "thinking": reasoning})
    blocks.append({"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "a.txt"}})
    return [
        {"role": "user", "content": [{"type": "text", "text": "读文件"}]},
        {"role": "assistant", "content": blocks},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "内容"}]},
    ]


def test_legacy_history_without_reasoning_disables_thinking_mode():
    captured = {}
    backend = _openai_backend_capturing(captured)
    backend.generate("继续", tools=_TOOLS, messages=_tool_call_round_messages(None))

    payload = captured["payload"]
    assert payload["thinking"] == {"type": "disabled"}, "旧历史缺思考时必须显式关思考，否则上游 400"
    assistant = next(m for m in payload["messages"] if m.get("role") == "assistant")
    assert "reasoning_content" not in assistant, "补空串既救不了旧会话，又会伪装成有思考"


def test_empty_thinking_block_does_not_become_empty_reasoning_field():
    captured = {}
    backend = _openai_backend_capturing(captured)
    backend.generate("继续", tools=_TOOLS, messages=_tool_call_round_messages(""))

    payload = captured["payload"]
    assistant = next(m for m in payload["messages"] if m.get("role") == "assistant")
    assert "reasoning_content" not in assistant
    assert payload["thinking"] == {"type": "disabled"}


def test_complete_reasoning_history_keeps_thinking_mode_and_original_text():
    captured = {}
    backend = _openai_backend_capturing(captured)
    backend.generate("继续", tools=_TOOLS, messages=_tool_call_round_messages("我需要先读文件"))

    payload = captured["payload"]
    assert "thinking" not in payload, "历史满足思考模式时不得擅自关闭"
    assistant = next(m for m in payload["messages"] if m.get("role") == "assistant")
    assert assistant["reasoning_content"] == "我需要先读文件", "必须回传真实思考原文"


def test_unknown_gateway_never_receives_deepseek_thinking_field():
    from agent_py_agent.agent.backends.base import BackendOptions
    from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend

    captured = {}
    backend = OpenAICompatibleBackend(
        BackendOptions(api_base="https://example.test/v1", api_key="k",
                       model_name="deepseek-v4-flash", stream_enabled=False)
    )
    backend.request_json = lambda path, payload, headers: (
        captured.__setitem__("payload", payload)
        or {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}
    )
    backend.generate("继续", tools=_TOOLS, messages=_tool_call_round_messages(None))

    assert "thinking" not in captured["payload"], "未知网关不得被强塞专有字段"
