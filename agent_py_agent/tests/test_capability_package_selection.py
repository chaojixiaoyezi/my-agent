"""一次能力选择只准备元数据与原辅助调用；全部模型传输为 fake，不运行包或真实 TUI。"""
from __future__ import annotations

import hashlib
import importlib
import json
from concurrent.futures import CancelledError
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.backends.base import ModelResponse, ProviderRequestOptions
from agent_py_agent.agent.backends.gateway_helpers import _emit_provider_attempt
from agent_py_agent.agent.capability.package_snapshot import CapabilityPackageSnapshot
from agent_py_agent.agent.capability_package_manifest import CapabilityFile
from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.conversation.auxiliary_model_call import (
    AuxiliaryModelCallRequest,
    generate_auxiliary_model_response,
)
from agent_py_agent.agent.memory_archive import estimate_tokens
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice


# LLM: 测试 reader 一旦触发即失败；候选准备没有任何资源读取权，不能靠 fake 正文掩盖越界。
# 函数用途: 制造公开元数据、准确引用和不可读取的私有成员。
def _package(name="example-a", description="整理输入并核对来源"):
    def forbidden_read(_path):
        raise AssertionError("选择阶段不得读包正文")

    return CapabilityPackageSnapshot(
        package_id=name, version="1.0.0", summary="公开摘要", description=description,
        keywords=("核对", "来源"), entry_document="CAPABILITY.md",
        package_sha256=hashlib.sha256(name.encode()).hexdigest(), activation_id="a" * 64,
        members=(CapabilityFile("CAPABILITY.md", "b" * 64),
                 CapabilityFile("methods/private.md", "c" * 64)), reader=forbidden_read,
    )


def _selection():
    return importlib.import_module("agent_py_agent.agent.capability.package_selection")


def _agent(backend):
    return SimpleNamespace(backend=backend, config=AgentConfig(model_name="fake-model"))


def test_auxiliary_structured_dispatch_keeps_real_schema_and_accounting():
    calls, observations = [], []
    response = ModelResponse('{"selected_ids":[]}', "fake", usage={"input_tokens": 19, "output_tokens": 4})

    def structured(prompt, *, response_schema, messages=None):
        calls.append((prompt, response_schema, messages))
        _emit_provider_attempt({"attempt_id": "http-1", "status": "finished", "http_status": 200})
        return response

    agent = _agent(SimpleNamespace(generate_structured=structured))
    schema = {"type": "object", "properties": {"selected_ids": {"type": "array"}}}
    request = AuxiliaryModelCallRequest(agent, "选择", response_schema=schema, request_id="request-1",
                                        run_id="run-1", task_id="task-1", thread_id="thread-1",
                                        purpose="capability_selection", on_observation=observations.append)
    assert generate_auxiliary_model_response(request) is response
    assert calls == [("选择", schema, None)]
    record, = agent._model_call_ledger.records()
    assert record.metadata["purpose"] == "capability_selection"
    assert record.metadata["task_id"] == "task-1"
    assert record.provider_attempt_count == observations[0].provider_http_attempt_count == 1
    assert observations[0].call_id == record.call_id
    assert record.output_tokens == 4
    assert "选择" not in json.dumps(record.metadata, ensure_ascii=False)


def test_auxiliary_input_estimate_includes_schema_without_changing_plain_estimate(monkeypatch):
    from agent_py_agent.agent.conversation import auxiliary_model_call as auxiliary

    inputs = []
    monkeypatch.setattr(auxiliary, "estimate_tokens", lambda value: inputs.append(value) or 1)
    response = ModelResponse("{}", "fake")
    backend = SimpleNamespace(generate=lambda *_a, **_k: response, generate_structured=lambda *_a, **_k: response)
    agent = _agent(backend)
    generate_auxiliary_model_response(AuxiliaryModelCallRequest(agent, "普通"))
    generate_auxiliary_model_response(AuxiliaryModelCallRequest(agent, "结构", response_schema={"type": "object"}))
    assert inputs[0] == {"prompt": "普通", "messages": [], "tools": [], "system_instruction": ""}
    assert inputs[1]["response_schema"] == {"type": "object"}


