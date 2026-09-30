"""出站协议合同：规范原生历史在三个后端出口的修整与校验，以及随机历史下三种协议的请求都合规。

形状依据：Chat Completions / Anthropic Messages / Responses 公开规范，DeepSeek 官网 2026-09-30 生产 400，
MiniMax-M2.7 与 MiniMax-M3 官网 2026-09-30 实测（孤儿工具结果两者都 400；调用与结果之间夹 user 消息 M3 400）。
"""
from __future__ import annotations

from copy import deepcopy

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agent_py_agent.agent.backends import BackendOptions, responses, wire_contract
from agent_py_agent.agent.backends.anthropic import AnthropicCompatibleBackend
from agent_py_agent.agent.backends.errors import (
    ProviderRequestRejectedError,
    ProviderRequestShapeInvalidError,
    is_provider_environment_fault,
    provider_configuration_report,
)
from agent_py_agent.agent.backends.message_adapter import ORPHAN_TOOL_RESULT_STUB
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend
from agent_py_agent.agent.backends.wire_contract import (
    repair_native_messages,
    validate_anthropic_messages,
    validate_chat_messages,
    validate_responses_input,
)
from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.gateway_parts.request_errors import gateway_client_error_message
from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt

_OPTIONS = BackendOptions("https://example.test/v1", "test", "model", stream_enabled=False)
_TOOLS = [{"name": "read_file", "description": "读取文件", "input_schema": {"type": "object"}}]
_REASONING = {"type": "responses_reasoning", "model": "model",
              "item": {"type": "reasoning", "encrypted_content": "enc", "summary": []}}


def _user(*blocks):
    return {"role": "user", "content": list(blocks)}


def _assistant(*blocks):
    return {"role": "assistant", "content": list(blocks)}


def _text(value):
    return {"type": "text", "text": value}


def _call(call_id):
    return {"type": "tool_use", "id": call_id, "name": "read_file", "input": {"path": "a"}}


def _result(call_id, content="ok"):
    return {"type": "tool_result", "tool_use_id": call_id, "content": content, "is_error": False}


def _stub(call_id):
    return {"type": "tool_result", "tool_use_id": call_id, "content": ORPHAN_TOOL_RESULT_STUB, "is_error": True}


def _chat_payload(messages, prompt="继续"):
    return OpenAICompatibleBackend(_OPTIONS).project_generate_payload(prompt, tools=_TOOLS, messages=messages)


def _anthropic_payload(messages, prompt="继续"):
    return AnthropicCompatibleBackend(_OPTIONS).project_generate_payload(prompt, tools=_TOOLS, messages=messages)


def _responses_payload(messages, prompt="继续"):
    backend = OpenAIResponsesBackend(_OPTIONS)
    captured = []
    reply = {"status": "completed", "usage": {},
             "output": [{"type": "message", "content": [{"type": "output_text", "text": "好"}]}]}
    backend.request_json = lambda path, payload, headers: captured.append(payload) or reply
    backend.generate(prompt, tools=_TOOLS, messages=messages)
    return captured[0]


def test_text_protocol_passes_through():
    assert repair_native_messages(None) is None


def test_valid_history_keeps_original_objects_for_stable_cache_bytes():
    history = [_user(_text("任务")), _assistant(_text("读"), _call("a")), _user(_result("a")), _assistant(_text("完成"))]
    repaired = repair_native_messages(history)
    assert repaired == history
    assert all(new is old for new, old in zip(repaired, history))


@pytest.mark.parametrize("block", [_text(""), _text("  \n"), {"type": "thinking", "thinking": ""},
                                   {"type": "thinking", "thinking": " "}])
def test_blank_blocks_are_not_sent(block):
    history = [_user(_text("任务")), _assistant(block, _text("答复")), _user(_text("继续"))]
    assert repair_native_messages(history)[1] == _assistant(_text("答复"))


def test_signed_thinking_is_kept_and_emptied_messages_are_dropped():
    signed = {"type": "thinking", "thinking": "", "signature": "sig"}
    history = [_user(_text("任务")), _assistant(signed), _assistant(_text("")), {"role": "user", "content": "  "},
               {"role": "assistant", "content": None}, _user(_text("继续"))]
    assert repair_native_messages(history) == [_user(_text("任务")), _assistant(signed), _user(_text("继续"))]


def test_steer_between_call_and_results_moves_after_all_results():
    history = [_user(_text("任务")), _assistant(_call("a"), _call("b")), _user(_result("a")), _user(_text("插话")),
               _user(_result("b"))]
    assert repair_native_messages(history) == [
        _user(_text("任务")), _assistant(_call("a"), _call("b")), _user(_result("a"), _result("b")), _user(_text("插话")),
    ]


