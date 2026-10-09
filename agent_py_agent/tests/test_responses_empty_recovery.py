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
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability, ToolChoice
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


def probe_backend(monkeypatch, stream, output):
    from agent_py_agent.agent.backends import http

    backend = OpenAIResponsesBackend(BackendOptions("https://example.test/v1", "test-key", "m", stream_enabled=stream))
    obj = completed(output)

    def provider(request):
        assert "is_probe" not in request.payload, "宿主用途不改变供应商payload"
        return obj

    monkeypatch.setattr(http, "post_json", provider)
    monkeypatch.setattr(http, "post_stream_iter", lambda request: iter([
        json.dumps({"type": "response.completed", "response": provider(request)})]))
    requests = []
    generate = backend._generate
    monkeypatch.setattr(backend, "_generate", lambda request: requests.append(request) or generate(request))
    return backend, requests


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("output", [[], [{"type": "message", "content": [{"type": "output_text", "text": " \n"}]}]])
def test_tool_probe_empty_is_unproven_bounded_and_not_cached(monkeypatch, stream, output):
    backend, requests = probe_backend(monkeypatch, stream, output)
    for count in (3, 6):
        capability = backend.probe_tool_capability()
        assert not capability.native_supported
        assert capability.evidence == "live_probe_returned_no_valid_structured_tool_call"
        assert len(requests) == count
        assert all(request.is_probe for request in requests)
    previous = requests[-1]
    with pytest.raises(ProviderResponseError) as caught:
        backend.generate(previous.prompt, tools=previous.tools, tool_choice=previous.tool_choice)
    assert caught.value.error_code == "MODEL_EMPTY_RESPONSE"
    assert not requests[-1].is_probe, "相同提示与工具选择不能冒充探测用途；标记不污染后续请求"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("output", [[], [{"type": "message", "content": [{"type": "output_text", "text": " \n"}]}]])
def test_vision_probe_empty_is_inconclusive_bounded_and_not_cached(monkeypatch, stream, output):
    from agent_py_agent.agent.backends import vision_capability as vision

    backend, requests = probe_backend(monkeypatch, stream, output)
    monkeypatch.setattr(backend, "probe_tool_capability", lambda: SimpleNamespace(native_supported=True))
    vision.reset_vision_capability_cache()
    try:
        for count in (2, 4):
            result = vision.resolve_vision_capability(backend)
            assert result.status == vision.VISION_INCONCLUSIVE and result.evidence == "probe_no_tool_call"
            assert len(requests) == count
            assert all(request.is_probe for request in requests)
    finally:
        vision.reset_vision_capability_cache()


def test_probe_purpose_does_not_suppress_provider_errors(monkeypatch):
    from agent_py_agent.agent.backends.errors import ProviderQuotaExhaustedError
    from agent_py_agent.agent.backends.vision_capability import probe_vision_capability

    backend, requests = probe_backend(monkeypatch, False, [])
    failure = ProviderQuotaExhaustedError("synthetic quota")
    monkeypatch.setattr(backend, "_send_responses_request", lambda *_args: (_ for _ in ()).throw(failure))
    with pytest.raises(ProviderQuotaExhaustedError) as caught:
        backend.probe_tool_capability()
    assert caught.value is failure and len(requests) == 1
    monkeypatch.setattr(backend, "probe_tool_capability", lambda: SimpleNamespace(native_supported=True))
    with pytest.raises(ProviderQuotaExhaustedError) as caught:
        probe_vision_capability(backend)
    assert caught.value is failure and len(requests) == 2
    assert requests[-1].is_probe
    assert requests[-1].messages[0]["content"][1]["type"] == "image", "失败来自视觉请求，不是前置工具探针"


def test_tool_choice_reason_alone_does_not_exempt_business_empty(monkeypatch):
    backend = backend_with_output(monkeypatch, [])
    with pytest.raises(ProviderResponseError) as caught:
        backend.generate("Internal vision capability probe", tools=TOOLS,
                         tool_choice=ToolChoice.auto("vision_capability_probe"))
    assert caught.value.error_code == "MODEL_EMPTY_RESPONSE"


def test_legacy_vision_backend_does_not_receive_new_keyword():
    from agent_py_agent.agent.backends.vision_capability import (
        VISION_INCONCLUSIVE,
        probe_vision_capability,
    )

    class LegacyBackend:
        calls = 0

        def probe_tool_capability(self):
            return SimpleNamespace(native_supported=True)

        def generate(self, prompt, *, tools, tool_choice, messages):
            self.calls += 1
            return SimpleNamespace(tool_use_blocks=[])

    backend = LegacyBackend()
    assert probe_vision_capability(backend).status == VISION_INCONCLUSIVE
    assert backend.calls == 2, "旧后端无typed-options能力位，不把新关键字当解析失败"


def test_probe_purpose_is_local_to_request_and_absent_from_fake_ws_wire(monkeypatch):
    backend, requests = probe_backend(monkeypatch, True, [])
    connections = [FakeConnection(completed_events("")) for _ in range(3)]
    used = []
    monkeypatch.setattr(ws, "_connect", lambda _request: used.append(connections[len(used)]) or used[-1])

    def send(payload, _headers, info):
        lines = ws.iter_responses_websocket(replace(request(), payload=payload))
        try:
            return collect_response(lines, info.on_chunk, info.on_thinking_delta)
        finally:
            lines.close()

    monkeypatch.setattr(backend, "_send_responses_request", send)
    assert not backend.probe_tool_capability().native_supported
    assert len(requests) == len(used) == 3 and all(info.is_probe for info in requests)
    assert all("is_probe" not in connection.sent[0] for connection in used)
    assert all(connection.closed for connection in used)