def test_auxiliary_plain_path_preserves_messages_tools_system_and_response():
    calls = []
    response = ModelResponse("摘要", "fake")

    def generate(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return response

    request = AuxiliaryModelCallRequest(_agent(SimpleNamespace(generate=generate)), "全文",
                                        messages=[{"role": "user", "content": "原文"}],
                                        tools=[{"name": "read_file"}], system_instruction="缓存规则")
    assert generate_auxiliary_model_response(request) is response
    assert calls[0][1]["messages"] == request.messages
    assert calls[0][1]["tools"] == request.tools
    assert calls[0][1]["request_options"].system_instruction == "缓存规则"
    assert "response_schema" not in calls[0][1]


@pytest.mark.parametrize("protocol,base,mode", [
    ("openai_compatible", "https://example.test/v1", "native"),
    ("openai_compatible", "https://api.deepseek.com", "auto"),
    ("anthropic_compatible", "https://example.test", "auto"),
    ("openai_responses", "https://example.test/v1", "auto"),
])
def test_auxiliary_three_protocols_use_existing_structured_mode(monkeypatch, protocol, base, mode):
    config = AgentConfig(model_backend=protocol, model_name="fake-model", api_base=base,
                         api_key="fake", stream_enabled=False, model_structured_output=mode)
    backend = get_backend(protocol, config)
    captured = []

    def fake_request(path, payload, headers):
        captured.append(payload)
        if protocol == "anthropic_compatible":
            return {"content": [{"type": "tool_use", "id": "envelope-1", "name": "my_agent_structured_output",
                                 "input": {"selected_ids": []}}], "stop_reason": "tool_use"}
        if protocol == "openai_responses":
            return {"status": "completed", "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": '{"selected_ids":[]}'}]}]}
        return {"choices": [{"message": {"content": '{"selected_ids":[]}'}, "finish_reason": "stop"}]}

    monkeypatch.setattr(backend, "request_json", fake_request)
    material = _selection().build_package_selection_material("核对资料", [_package()])
    result = _selection().select_capability_packages(SimpleNamespace(backend=backend, config=config), material)
    assert result.outcome == "empty"
    assert len(captured) == 1
    payload = captured[0]
    if protocol == "anthropic_compatible":
        assert payload["tools"][0]["input_schema"] == material.response_schema
    elif protocol == "openai_responses":
        assert payload["text"]["format"]["schema"] == material.response_schema
    elif base == "https://api.deepseek.com":
        assert payload["response_format"] == {"type": "json_object"}
        assert "selected_ids" in json.dumps(payload["messages"])
    else:
        assert payload["response_format"]["json_schema"]["schema"] == material.response_schema


def test_material_freezes_complete_public_candidates_and_exact_refs():
    package = _package()
    material = _selection().build_package_selection_material("帮我核对资料", [package])
    assert material.error_code == ""
    assert material.candidate_refs == (package.to_ref(),)
    assert "methods/private.md" not in material.prompt
    assert "CAPABILITY.md" not in material.prompt
    assert material.estimated_input_tokens <= 3000
    assert material.candidate_digest == _selection().build_package_selection_material("帮我核对资料", [package]).candidate_digest
    material.candidate_refs[0]["activation_id"] = "f" * 64
    material.response_schema["properties"].clear()
    assert material.candidate_refs == (package.to_ref(),)
    assert "selected_ids" in material.response_schema["properties"]
    with pytest.raises(FrozenInstanceError):
        material.prompt = "伪造"


def test_input_budget_never_silently_truncates_user_facts_or_partial_candidate():
    build = _selection().build_package_selection_material
    assert build("资料" * 1000, [_package()], max_input_tokens=100).error_code == "CAPABILITY_SELECTION_INPUT_TOO_LARGE"
    packages = [_package("oversized", "说明" * 6000), _package("small")]
    material = build("核对", packages, max_input_tokens=800, candidate_limit=0)
    assert material.candidate_refs == (packages[1].to_ref(),)
    assert material.omitted_candidates == 1
    assert material.estimated_input_tokens <= 800
    assert "oversized" not in material.prompt
    bounded = build("核对", [_package()], max_input_tokens=0, context_window_tokens=50)
    assert bounded.error_code
    assert build("核对", [_package()], max_input_tokens=0).error_code == "CAPABILITY_SELECTION_BUDGET_UNAVAILABLE"


def test_candidate_limit_zero_retains_budget_and_missing_candidates_never_call_model():
    build = _selection().build_package_selection_material
    packages = [_package(str(index)) for index in range(6)]
    limited = build("核对", packages, candidate_limit=1)
    assert len(limited.candidate_refs) == 1 and limited.omitted_candidates == 5
    unlimited = build("核对", packages, candidate_limit=0, max_input_tokens=4000)
    assert len(unlimited.candidate_refs) == 6
    agent = _agent(SimpleNamespace(generate_structured=lambda *_a, **_k: pytest.fail("不能发送空候选")))
    result = _selection().select_capability_packages(agent, build("核对", []))
    assert result.outcome == "failed" and result.error_code == "CAPABILITY_SELECTION_NO_CANDIDATES"