def test_text_before_results_moves_after_them():
    history = [_assistant(_call("a")), _user(_text("说明"), _result("a"))]
    assert repair_native_messages(history) == [_assistant(_call("a")), _user(_result("a"), _text("说明"))]


def test_missing_results_get_structured_unknown_stub_after_found_ones():
    history = [_user(_text("任务")), _assistant(_call("a"), _call("b")), _user(_result("b"))]
    assert repair_native_messages(history)[2] == _user(_result("b"), _stub("a"))


def test_trailing_call_without_results_gets_stub_message():
    repaired = repair_native_messages([_user(_text("任务")), _assistant(_text("读"), _call("a"))])
    assert repaired[-1] == _user(_stub("a"))


def test_orphan_and_misplaced_results_are_dropped():
    history = [_user(_result("ghost")), _user(_text("任务")), _assistant(_call("a")), _user(_text("插话")),
               _assistant(_text("先回答")), _user(_result("a"), _text("补充"))]
    assert repair_native_messages(history) == [
        _user(_text("任务")), _assistant(_call("a")), _user(_stub("a"), _text("插话")), _assistant(_text("先回答")),
        _user(_text("补充")),
    ]


def test_duplicate_results_keep_the_first_one():
    history = [_assistant(_call("a")), _user(_result("a", "first"), _result("a", "second"))]
    assert repair_native_messages(history)[1] == _user(_result("a", "first"))


def test_same_call_id_in_two_rounds_pairs_by_adjacency():
    history = [_assistant(_call("call_0")), _user(_result("call_0", "one")), _assistant(_call("call_0")),
               _user(_result("call_0", "two"))]
    assert repair_native_messages(history) == history


def test_repair_never_mutates_caller_history():
    history = [_user(_result("ghost"), _text("")), _assistant(_text(""), _call("a")), _user(_text("插话"), _result("a"))]
    snapshot = deepcopy(history)
    repair_native_messages(history)
    assert history == snapshot


