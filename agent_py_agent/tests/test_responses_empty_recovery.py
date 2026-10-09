"""wsempty：完整空输出须进入既有有界修复；WS 使用假连接，不接生产。"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from websockets.exceptions import ConnectionClosedError

from agent_py_agent.agent.agent_core import _tool_loop_service as service
from agent_py_agent.agent.agent_core import provider_transient_auto_resume as retry
from agent_py_agent.agent.agent_core.tool_loop.model_turn import ModelTurnRequest
from agent_py_agent.agent.backends import BackendOptions
from agent_py_agent.agent.backends import responses_websocket as ws
from agent_py_agent.agent.backends.errors import ProviderResponseError
from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend
from agent_py_agent.agent.backends.responses_wire import collect_response
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
from agent_py_agent.tests.test_responses_backend import completed
from agent_py_agent.tests.test_responses_websocket import (
    FakeConnection,
    completed_events,
    request,
)

TOOLS = [{"name": "read_file", "input_schema": {"type": "object"}}]


def backend_with_output(monkeypatch, output):
    backend = OpenAIResponsesBackend(BackendOptions("https://example.test/v1", "", "m"))
    monkeypatch.setattr(backend, "_send_responses_request", lambda *_args, **_kw: completed(output))
    return backend


@pytest.mark.parametrize("text", [None, "", " \n\t"])
def test_completed_empty_tool_response_has_typed_provider_failure(monkeypatch, text):
    output = [] if text is None else [{"type": "message", "content": [{"type": "output_text", "text": text}]}]
    backend = backend_with_output(monkeypatch, output)
    with pytest.raises(ProviderResponseError) as caught:
        backend.generate("继续", tools=TOOLS)
    assert caught.value.error_code == "MODEL_EMPTY_RESPONSE"


@pytest.mark.parametrize("item", [
    {"type": "reasoning", "encrypted_content": "opaque", "summary": []},
    {"type": "compaction", "encrypted_content": "opaque"},
    {"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": "{}"},
    {"type": "message", "channel": "commentary", "content": [{"type": "output_text", "text": "进度"}]},
    {"type": "message", "content": [{"type": "refusal", "refusal": "拒绝"}]},
])
def test_nonempty_or_native_continuation_items_keep_their_original_contract(monkeypatch, item):
    response = backend_with_output(monkeypatch, [item]).generate("继续", tools=TOOLS)
    assert response.usage["output_tokens"] == 2
    assert response.assistant_content_blocks
    assert response.runtime_reason == ""


def test_incomplete_or_invalid_arguments_are_not_reclassified_as_empty(monkeypatch):
    backend = backend_with_output(monkeypatch, [{"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": "{"}])
    assert backend.generate("继续", tools=TOOLS).runtime_reason == "MODEL_TOOL_ARGUMENTS_INVALID"
    monkeypatch.setattr(backend, "_send_responses_request", lambda *_a, **_kw: {
        "status": "incomplete", "incomplete_details": {"reason": "stream_eof"}, "output": [],
    })
    assert backend.generate("继续", tools=TOOLS).runtime_reason == "MODEL_STREAM_INCOMPLETE"


def test_auxiliary_no_tool_empty_response_preserves_existing_contract(monkeypatch):
    assert backend_with_output(monkeypatch, []).generate("辅助请求").text == ""


def install_ws_sampling(monkeypatch, connections):
    backend = OpenAIResponsesBackend(BackendOptions("https://example.test/v1", "", "m"))
    used = []

    def connect(_request):
        connection = connections[len(used)]
        used.append(connection)
        return connection

    def send(payload, _headers, request_info):
        lines = ws.iter_responses_websocket(replace(request(), payload=payload))
        try:
            return collect_response(lines, request_info.on_chunk, None)
        finally:
            lines.close()

    monkeypatch.setattr(ws, "_connect", connect)
    monkeypatch.setattr(backend, "_send_responses_request", send)
    monkeypatch.setattr(retry, "provider_transient_retry_delays", lambda _policy: (0.0,))
    monkeypatch.setattr(retry, "wait_interruptibly", lambda _delay: None)
    params = SimpleNamespace(effective_on_chunk=None, live_archive_state={},
                             executed_tools=["read_file"], tool_context=["已完成工具结果"])
    monkeypatch.setattr(service, "next_tool_loop_model_response", lambda *_args:
                        ModelTurnRequest("保留历史", backend.generate("继续", tools=TOOLS), params))
    monkeypatch.setattr(service, "stale_subagent_attempt_message", lambda *_args: None)
    monkeypatch.setattr(service, "begin_goal_model_turn", lambda *_args: None)
    monkeypatch.setattr(service, "account_goal_model_response", lambda *_args: None)
    monkeypatch.setattr(service, "acknowledge_injected_turn_input", lambda *_args: None)
    monkeypatch.setattr(service, "_pending_turn_input_supersedes_unusable_response", lambda *_args: False)
    monkeypatch.setattr(service, "build_tool_loop_prompt", lambda *_args: "保留历史")
    return SimpleNamespace(runtime_guard_policy=None), params, used


def test_disconnect_then_completed_empty_uses_bounded_repair_then_returns_final(monkeypatch):
    connections = [FakeConnection([ConnectionClosedError(None, None)]),
                   FakeConnection(completed_events("")), FakeConnection(completed_events("最终回复"))]
    agent, params, used = install_ws_sampling(monkeypatch, connections)
    outcome = service._model_turn_or_retry(agent, params, 192, 0)
    assert outcome.retry_after_provider_response and outcome.response is None
    assert outcome.provider_response_repairs == 1 and len(used) == 2
    assert params.tool_context[0] == "已完成工具结果" and len(params.tool_context) == 2
    recovered = service._model_turn_or_retry(agent, params, 192, 1)
    assert recovered.response.text == "最终回复" and not recovered.retry_after_provider_response
    assert len(used) == 3 and all(c.closed for c in connections)
    assert params.executed_tools == ["read_file"], "恢复不重放任何工具"


def test_repeated_completed_empty_exhausts_existing_one_repair_without_silent_final(monkeypatch):
    connections = [FakeConnection(completed_events("")), FakeConnection(completed_events(""))]
    agent, params, used = install_ws_sampling(monkeypatch, connections)
    outcome = service._model_turn_or_retry(agent, params, 159, 0)
    assert outcome.retry_after_provider_response
    with pytest.raises(ProviderResponseError) as caught:
        service._model_turn_or_retry(agent, params, 159, outcome.provider_response_repairs)
    assert caught.value.error_code == "MODEL_EMPTY_RESPONSE" and len(used) == 2


def run_real_loop(monkeypatch, tmp_path, endings):
    agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl"), tmp_path)
    (agent.effective_workspace_root / "notes.txt").write_text("已读取的唯一结果", encoding="utf-8")
    backend = OpenAIResponsesBackend(BackendOptions("https://example.test/v1", "", "m"))
    agent.backend = backend
    monkeypatch.setattr(backend, "probe_tool_capability", lambda: ProviderToolCapability(
        provider=backend.name, endpoint="local://fake", model="m", stream=True,
        native_supported=True, evidence="test_fake_native"))
    tool = {"type": "function_call", "call_id": "read-1", "name": "read_file", "arguments": '{"path":"notes.txt"}'}
    events = [json.dumps({"type": "response.completed", "response": completed([tool])})]
    connections = [FakeConnection(events), *endings]
    used = []

    def connect(_request):
        connection = connections[len(used)]
        used.append(connection)
        return connection

    def send(payload, _headers, request_info):
        lines = ws.iter_responses_websocket(replace(request(), payload=payload))
        try:
            return collect_response(lines, request_info.on_chunk, request_info.on_thinking_delta)
        finally:
            lines.close()

    monkeypatch.setattr(ws, "_connect", connect)
    monkeypatch.setattr(backend, "_send_responses_request", send)
    monkeypatch.setattr(retry, "provider_transient_retry_delays", lambda _policy: (0.0,))
    monkeypatch.setattr(retry, "wait_interruptibly", lambda _delay: None)
    result = agent.run("读取notes然后总结", save=False, allowed_tools=["read_file"])
    return result, used


def test_real_loop_sends_original_result_and_empty_repair_in_next_ws_payload(monkeypatch, tmp_path):
    result, used = run_real_loop(monkeypatch, tmp_path, [
        FakeConnection([ConnectionClosedError(None, None)]),
        FakeConnection(completed_events("")), FakeConnection(completed_events("最终回复")),
    ])
    assert result.response == "最终回复" and result.executed_tools == ["read_file"]
    assert len(used) == 4 and all(c.closed for c in used)
    payload = used[-1].sent[0]
    outputs = [item for item in payload["input"] if item.get("type") == "function_call_output"]
    assert len(outputs) == 1 and "已读取的唯一结果" in outputs[0]["output"]
    assert "上一轮模型接口返回了空文本" in json.dumps(payload["input"], ensure_ascii=False)


def test_real_loop_commentary_only_keeps_existing_plain_text_final_projection(monkeypatch, tmp_path):
    message = {"type": "message", "channel": "commentary", "content": [{"type": "output_text", "text": "过程说明"}]}
    events = [json.dumps({"type": "response.completed", "response": completed([message])})]
    result, used = run_real_loop(monkeypatch, tmp_path, [FakeConnection(events)])
    assert result.response == "过程说明" and result.executed_tools == ["read_file"]
    assert len(used) == 2, "当前合同未区分channel，不将非空commentary误报为空"
