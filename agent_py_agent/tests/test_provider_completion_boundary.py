"""供应商未完成响应必须保留真实原因且整轮零工具执行，不靠正文判断。"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.base import (
    AnthropicCompatibleBackend,
    ModelResponse,
    OpenAICompatibleBackend,
)
from agent_py_agent.agent.backends.responses_wire import response_fields
from agent_py_agent.agent.backends.tool_protocol_adapter import canonical_tool_calls_from_response
from agent_py_agent.agent.turn_end import result_turn_end_reason
from agent_py_agent.tests.test_backends_incomplete_response import _OPTIONS
from agent_py_agent.tests.test_canonical_tool_protocol_adapter import _request


# LLM: 纯内存 SSE；不调用网络、工具 handler、Gateway 或真实会话。
# 函数用途: 构造多工具参数及可选停止事件，以验证终态与 JSON 边界。
def _openai_stream(arguments: list[str], finish: str = "tool_calls") -> list[str]:
    lines = [json.dumps({"choices": [{"delta": {"content": "partial text", "reasoning_content": "reasoning"}}]})]
    for index, value in enumerate(arguments):
        lines.append(json.dumps({"choices": [{"delta": {"tool_calls": [{"index": index, "id": f"c{index}",
            "function": {"name": "run_command", "arguments": value}}]}}]}))
    if finish:
        lines.extend([json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}), "[DONE]"])
    return lines


@pytest.mark.parametrize(("arguments", "finish", "reason", "end"), [
    (["{\"command\":\"true\"}"], "", "MODEL_STREAM_INCOMPLETE", "error"),
    (["{\"command\":\"true\"}", "{\"command\":"], "tool_calls", "MODEL_TOOL_ARGUMENTS_INVALID", "error"),
    (["[]"], "tool_calls", "MODEL_TOOL_ARGUMENTS_INVALID", "error"),
    (["{\"command\":\"true\"}"], "length", "MODEL_RESPONSE_TRUNCATED", "max-tokens"),
    (["{\"command\":\"true\"}"], "content_filter", "MODEL_RESPONSE_CONTENT_FILTERED", "error"),
])
def test_openai_stream_incomplete_never_executes_or_archives_tools(arguments, finish, reason, end):
    backend = OpenAICompatibleBackend(_OPTIONS)
    requests = []

    def stream(*args):
        requests.append(args)
        return _openai_stream(arguments, finish)

    backend.request_stream = stream
    response = backend.generate("synthetic", tools=[{"name": "run_command"}])
    assert response.runtime_reason == reason
    assert result_turn_end_reason(response) == end
    assert response.stop_reason == finish
    assert response.truncated
    assert response.tool_use_blocks == []
    assert not any(block["type"] == "tool_use" for block in response.assistant_content_blocks)
    assert any(block["type"] == "thinking" for block in response.assistant_content_blocks)
    assert response.text == "partial text"
    assert canonical_tool_calls_from_response(_request(response, "native")).calls == ()
    assert len(requests) == 1


@pytest.mark.parametrize("arguments", ["{\"command\":", "[]", None])
def test_openai_nonstream_invalid_arguments_do_not_become_empty_dict(arguments):
    backend = OpenAICompatibleBackend(replace(_OPTIONS, stream_enabled=False))
    backend.request_json = lambda *args: {"choices": [{"finish_reason": "tool_calls", "message": {
        "content": "partial", "tool_calls": [{"id": "c1", "function": {"name": "run_command", "arguments": arguments}}]}}]}
    response = backend.generate("synthetic", tools=[{"name": "run_command"}])
    assert response.tool_use_blocks == []
    assert response.runtime_reason == "MODEL_TOOL_ARGUMENTS_INVALID"
    assert result_turn_end_reason(response) == "error"


# LLM: 坏块后跟好块也必须保留整轮失败事实；事件只在测试内生成。
# 函数用途: 生成 Anthropic 工具序列，覆盖末块覆盖先前错误的情况。
def _anthropic_stream(arguments: list[str], finish: str = "tool_use") -> list[str]:
    events = []
    for index, value in enumerate(arguments):
        events.extend([
            {"type": "content_block_start", "index": index,
             "content_block": {"type": "tool_use", "id": f"c{index}", "name": "run_command", "input": {}}},
            {"type": "content_block_delta", "index": index,
             "delta": {"type": "input_json_delta", "partial_json": value}},
            {"type": "content_block_stop", "index": index},
        ])
    if finish:
        events.extend([{"type": "message_delta", "delta": {"stop_reason": finish}}, {"type": "message_stop"}])
    return [json.dumps(event) for event in events]


@pytest.mark.parametrize(("arguments", "finish", "reason"), [
    (["{\"command\":\"true\"}"], "", "MODEL_STREAM_INCOMPLETE"),
    (["{\"command\":", "{\"command\":\"true\"}"], "tool_use", "MODEL_TOOL_ARGUMENTS_INVALID"),
    (["[]"], "tool_use", "MODEL_TOOL_ARGUMENTS_INVALID"),
])
def test_anthropic_bad_or_unfinished_stream_is_zero_execution(arguments, finish, reason):
    backend = AnthropicCompatibleBackend(_OPTIONS)
    requests = []

    def stream(*args):
        requests.append(args)
        return _anthropic_stream(arguments, finish)

    backend.request_stream = stream
    response = backend.generate("synthetic", tools=[{"name": "run_command"}])
    assert response.tool_use_blocks == []
    assert response.runtime_reason == reason
    assert result_turn_end_reason(response) == "error"
    assert len(requests) == 1


@pytest.mark.parametrize("tool_input", [None, [], "{}"])
def test_anthropic_nonstream_invalid_object_is_not_fabricated(tool_input):
    backend = AnthropicCompatibleBackend(replace(_OPTIONS, stream_enabled=False))
    backend.request_json = lambda *args: {"stop_reason": "tool_use", "content": [
        {"type": "tool_use", "id": "c1", "name": "run_command", "input": tool_input}]}
    response = backend.generate("synthetic", tools=[{"name": "run_command"}])
    assert response.tool_use_blocks == []
    assert response.runtime_reason == "MODEL_TOOL_ARGUMENTS_INVALID"


@pytest.mark.parametrize(("provider_reason", "runtime_reason", "end"), [
    ("max_output_tokens", "MODEL_RESPONSE_TRUNCATED", "max-tokens"),
    ("stream_eof", "MODEL_STREAM_INCOMPLETE", "error"),
    ("content_filter", "MODEL_RESPONSE_CONTENT_FILTERED", "error"),
    ("future_provider_reason", "MODEL_RESPONSE_INCOMPLETE", "error"),
    ("", "MODEL_RESPONSE_INCOMPLETE", "error"),
])
def test_responses_incomplete_reason_preserved(provider_reason, runtime_reason, end):
    fields = response_fields({"status": "incomplete", "incomplete_details": {"reason": provider_reason},
                              "output": []}, "synthetic")
    response = ModelResponse(backend="openai_responses", **fields)
    assert response.stop_reason == provider_reason
    assert response.runtime_reason == runtime_reason
    assert result_turn_end_reason(response) == end


def test_responses_malformed_tool_is_not_output_length_limit():
    fields = response_fields({"status": "completed", "output": [{"type": "function_call", "call_id": "c1",
        "name": "run_command", "arguments": "[]"}]}, "synthetic")
    response = ModelResponse(backend="openai_responses", **fields)
    assert response.runtime_reason == "MODEL_TOOL_ARGUMENTS_INVALID"
    assert result_turn_end_reason(response) == "error"
    assert response.tool_use_blocks == []


def test_canonical_boundary_never_promotes_truncated_calls():
    response = SimpleNamespace(text="", truncated=True, tool_use_blocks=[{
        "id": "c1", "name": "run_command", "input": {"command": "true"}}])
    result = canonical_tool_calls_from_response(_request(response, "native"))
    assert result.calls == ()
    assert result.violations == (), "截断是 provider 终态，不另触发模型修参重试"
