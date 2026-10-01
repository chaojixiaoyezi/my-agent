"""D5：一次能力包选择的辅助调用失败要留下无正文的结构化原因；选择 schema 只用严格模式都接受的关键字。

全部传输都是替身（fake Responses 事件流 / 抛出的供应商异常），不发网络、不调用真实模型。
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import BackendOptions
from agent_py_agent.agent.backends.errors import ProviderRequestRejectedError, ProviderResponseError
from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend
from agent_py_agent.agent.capability.package_selection import (
    build_package_selection_material,
    select_capability_packages,
)
from agent_py_agent.agent.capability.package_selection_failure import selection_failure_facts
from agent_py_agent.agent.capability.package_snapshot import CapabilityPackageSnapshot
from agent_py_agent.agent.capability_package_manifest import CapabilityFile
from agent_py_agent.agent.conversation.capability_selection_state import TaskCapabilitySelection
from agent_py_agent.agent.settings.config import AgentConfig

_SECRET = "provider said: secret prompt text leaked"
_REJECTED_DETAILS = {"status_code": 400, "provider_error": {"error": {
    "code": "invalid_json_schema", "type": "invalid_request_error", "param": "text.format.schema", "message": _SECRET}}}


# 函数用途: 制造只有公开元数据的候选包；选择阶段不得读包正文。
def _package(name="meeting-records"):
    def forbidden_read(_path):
        raise AssertionError("选择阶段不得读包正文")

    return CapabilityPackageSnapshot(
        package_id=name, version="0.2.0", summary="公开摘要", description="会议记录整理规范",
        keywords=("会议记录",), entry_document="CAPABILITY.md",
        package_sha256=hashlib.sha256(name.encode()).hexdigest(), activation_id="a" * 64,
        members=(CapabilityFile("CAPABILITY.md", "b" * 64),), reader=forbidden_read,
    )


# 函数用途: 生成一份可发送的选择材料。
def _material():
    return build_package_selection_material("整理会议记录", [_package("pkg-a"), _package("pkg-b")],
                                            max_input_tokens=3000, context_window_tokens=200000)


# 函数用途: 把结构化后端包成选择函数需要的最小 agent。
def _agent(backend):
    return SimpleNamespace(backend=backend, config=AgentConfig(model_name="fake-model"))


# 函数用途: 制造一个已领取的选择标记。
def _claimed():
    return TaskCapabilitySelection(status="claimed", claim_id="claim", request_id="request", run_id="run",
                                   attempt_id="attempt", candidate_digest="a" * 64, model_binding_digest="d" * 64)


def test_selection_schema_only_uses_strict_mode_keywords():
    schema = _material().response_schema
    selected = schema["properties"]["selected_ids"]
    assert selected == {"type": "array", "items": {"type": "string", "enum": selected["items"]["enum"]}}
    assert schema["additionalProperties"] is False and schema["required"] == ["selected_ids"]
    assert "uniqueItems" not in json.dumps(schema) and "maxItems" not in json.dumps(schema)


def test_duplicate_or_unknown_ids_are_still_rejected_locally():
    material = _material()
    ids = material.response_schema["properties"]["selected_ids"]["items"]["enum"]
    for text in (json.dumps({"selected_ids": [ids[0], ids[0]]}), json.dumps({"selected_ids": ["capability:nope"]})):
        backend = SimpleNamespace(generate_structured=lambda prompt, *, response_schema, messages=None, t=text:
                                  SimpleNamespace(text=t, truncated=False, tool_use_blocks=None,
                                                  tool_protocol_violations=None, runtime_status="ok", usage={}))
        result = select_capability_packages(_agent(backend), material)
        assert result.outcome == "failed" and result.error_code == "CAPABILITY_SELECTION_RESPONSE_INVALID"
        assert result.failure_facts == {}


def test_rejected_provider_error_records_codes_but_never_the_message(caplog):
    def rejected(prompt, *, response_schema, messages=None):
        raise ProviderRequestRejectedError(f"HTTP 400: {_SECRET}", status_code=400, details=_REJECTED_DETAILS)

    with caplog.at_level("WARNING", logger="agent.capability.package_selection"):
        result = select_capability_packages(_agent(SimpleNamespace(generate_structured=rejected)), _material(),
                                            request_id="request-1", run_id="run-1")
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "request=request-1" in logged and "provider_error_code=invalid_json_schema" in logged
    assert "secret" not in logged
    assert result.outcome == "failed" and result.error_code == "CAPABILITY_SELECTION_MODEL_FAILED"
    assert result.failure_facts == {
        "error_type": "ProviderRequestRejectedError", "error_code": "PROVIDER_REQUEST_REJECTED", "http_status": 400,
        "provider_error_code": "invalid_json_schema", "provider_error_type": "invalid_request_error",
        "provider_error_param": "text.format.schema",
    }
    assert "secret" not in json.dumps(result.failure_facts)


def test_free_text_or_out_of_range_values_are_dropped():
    exc = ProviderResponseError("boom", error_code="has spaces in it",
                                details={"status_code": 999, "provider_error": {"code": "x" * 81, "type": "ok_type"}})
    assert selection_failure_facts(exc) == {"error_type": "ProviderResponseError", "provider_error_type": "ok_type"}


# 函数用途: 订阅模式的 Responses 后端，事件流由用例给出；记录实际出站请求体。
def _subscription_backend(events):
    backend = OpenAIResponsesBackend(BackendOptions("https://chatgpt.example.test/backend-api/codex", "", "gpt-x",
                                                    stream_enabled=True))
    backend.auth_ref = {"mode": "chatgpt"}
    sent = []

    class _Lines:
        def __iter__(self):
            return iter(json.dumps(event) for event in events)

        def close(self):
            pass

    backend.request_stream_iter = (lambda path, payload, headers, first_event_timeout_seconds=None:
                                   sent.append(payload) or _Lines())
    return backend, sent


def test_subscription_responses_selection_sends_strict_compatible_schema_and_parses_result():
    material = _material()
    first_id = material.response_schema["properties"]["selected_ids"]["items"]["enum"][0]
    text = json.dumps({"selected_ids": [first_id]})
    backend, sent = _subscription_backend((
        {"type": "response.created", "response": {"status": "in_progress"}},
        {"type": "response.output_text.delta", "delta": text},
        {"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 50, "output_tokens": 8}}},
    ))
    result = select_capability_packages(_agent(backend), material)
    assert result.outcome == "selected" and result.failure_facts == {}
    fmt = sent[0]["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert "uniqueItems" not in json.dumps(fmt["schema"])


def test_subscription_responses_failed_event_is_recorded_as_structured_failure():
    backend, _sent = _subscription_backend((
        {"type": "response.created", "response": {"status": "in_progress"}},
        {"type": "response.failed", "response": {"status": "failed", "error": {"code": "server_error", "message": _SECRET}}},
    ))
    result = select_capability_packages(_agent(backend), _material())
    assert result.outcome == "failed" and result.error_code == "CAPABILITY_SELECTION_MODEL_FAILED"
    assert result.failure_facts["error_type"] == "ProviderResponseError"
    assert "secret" not in json.dumps(result.failure_facts)


def test_failure_is_persisted_only_for_failed_outcome_and_round_trips():
    facts = {"error_type": "ProviderRequestRejectedError", "http_status": 400, "provider_error_param": "text.format.schema"}
    failed = _claimed().finished(outcome="failed", warning_codes=("CAPABILITY_SELECTION_MODEL_FAILED",), failure=facts)
    payload = failed.to_dict()
    assert payload["failure"] == facts
    assert TaskCapabilitySelection.from_dict(payload) == failed
    empty = _claimed().finished(outcome="empty")
    assert "failure" not in empty.to_dict()
    assert TaskCapabilitySelection.from_dict(empty.to_dict()) == empty
    with pytest.raises(ValueError):
        _claimed().finished(outcome="empty", failure=facts)


@pytest.mark.parametrize("bad", [
    {"error_type": "has spaces"}, {"http_status": 99}, {"http_status": "400"}, {"unknown_key": "x"},
    {"provider_error_code": "x" * 81},
])
def test_failure_rejects_free_text_unknown_keys_and_bad_status(bad):
    with pytest.raises(ValueError):
        _claimed().finished(outcome="failed", failure=bad)
