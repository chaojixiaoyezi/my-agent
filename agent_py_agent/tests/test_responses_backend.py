"""Responses 的 typed 工具、完整流边界、加密历史和用量校验。"""
import json

import pytest

from agent_py_agent.agent.backends import BackendOptions
from agent_py_agent.agent.backends.errors import ProviderResponseError
from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend
from agent_py_agent.agent.backends.responses_wire import (
    collect_response,
    input_items,
    response_fields,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice


def completed(output):
    return {"status": "completed", "output": output, "usage": {"input_tokens": 10, "output_tokens": 2,
        "input_tokens_details": {"cached_tokens": 8}}}


def test_responses_tool_round_payload(monkeypatch):
    backend = OpenAIResponsesBackend(BackendOptions("https://example.test/v1", "secret", "model", stream_enabled=False))
    captured = []
    obj = completed([{"type": "function_call", "call_id": "call-1", "name": "read", "arguments": '{"path":"a"}'}])
    monkeypatch.setattr(backend, "request_json", lambda path, payload, headers: captured.append((path, payload)) or obj)
    result = backend.generate("你好", tools=[{"name": "read", "input_schema": {"type": "object"}}], tool_choice=ToolChoice.specific("read"))
    assert result.tool_use_blocks[0]["input"] == {"path": "a"}
    assert captured[0][0] == "/responses"
    payload = captured[0][1]
    assert payload["tool_choice"] == {"type": "function", "name": "read"}
    assert payload["store"] is False and "messages" not in payload
    items = input_items("", [{"role": "assistant", "content": result.assistant_content_blocks},
                             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "hello"}]}], "", "model")
    assert [i["type"] for i in items] == ["function_call", "function_call_output"]
    assert items[0]["call_id"] == items[1]["call_id"]
    assert result.usage["input_tokens_details"]["cached_tokens"] == 8


def test_stream_requires_terminal_and_preserves_partial():
    out = []
    lines = [json.dumps({"type": "response.output_text.delta", "delta": "你好"})]
    result = response_fields(collect_response(lines, out.append, None), "model")
    assert out == ["你好"] and result["text"] == "你好" and result["truncated"]
    assert result["tool_use_blocks"] == []
    with pytest.raises(ProviderResponseError):
        collect_response([json.dumps({"type": "response.failed"})], None, None)
    thinking, closed = [], []
    def on_thinking(delta):
        thinking.append(delta)
    on_thinking.complete = closed.append
    events = [{"type": "response.reasoning_summary_text.delta", "delta": "先"},
              {"type": "response.output_item.added", "item": {"type": "reasoning"}},
              {"type": "response.reasoning_summary_text.delta", "delta": "后"},
              {"type": "response.output_text.delta", "delta": "正文"},
              {"type": "response.completed", "response": completed([])}]
    collect_response([json.dumps(e) for e in events], None, on_thinking)
    assert thinking == ["先", "后"] and closed == ["先后"]


def test_reasoning_encrypted_replay_is_model_scoped_and_not_text():
    obj = completed([{"type": "reasoning", "id": "rs-1", "encrypted_content": "cipher", "summary": []},
                     {"type": "message", "content": [{"type": "output_text", "text": "完成"}]}])
    result = response_fields(obj, "model-a")
    assert result["text"] == "完成"
    message = {"role": "assistant", "content": result["assistant_content_blocks"]}
    assert input_items("", [message], "", "model-a")[0]["encrypted_content"] == "cipher"
    assert "cipher" not in json.dumps(input_items("", [message], "", "model-b"))


@pytest.mark.parametrize("arguments", ['{"x":', '[]', 'null'])
def test_malformed_tools_never_execute(arguments):
    result = response_fields(completed([{"type": "function_call", "call_id": "id", "name": "tool", "arguments": arguments}]), "m")
    assert result["truncated"] and not result["tool_use_blocks"]


def test_explicit_temperature_sent_but_default_omitted(monkeypatch):
    sent = []
    for explicit in [False, True]:
        backend = OpenAIResponsesBackend(BackendOptions("https://example.test/v1", "key", "model", stream_enabled=False,
                                                        temperature=1, temperature_explicit=explicit))
        monkeypatch.setattr(backend, "request_json", lambda path, payload, headers: sent.append(payload) or completed([]))
        backend.generate("hello")
    assert "temperature" not in sent[0]
    assert sent[1]["temperature"] == 1


def test_duplicate_and_partial_item_cannot_execute():
    call = {"type": "function_call", "call_id": "same", "name": "read", "arguments": "{}"}
    for calls in [[call, call], [{**call, "status": "in_progress"}]]:
        result = response_fields(completed(calls), "model")
        assert result["truncated"] and not result["tool_use_blocks"]