@pytest.mark.parametrize("text,kwargs", [
    ('{"selected_ids":["capability:unknown"]}', {}),
    ('{"selected_ids":["capability:example-a","capability:example-a"]}', {}),
    ('{"selected_ids":[],"selected_ids":[]}', {}),
    ('{"selected_ids":[],"extra":"private-material"}', {}),
    ('{"selected_ids":"capability:example-a"}', {}),
    ('{"selected_ids":[1]}', {}),
    ('```json\n{"selected_ids":[]}\n```', {}),
    ('{"selected_ids":[]}', {"truncated": True}),
    ('{"selected_ids":[]}', {"tool_use_blocks": [{"name": "write_file"}]}),
    ('{"selected_ids":[]}', {"tool_protocol_violations": [{"code": "invalid_envelope"}]}),
    ('{"selected_ids":[]}', {"runtime_status": "failed"}),
])
def test_selector_rejects_invalid_result_without_retry_or_diagnostic_body(text, kwargs):
    calls = []

    def structured(*_a, **_k):
        calls.append(1)
        return ModelResponse(text, "fake", **kwargs)

    material = _selection().build_package_selection_material("原始私有业务", [_package()])
    result = _selection().select_capability_packages(_agent(SimpleNamespace(generate_structured=structured)), material)
    assert result.outcome == "failed" and result.error_code
    assert calls == [1] and result.selected_refs == ()
    assert "private-material" not in repr(result) and "原始私有业务" not in repr(result)


@pytest.mark.parametrize("selected", [[], ["capability:example-b", "capability:example-a"]])
def test_selector_returns_original_refs_or_explicit_empty_without_history_writes(selected):
    packages = [_package(), _package("example-b")]
    material = _selection().build_package_selection_material("核对资料", packages)
    response = ModelResponse(json.dumps({"selected_ids": selected}), "fake")
    agent = _agent(SimpleNamespace(generate_structured=lambda *_a, **_k: response))
    agent.history = [{"role": "assistant", "content": "原业务"}]
    result = _selection().select_capability_packages(agent, material)
    assert result.outcome == ("selected" if selected else "empty")
    assert result.selected_refs == tuple(next(p.to_ref() for p in packages if p.stable_id == key) for key in selected)
    assert agent.history == [{"role": "assistant", "content": "原业务"}]
    assert "CAPABILITY_SELECTION_USAGE_NOT_REPORTED" in result.warning_codes


@pytest.mark.parametrize("error", [InterruptedError("stop"), KeyboardInterrupt(), CancelledError(),
                                 ToolCancelled("stop"), RuntimeError("private-key-like-text")])
def test_selector_propagates_cancellation_but_types_optional_failures(error):
    def structured(*_a, **_k):
        raise error

    agent = _agent(SimpleNamespace(generate_structured=structured))
    material = _selection().build_package_selection_material("核对", [_package()])
    if isinstance(error, (InterruptedError, KeyboardInterrupt, CancelledError, ToolCancelled)):
        with pytest.raises(type(error)):
            _selection().select_capability_packages(agent, material)
    else:
        result = _selection().select_capability_packages(agent, material)
        assert result.outcome == "failed" and "private-key-like-text" not in repr(result)


def test_selector_reports_multiple_http_usage_as_incomplete_without_fabricating_totals():
    def structured(*_a, **_k):
        for index in range(2):
            _emit_provider_attempt({"attempt_id": f"http-{index}", "status": "started"})
            _emit_provider_attempt({"attempt_id": f"http-{index}", "status": "finished", "http_status": 200})
        return ModelResponse('{"selected_ids":[]}', "fake", usage={"input_tokens": 7, "output_tokens": 2})

    agent = _agent(SimpleNamespace(generate_structured=structured))
    result = _selection().select_capability_packages(agent, _selection().build_package_selection_material("核对", [_package()]))
    assert result.outcome == "empty" and result.provider_http_attempt_count == 2
    assert "CAPABILITY_SELECTION_USAGE_INCOMPLETE" in result.warning_codes
    record, = agent._model_call_ledger.records()
    assert record.call_id == result.call_id
    assert record.accounted_input_tokens == 7 and record.output_tokens == 2


def test_auxiliary_schema_estimate_can_be_observed_without_response_usage():
    response = ModelResponse("{}", "fake")
    agent = _agent(SimpleNamespace(generate_structured=lambda *_a, **_k: response))
    schema = {"type": "object", "description": "x" * 1000}
    generate_auxiliary_model_response(AuxiliaryModelCallRequest(agent, "x", response_schema=schema))
    record, = agent._model_call_ledger.records()
    assert record.input_tokens >= estimate_tokens(schema)


def test_auxiliary_unsupported_schema_is_rejected_before_io_without_retry():
    calls, observations = [], []

    def structured(prompt):
        calls.append(prompt)
        return ModelResponse("{}", "fake")

    agent = _agent(SimpleNamespace(generate_structured=structured))
    with pytest.raises(TypeError, match="response_schema"):
        generate_auxiliary_model_response(AuxiliaryModelCallRequest(
            agent, "x", response_schema={}, on_observation=observations.append,
        ))
    assert calls == [] and observations[0].provider_http_attempt_count == 0
    assert agent._model_call_ledger.records()[0].status == "failed"


