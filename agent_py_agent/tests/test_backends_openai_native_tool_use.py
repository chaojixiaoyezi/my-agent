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


# 生产 2026-09-30：模型只回了思考（没有正文、没有工具调用）的一条 assistant 进了 native history，
#   之后每次请求都被 DeepSeek 以「content or tool_calls must be set」400 拒绝，线程卡死。
@pytest.mark.parametrize("blocks", [
    [{"type": "thinking", "thinking": "只有思考，没有可见答复。"}],
    [{"type": "thinking", "thinking": "思考"}, {"type": "text", "text": ""}],
    [{"type": "text", "text": ""}],
])
def test_openai_assistant_without_text_or_tool_calls_is_not_replayed(blocks) -> None:
    from agent_py_agent.agent.backends.openai_chat import _openai_assistant_messages

    assert _openai_assistant_messages(blocks) == []


def test_openai_request_never_sends_assistant_without_content_or_tool_calls() -> None:
    backend = OpenAICompatibleBackend(_OPTIONS)
    captured = {}
    messages = [
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "只有思考的空答复"}]},
        {"role": "user", "content": [{"type": "text", "text": "上一条没有答复，请继续。"}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "这次有正文"},
            {"type": "text", "text": "好的，继续处理。"},
        ]},
    ]
    before = json.dumps(messages, ensure_ascii=False)

    def request_json(path, payload, headers):
        captured.update(payload)
        return {"choices": [{"message": {"content": "继续"}, "finish_reason": "stop"}]}

    backend.request_json = request_json
    backend.generate("原任务", messages=messages, tools=_TOOLS)
    assistants = [m for m in captured["messages"] if m["role"] == "assistant"]
    assert all(m.get("content") or m.get("tool_calls") for m in assistants), assistants
    assert assistants == [
        {"role": "assistant", "content": "好的，继续处理。", "reasoning_content": "这次有正文"}
    ]
    assert json.dumps(messages, ensure_ascii=False) == before, "历史本身不得被改写"


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


# --------------------------------------------------------------------------- #
# 10-04 DeepSeek 官网实测（~/.my-agent/decision-evidence/deepseek-cache-audit-1004/server_probe_facts.md）：
# 思考模式只要求“最后一条 user 之后”的 assistant 都带 reasoning_content（缺了 400，纯文本也一样）；更早轮次的不带也接受。
# 关思考是另一个缓存分区（整段上下文按未命中重算），所以只能在本轮真缺思考时才关，旧答复缺思考不能连累整条线程。
# --------------------------------------------------------------------------- #


def _two_turn_messages(previous_reasoning, current_reasoning):
    """上一轮一条纯文本答复 + 本轮一次工具调用；reasoning 为 None 表示那条消息没有思考块。"""
    previous = [{"type": "thinking", "thinking": previous_reasoning}] if previous_reasoning is not None else []
    current = [{"type": "thinking", "thinking": current_reasoning}] if current_reasoning is not None else []
    current.append({"type": "tool_use", "id": "call_2", "name": "read_file", "input": {"path": "b.txt"}})
    return [
        {"role": "user", "content": [{"type": "text", "text": "上一轮的问题"}]},
        {"role": "assistant", "content": [*previous, {"type": "text", "text": "上一轮的答复"}]},
        {"role": "user", "content": [{"type": "text", "text": "这一轮的问题"}]},
        {"role": "assistant", "content": current},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_2", "content": "内容"}]},
    ]


def test_previous_turn_answer_without_reasoning_keeps_thinking_mode():
    captured = {}
    backend = _openai_backend_capturing(captured)
    backend.generate("继续", tools=_TOOLS, messages=_two_turn_messages(None, "这一轮要读 b"))

    payload = captured["payload"]
    assert "thinking" not in payload, "上一轮答复缺思考不在本轮，不能关思考（关思考会换缓存分区并让模型不再思考）"
    assistants = [m for m in payload["messages"] if m.get("role") == "assistant"]
    assert "reasoning_content" not in assistants[0], "不补造旧答复的思考"
    assert assistants[1]["reasoning_content"] == "这一轮要读 b"