@pytest.mark.parametrize(("messages", "rule", "index"), [
    ([{"role": "user", "content": "hi"}, {"role": "assistant", "content": None}], "assistant_without_content", 1),
    ([{"role": "assistant", "content": "", "reasoning_content": "x"}], "assistant_without_content", 0),
    ([{"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]}, {"role": "user", "content": "x"}],
     "tool_calls_without_reply", 1),
    ([{"role": "assistant", "content": "ok"}, {"role": "tool", "tool_call_id": "c1", "content": "x"}],
     "tool_reply_without_call", 1),
    ([{"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]}], "tool_calls_without_reply", 1),
])
def test_chat_validator_rejects_known_provider_400_shapes(messages, rule, index):
    with pytest.raises(ProviderRequestShapeInvalidError) as caught:
        validate_chat_messages(messages)
    assert caught.value.details == {"protocol": "chat", "rule": rule, "index": index}


@pytest.mark.parametrize(("messages", "rule", "index"), [
    ([{"role": "system", "content": "x"}], "unknown_role", 0),
    ([_user()], "empty_content", 0),
    ([_user(_text(" "))], "blank_text_block", 0),
    ([_assistant(_call("a")), _user(_text("x"))], "tool_use_without_result", 1),
    ([_assistant(_call("a")), _user(_text("x"), _result("a"))], "tool_result_after_other_content", 1),
    ([_user(_result("x"))], "tool_result_without_call", 0),
    ([_assistant(_call("a")), _assistant(_text("x"))], "tool_use_without_result", 1),
    ([_assistant(_call("a"))], "tool_use_without_result", 1),
])
def test_anthropic_validator_rejects_protocol_violations(messages, rule, index):
    with pytest.raises(ProviderRequestShapeInvalidError) as caught:
        validate_anthropic_messages(messages)
    assert caught.value.details == {"protocol": "anthropic", "rule": rule, "index": index}


@pytest.mark.parametrize(("items", "rule", "index"), [
    ([{"type": "reasoning"}, {"role": "user", "content": "x"}], "reasoning_without_follower", 0),
    ([{"role": "assistant", "content": "x"}, {"type": "reasoning"}], "reasoning_without_follower", 1),
    ([{"type": "function_call_output", "call_id": "c1", "output": "x"}], "output_without_call", 0),
    ([{"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"}], "call_without_output", 1),
])
def test_responses_validator_rejects_protocol_violations(items, rule, index):
    with pytest.raises(ProviderRequestShapeInvalidError) as caught:
        validate_responses_input(items)
    assert caught.value.details == {"protocol": "responses", "rule": rule, "index": index}


def test_validators_accept_shapes_providers_accept():
    validate_chat_messages([{"role": "system", "content": "s"}, {"role": "user", "content": ""},
                            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}, {"id": "c2"}]},
                            {"role": "tool", "tool_call_id": "c2", "content": "x"},
                            {"role": "tool", "tool_call_id": "c1", "content": "y"},
                            {"role": "user", "content": "a"}, {"role": "user", "content": "b"}])
    validate_anthropic_messages([{"role": "user", "content": ""}, _assistant({"type": "thinking", "thinking": "t"}),
                                 _user(_text("a")), _user(_text("b")), _assistant(_call("a"), _call("b")),
                                 _user(_result("b"), _result("a"), _text("c"))])
    validate_responses_input([{"type": "reasoning"}, {"type": "reasoning"}, {"role": "assistant", "content": "x"},
                              {"type": "reasoning"}, {"type": "function_call", "call_id": "c1"},
                              {"type": "function_call_output", "call_id": "c1"}])


def test_shape_error_is_a_typed_deterministic_rejection():
    exc = ProviderRequestShapeInvalidError(protocol="chat", rule="assistant_without_content", index=3)
    assert isinstance(exc, ProviderRequestRejectedError) and exc.status_code == 0
    assert exc.error_code == "PROVIDER_REQUEST_SHAPE_INVALID"
    assert not is_provider_environment_fault(exc)
    contract = error_contract(exc.error_code)
    assert contract.code == exc.error_code and contract.retryable is False
    # 用户看到的是"请求没有发出、属于组装缺陷"，不是"服务拒绝"或"检查密钥"。
    user_text = gateway_client_error_message(exc.error_code)
    assert "没有发出" in user_text and user_text != gateway_client_error_message("PROVIDER_REQUEST_REJECTED")
    assert provider_configuration_report(exc).startswith("[provider_request_shape_invalid]")


# 一段把已知坏形状都放进去的历史：孤儿结果、仅思考轮（09-30 生产 400）、空白文本、插话夹在调用与结果之间、
# 结果前面有正文、仅 Responses 推理密文轮、末尾调用没有结果。
_BROKEN_HISTORY = [
    _user(_result("ghost")),
    _user(_text("任务")),
    _assistant({"type": "thinking", "thinking": "只想了没说"}),
    _assistant(_text(""), _call("a"), _call("b")),
    _user(_text("插话"), _result("a")),
    _user(_result("b")),
    _assistant(_text("  ")),
    _user(_text("")),
    _assistant(_REASONING),
    _assistant(_text("再读"), _call("c")),
]


def test_broken_history_becomes_valid_chat_request():
    history = deepcopy(_BROKEN_HISTORY)
    messages = _chat_payload(history)["messages"]
    assert history == _BROKEN_HISTORY
    validate_chat_messages(messages)
    roles = [message["role"] for message in messages]
    assert roles == ["user", "user", "assistant", "tool", "tool", "user", "assistant", "tool"]
    assert all(message.get("content") or message.get("tool_calls") for message in messages if message["role"] == "assistant")


def test_broken_history_becomes_valid_anthropic_request():
    messages = _anthropic_payload(deepcopy(_BROKEN_HISTORY))["messages"]
    validate_anthropic_messages(messages)
    calls = [message for message in messages if any(block.get("type") == "tool_use" for block in message["content"])]
    assert [len(message["content"]) for message in calls] == [2, 2]


def test_broken_history_becomes_valid_responses_request():
    items = _responses_payload(deepcopy(_BROKEN_HISTORY))["input"]
    validate_responses_input(items)
    assert [item["type"] for item in items if item.get("type")] == [
        "function_call", "function_call", "function_call_output", "function_call_output", "function_call",
        "function_call_output",
    ]


def test_reasoning_only_assistant_is_not_sent_as_orphan_reasoning_item():
    items = _responses_payload([_user(_text("任务")), _assistant(_REASONING), _user(_text("继续"))])["input"]
    assert all(item.get("type") != "reasoning" for item in items)
    items = _responses_payload([_user(_text("任务")), _assistant(_REASONING, _text("答"))])["input"]
    assert [item.get("type") for item in items][-2:] == ["reasoning", None]


@pytest.mark.parametrize("cache", [True, False])
@pytest.mark.parametrize("prompt", [" \n", CacheStructuredPrompt("稳定规则", "  \n"),
                                    CacheStructuredPrompt("稳定规则", "动态", stable_user_prefix="  ")])
def test_whitespace_prompt_parts_are_not_sent_as_blank_text_blocks(prompt, cache):
    # Anthropic 规范拒收空白文本块；prompt 投影不能产出它们，否则出口校验会把本可发送的请求拦在本地。
    backend = AnthropicCompatibleBackend(BackendOptions("https://example.test/v1", "test", "model",
                                                        stream_enabled=False, prompt_cache_enabled=cache))
    messages = backend.project_generate_payload(prompt, messages=[_user(_text("任务"))])["messages"]
    validate_anthropic_messages(messages)
    assert all(block["text"].strip() for m in messages if isinstance(m["content"], list)
               for block in m["content"] if block.get("type") == "text")


def _record_sends(backend):
    sent = []
    backend.request_json = lambda path, payload, headers: sent.append(payload) or {}
    return sent


@pytest.mark.parametrize("backend_class", [OpenAICompatibleBackend, AnthropicCompatibleBackend, OpenAIResponsesBackend])
def test_exit_validator_blocks_the_send_when_projection_is_broken(monkeypatch, backend_class):
    # 模拟修整被改坏：校验必须在本地拦下，一次请求都不发。
    monkeypatch.setattr(wire_contract, "repair_native_messages", lambda messages: messages)
    monkeypatch.setattr(responses, "repair_native_messages", lambda messages: messages)
    backend = backend_class(_OPTIONS)
    sent = _record_sends(backend)
    with pytest.raises(ProviderRequestShapeInvalidError):
        backend.generate("继续", tools=_TOOLS, messages=[_user(_text("任务")), _assistant(_call("a")), _user(_text("插话"))])
    assert sent == []


_IDS = st.sampled_from(["a", "b", "c"])
_TEXTS = st.sampled_from(["", " ", "\n", "正文", "done"])
_USER_BLOCK = st.one_of(st.builds(_text, _TEXTS), st.builds(_result, _IDS, st.sampled_from(["", "ok"])))
_THINKING = st.builds(lambda text, signed: {"type": "thinking", "thinking": text, **({"signature": "sig"} if signed else {})},
                      _TEXTS, st.booleans())
_ASSISTANT_BLOCK = st.one_of(st.builds(_text, _TEXTS), _THINKING, st.just({"type": "redacted_thinking", "data": "opaque"}),
                             st.just(_REASONING), st.builds(_call, _IDS))


# LLM: 规范 IR 里同一条助手消息的工具调用 id 唯一（来自 canonical ToolCall）；随机生成时保持这个不变量。
# 函数用途: 去掉同一条助手消息里重复 id 的工具调用块。
def _unique_calls(blocks):
    seen, kept = set(), []
    for block in blocks:
        if block.get("type") == "tool_use" and block["id"] in seen:
            continue
        seen.add(block.get("id"))
        kept.append(block)
    return kept


_MESSAGE = st.one_of(
    st.builds(lambda blocks: {"role": "user", "content": blocks}, st.lists(_USER_BLOCK, max_size=4)),
    st.builds(lambda text: {"role": "user", "content": text}, _TEXTS),
    st.builds(lambda blocks: {"role": "assistant", "content": _unique_calls(blocks)}, st.lists(_ASSISTANT_BLOCK, max_size=4)),
)
_PROMPTS = st.sampled_from(["", " \n", "继续", CacheStructuredPrompt("稳定规则", "动态事实"), CacheStructuredPrompt("稳定规则"),
                           CacheStructuredPrompt("稳定规则", "  \n", stable_user_prefix=" ")])


# LLM: 打平非空白正文，用来确认修整不丢、不重排任何可见文字。
# 函数用途: 依次取出一段历史里全部非空白文本。
def _visible_texts(messages):
    texts = []
    for message in messages:
        content = message["content"]
        blocks = [_text(content)] if isinstance(content, str) else content
        texts.extend(block["text"] for block in blocks if block.get("type") == "text" and block["text"].strip())
    return texts


@settings(max_examples=400, deadline=None, derandomize=True)
@given(st.lists(_MESSAGE, max_size=10), _PROMPTS)
def test_any_history_produces_valid_requests_for_every_protocol(history, prompt):
    snapshot = deepcopy(history)
    repaired = repair_native_messages(history)
    assert history == snapshot
    assert repair_native_messages(repaired) == repaired
    validate_anthropic_messages(repaired)
    assert _visible_texts(repaired) == _visible_texts([m for m in history if isinstance(m["content"], (str, list))])
    calls = [block for m in history for block in m["content"] if isinstance(m["content"], list) and block.get("type") == "tool_use"]
    assert [block for m in repaired for block in m["content"] if isinstance(m["content"], list) and block.get("type") == "tool_use"] == calls
    validate_chat_messages(_chat_payload(history, prompt)["messages"])
    validate_anthropic_messages(_anthropic_payload(history, prompt)["messages"])
    validate_responses_input(_responses_payload(history, prompt)["input"])