def test_auxiliary_broken_observer_does_not_change_success():
    response = ModelResponse("{}", "fake")

    def observer(_observation):
        raise ValueError("private-observer-value")

    agent = _agent(SimpleNamespace(generate_structured=lambda *_a, **_k: response))
    assert generate_auxiliary_model_response(AuxiliaryModelCallRequest(
        agent, "x", response_schema={}, on_observation=observer,
    )) is response


def test_anthropic_existing_schema_retry_keeps_only_reported_usage_and_warns(monkeypatch):
    config = AgentConfig(model_backend="anthropic_compatible", api_base="https://example.test",
                         api_key="fake", model_name="fake-model", stream_enabled=False)
    backend = get_backend(config.model_backend, config)
    captured = []

    def request_json(path, payload, headers):
        captured.append(payload)
        _emit_provider_attempt({"attempt_id": f"http-{len(captured)}", "status": "finished", "http_status": 200})
        if len(captured) == 1:
            return {"content": [{"type": "text", "text": "没有使用信封"}],
                    "stop_reason": "end_turn", "usage": {"input_tokens": 100, "output_tokens": 10}}
        return {"content": [{"type": "tool_use", "id": "selection", "name": "my_agent_structured_output",
                             "input": {"selected_ids": []}}], "stop_reason": "tool_use",
                "usage": {"input_tokens": 7, "output_tokens": 2}}

    monkeypatch.setattr(backend, "request_json", request_json)
    agent = SimpleNamespace(backend=backend, config=config)
    result = _selection().select_capability_packages(agent, _selection().build_package_selection_material("核对", [_package()]))
    assert result.outcome == "empty" and result.provider_http_attempt_count == len(captured) == 2
    assert "CAPABILITY_SELECTION_USAGE_INCOMPLETE" in result.warning_codes
    record, = agent._model_call_ledger.records()
    assert record.accounted_input_tokens == 7 and record.output_tokens == 2


def test_deepseek_selection_keeps_following_business_history_and_effort_high(monkeypatch):
    config = AgentConfig(model_backend="openai_compatible", api_base="https://api.deepseek.com",
                         model_name="deepseek-v4-flash", api_key="fake", stream_enabled=False)
    backend = get_backend(config.model_backend, config)
    payloads = []

    def request_json(path, payload, headers):
        payloads.append(payload)
        return {"choices": [{"message": {"content": '{"selected_ids":[]}', "reasoning_content": "推理"},
                             "finish_reason": "stop"}]}

    monkeypatch.setattr(backend, "request_json", request_json)
    messages = [{"role": "user", "content": "原业务"}]
    agent = SimpleNamespace(backend=backend, config=config, history=messages)
    _selection().select_capability_packages(agent, _selection().build_package_selection_material("核对", [_package()]))
    tools = [{"name": "read_file", "description": "读资料", "input_schema": {"type": "object", "properties": {}}}]
    for _ in range(2):
        backend.generate("原业务", messages=messages, tools=tools, tool_choice=ToolChoice.auto(),
                         request_options=ProviderRequestOptions(reasoning_effort="high"))
    assert len(payloads) == 3
    assert payloads[0]["response_format"] == {"type": "json_object"}
    for payload in payloads[1:]:
        assert payload["reasoning_effort"] == "high"
        assert payload.get("thinking") != {"type": "disabled"}
        assert "selected_ids" not in json.dumps(payload["messages"])
    assert agent.history == [{"role": "user", "content": "原业务"}]


@pytest.mark.parametrize("limits", [{"max_input_tokens": -1}, {"candidate_limit": True}, {"context_window_tokens": -2}])
def test_bad_budget_cannot_silently_disable_limits(limits):
    material = _selection().build_package_selection_material("核对", [_package()], **limits)
    assert material.error_code == "CAPABILITY_SELECTION_BUDGET_INVALID"


def test_duplicate_candidate_identity_is_rejected_before_model():
    assert _selection().build_package_selection_material("核对", [_package(), _package()]).error_code == "CAPABILITY_SELECTION_CANDIDATE_CONFLICT"


def test_failed_material_keeps_a_deterministic_structural_digest_for_once_claim():
    first = _selection().build_package_selection_material("私有事实" * 1000, [_package()], max_input_tokens=50)
    second = _selection().build_package_selection_material("另一份私有事实" * 1000, [_package()], max_input_tokens=50)
    assert first.error_code == second.error_code == "CAPABILITY_SELECTION_INPUT_TOO_LARGE"
    assert len(first.candidate_digest) == 64 and int(first.candidate_digest, 16) >= 0
    assert first.candidate_digest == second.candidate_digest
    assert not first.prompt and first.candidate_refs == ()