def test_current_turn_tool_call_without_reasoning_still_disables_thinking():
    captured = {}
    backend = _openai_backend_capturing(captured)
    backend.generate("继续", tools=_TOOLS, messages=_two_turn_messages("上一轮有思考", None))

    assert captured["payload"]["thinking"] == {"type": "disabled"}, "本轮内缺思考上游会 400，必须显式关思考"


def test_thinking_rule_only_checks_assistants_after_last_user():
    from agent_py_agent.agent.backends.openai_chat import _thinking_mode_supported

    call = {"id": "c", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
    old_text = {"role": "assistant", "content": "旧答复"}
    thought_call = {"role": "assistant", "content": "", "reasoning_content": "要读", "tool_calls": [call]}
    tool_result = {"role": "tool", "tool_call_id": "c", "content": "内容"}
    user = {"role": "user", "content": "问题"}
    assert _thinking_mode_supported([user, old_text, user, thought_call, tool_result])
    assert not _thinking_mode_supported([user, old_text, thought_call, tool_result]), "最后一条 user 之后的纯文本也要思考"
    assert not _thinking_mode_supported([old_text, thought_call]), "没有 user 时整段都算本轮"
    assert _thinking_mode_supported([{"role": "system", "content": "s"}, user])


# LLM: 捕获真正进入 Chat 假传输的载荷；流/非流只返回合成无思考最终答复，不发网络、不输出认证头。
# 函数用途: 比较相邻请求的最终 JSON，而不是仅测内部支持函数。
def _capture_thinking_payloads(backend):
    captured = []

    def transport(path, payload, headers):
        del path, headers
        captured.append(json.loads(json.dumps(payload)))
        return {"choices": [{"message": {"content": "本轮结束，没有思考字段。"}, "finish_reason": "stop"}]}

    def stream(path, payload, headers):
        obj = transport(path, payload, headers)
        message = obj["choices"][0]["message"]
        return [json.dumps({"choices": [{"delta": message, "finish_reason": "stop"}]}), "[DONE]"]

    backend.request_json = transport
    backend.request_stream = stream
    return captured


# LLM: 只构造合成官网选项，不读生产配置、凭据或历史；档位由请求参数单独传递。
# 函数用途: 建立 max 档位的离线协议测试后端。
def _deepseek_history_backend(stream=False):
    return OpenAICompatibleBackend(replace(_OPTIONS, api_base="https://api.deepseek.com/v1",
        model_name="deepseek-v4-flash", stream_enabled=stream, reasoning_control="effort"))


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("thinking_disabled", [False, True])
def test_deepseek_final_without_reasoning_keeps_next_turn_thinking_and_effort(stream, thinking_disabled):
    backend = _deepseek_history_backend(stream)
    captured = _capture_thinking_payloads(backend)
    history = _tool_call_round_messages("真实工具思考")
    prompt = CacheStructuredPrompt("稳定规则", "")
    options = ProviderRequestOptions(reasoning_effort="max", thinking_disabled=thinking_disabled)
    final = backend.generate(prompt, tools=_TOOLS, messages=history, request_options=options)
    assert all(b["type"] != "thinking" for b in final.assistant_content_blocks)
    history += [{"role": "assistant", "content": final.assistant_content_blocks},
                {"role": "user", "content": [{"type": "text", "text": "下一轮任务"}]}]
    before = json.dumps(history, ensure_ascii=False)
    backend.generate(prompt, tools=_TOOLS, messages=history, request_options=options)
    previous, following = captured
    assert following["messages"][:len(previous["messages"])] == previous["messages"]
    assert following.get("tools") == previous["tools"] and following["tool_choice"] == previous["tool_choice"]
    assert following.get("thinking") == previous.get("thinking") == ({"type": "disabled"} if thinking_disabled else None)
    assert following.get("reasoning_effort") == previous.get("reasoning_effort") == (None if thinking_disabled else "max")
    assert json.dumps(history, ensure_ascii=False) == before


@pytest.mark.parametrize("reasoning", [None, "", " \n\t"])
def test_deepseek_current_tool_call_without_reasoning_still_disables_thinking(reasoning):
    backend = _deepseek_history_backend()
    captured = _capture_thinking_payloads(backend)
    history = _tool_call_round_messages(reasoning)
    before = json.dumps(history, ensure_ascii=False)
    backend.generate(CacheStructuredPrompt("稳定规则", ""), tools=_TOOLS, messages=history,
                     request_options=ProviderRequestOptions(reasoning_effort="max"))
    payload = captured[0]
    assert payload["messages"][-1]["role"] == "tool", "tool_result 的原生 user 容器不重置最终 user 边界"
    assert payload["thinking"] == {"type": "disabled"} and "reasoning_effort" not in payload
    assert "reasoning_content" not in payload["messages"][-2]
    assert json.dumps(history, ensure_ascii=False) == before


@pytest.mark.parametrize("kind", ["user", "runtime", "summary"])
def test_deepseek_ir_user_facts_and_summary_reset_wire_boundary(kind):
    from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
    from agent_py_agent.agent.backends.tool_ir import CompactionSummary, RuntimeFactsTurn, UserTurn

    backend = _deepseek_history_backend()
    captured = _capture_thinking_payloads(backend)
    item = {"user": UserTurn("插话"), "runtime": RuntimeFactsTurn("事实", source="fixture"),
            "summary": CompactionSummary("摘要")}[kind]
    history = [*_tool_call_round_messages(None), *AnthropicMessageAdapter().to_provider_messages([item])]
    backend.generate(CacheStructuredPrompt("稳定规则", ""), tools=_TOOLS, messages=history,
                     request_options=ProviderRequestOptions(reasoning_effort="max"))
    payload = captured[0]
    assert payload["messages"][-1] == {"role": "user", "content": item.text}
    assert "thinking" not in payload and payload["reasoning_effort"] == "max"


@pytest.mark.parametrize("thread_key", ["conversation_thread_id", "agent_thread_id"])
def test_main_and_child_request_paths_capture_forced_partition_changes(thread_key):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.tool_model_generation import _do_backend_generate
    from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice

    backend = _deepseek_history_backend()
    captured = _capture_thinking_payloads(backend)
    loaded = []
    def load(thread):
        loaded.append(thread)
        return SimpleNamespace(reasoning_effort="max")
    agent = SimpleNamespace(config=SimpleNamespace(model_reasoning_effort="low"),
                            conversation_store=SimpleNamespace(threads=SimpleNamespace(load=load)))
    state = SimpleNamespace(agent=agent, params=SimpleNamespace(task_attributes={thread_key: "fixture-thread"}),
        tools=_TOOLS, messages=_two_turn_messages(None, "新轮思考"), on_chunk=None, tool_rounds=1,
        system_instruction="稳定规则", first_token_timeout_seconds=1)
    choices = [ToolChoice.auto(), ToolChoice.none("natural"), ToolChoice.auto(),
               ToolChoice.required(), ToolChoice.specific("read_file"), ToolChoice.auto()]
    for choice in choices:
        state.tool_choice = choice
        _do_backend_generate(backend, CacheStructuredPrompt("", ""), state)
    assert loaded == ["fixture-thread"] * len(choices)
    # 记录现有强制策略的真实例外，不把 none/required/specific 的关思考包装成“相邻分区始终一致”。
    assert [p.get("thinking") for p in captured] == [None, {"type": "disabled"}, None,
                                                   {"type": "disabled"}, {"type": "disabled"}, None]
    assert [p.get("reasoning_effort") for p in captured] == ["max", None, "max", None, None, "max"]
    assert all(p["tools"] == captured[0]["tools"] and p["messages"] == captured[0]["messages"] for p in captured)


def test_deepseek_current_plain_assistant_without_reasoning_still_disables_thinking():
    backend = _deepseek_history_backend()
    captured = _capture_thinking_payloads(backend)
    history = [{"role": "user", "content": "本轮任务"}, {"role": "assistant", "content": "本轮无思考文本"}]
    backend.generate(CacheStructuredPrompt("稳定规则", ""), tools=_TOOLS, messages=history,
                     request_options=ProviderRequestOptions(reasoning_effort="max"))
    assert captured[0]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in captured[0]
