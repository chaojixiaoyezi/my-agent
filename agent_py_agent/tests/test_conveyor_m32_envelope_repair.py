"""M32 走原工具、后台回执和 Curator 边界验证首次纠错。"""

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core._tool_loop_service import _natural_user_reply_step
from agent_py_agent.agent.agent_core.capability_request_tool import (
    build_capability_request_model_spec,
)
from agent_py_agent.agent.agent_core.tool_loop import background_public_receipt as receipt
from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
    natural_user_reply_model_params,
    pending_natural_user_reply,
)
from agent_py_agent.agent.common.structured_output import parse_structured_envelope
from agent_py_agent.agent.contracts.tool_input_schema import validate_tool_input
from agent_py_agent.agent.memory_store.curator_backend import extract_with_retries
from agent_py_agent.agent.memory_store.curator_formal import CuratorFormalMemoryInput
from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig
from agent_py_agent.tests.test_background_public_receipt_contract import _body, _params, _response
from agent_py_agent.tests.test_curator_timeout_adaptive import (
    _batch,
    _config,
    _conversation,
    _ScriptedBackend,
    _service,
    _valid_output,
)
from agent_py_agent.tests.test_subagent_capability_request_tool import (
    _agent_with_current_run,
    _request_through_operation_ledger,
)

_PRIVATE = "SYNTHETIC_PRIVATE_VALUE"
_ROOT_LIST = [("$.constraints", "list", "object<string,string>")]
_ITEM_LIST = [("$.constraints.item", "list", "string")]


def _assert_first_correction(result, record, manager, task, expected):
    assert result.error_code == "CAPABILITY_REQUEST_INVALID"
    assert result.effect_outcome == "not_started"
    assert record is None
    assert manager.load(task.id).capability_requests == []
    details = result.metadata["error_details"]
    assert (details["json_path"], details["actual_type"], details["expected_type"]) == expected[0]
    assert details["effect_outcome"] == "not_started"
    assert details["recorded"] is False
    assert details["actual_summary"]
    assert _PRIVATE not in json.dumps(details)
    assert [issue["path"] for issue in details["issues"]] == [path for path, _, _ in expected]
    spec = build_capability_request_model_spec()
    assert spec.input_schema["required"] == ["problem"]
    assert details["example"] == json.loads(spec.hints.examples[0])
    assert validate_tool_input(details["example"], spec.input_schema).ok


def test_constraints_list_has_a_structured_first_correction(tmp_path):
    agent, manager, task = _agent_with_current_run(tmp_path)
    result, record = _request_through_operation_ledger(tmp_path, agent, {
        "problem": "读取任务记录", "requested_tools": ["read_file"], "constraints": [_PRIVATE],
    })
    assert result.metadata.get("error_details", {}).get("json_path") == "$.constraints"
    _assert_first_correction(result, record, manager, task, _ROOT_LIST)


@pytest.mark.parametrize(("constraints", "expected"), [
    pytest.param({"allow_write_only_probe_dir": True, "forbid_forbidden_write_roots": False}, [
        ("$.constraints.allow_write_only_probe_dir", "boolean", "string"),
        ("$.constraints.forbid_forbidden_write_roots", "boolean", "string"),
    ], id="D02"),
    pytest.param({"no_modify_other_modules": True, "only_write": [_PRIVATE]}, [
        ("$.constraints.no_modify_other_modules", "boolean", "string"),
        ("$.constraints.only_write", "list", "string"),
    ], id="D03"),
    pytest.param([_PRIVATE, "assigned_root"], _ROOT_LIST, id="D04"),
    pytest.param({"item": [_PRIVATE]}, _ITEM_LIST, id="D05"),
    pytest.param({"item": [_PRIVATE, "fixture-2"]}, _ITEM_LIST, id="D06"),
    pytest.param({"item": [_PRIVATE, "fixture-3"]}, _ITEM_LIST, id="D07"),
    pytest.param({"item": [_PRIVATE, "fixture-4"]}, _ITEM_LIST, id="D08"),
    pytest.param({"item": [_PRIVATE, "fixture-5"]}, _ITEM_LIST, id="D09"),
    pytest.param([_PRIVATE, "fixture-other"], _ROOT_LIST, id="D10"),
])
def test_reported_constraint_shapes_get_a_structured_first_correction(
    tmp_path, constraints, expected,
):
    agent, manager, task = _agent_with_current_run(tmp_path)
    result, record = _request_through_operation_ledger(tmp_path, agent, {
        "problem": "读取任务记录", "requested_tools": ["read_file"], "constraints": constraints,
    })
    _assert_first_correction(result, record, manager, task, expected)


def test_corrected_open_constraints_do_not_bypass_authorization(tmp_path):
    agent, manager, task = _agent_with_current_run(tmp_path)
    arguments = {"problem": "需要能访问合成外部服务", "capability_type": "mcp",
                 "requested_mcp_tools": ["mcp__synthetic_service__read"],
                 "constraints": {"service": [_PRIVATE]}}
    rejected, record = _request_through_operation_ledger(tmp_path, agent, arguments)
    expected = [("$.constraints.service", "list", "string")]
    _assert_first_correction(rejected, record, manager, task, expected)

    arguments["constraints"] = {"service": "synthetic-service", "unknown_constraint_key": "allowed"}
    result, record = _request_through_operation_ledger(tmp_path, agent, arguments)
    assert result.ok and record is not None
    request, = manager.load(task.id).capability_requests
    assert request.status == "OPEN"
    assert request.constraints == arguments["constraints"]


def test_missing_curator_conflicts_gets_one_repair(tmp_path):
    store, thread, messages = _conversation(tmp_path, 1)
    message_id = messages[0].message_id
    draft = _valid_output(thread.thread_id, [message_id])
    draft["candidates"][0].pop("conflicts_with")
    path = "$.candidates[0].conflicts_with"
    backend = _ScriptedBackend([draft, {path: []}])
    service = _service(tmp_path, backend, store, config=_config(retention_scan_slice_ms=5000))
    seen = []
    backend.on_call = lambda _index: seen.append(dict(service.state_store.load().per_thread_cursors))

    result = service.run(reason="admin")

    assert result.status == "succeeded", result
    assert len(backend.prompts) == 2 and seen == [{}, {}]
    repair_prompt = backend.prompts[1]
    assert repair_prompt.startswith(backend.prompts[0])
    assert path in repair_prompt and "[]" in repair_prompt and "mem-example-conflicting-entry" in repair_prompt
    assert service.state_store.load().per_thread_cursors == {thread.thread_id: message_id}
    candidate, = service.candidate_service.list()
    assert not candidate.conflicts_with
    assert candidate.content == draft["candidates"][0]["content"]


@pytest.mark.parametrize(("max_retries", "repair"), [
    pytest.param(0, None, id="budget-exhausted"),
    pytest.param(2, {"$.candidates[0].conflicts_with": ["unseen-memory"]}, id="unverified-conflict"),
    pytest.param(2, {"$.candidates[0].conflicts_with": [], "content": "改写原稿"}, id="extra-field"),
])
def test_curator_invalid_repair_preserves_cursor_and_eligibility(tmp_path, max_retries, repair):
    store, thread, messages = _conversation(tmp_path, 1)
    message_id = messages[0].message_id
    draft = _valid_output(thread.thread_id, [message_id])
    draft["candidates"][0].pop("conflicts_with")
    backend = _ScriptedBackend([draft] if repair is None else [draft, repair])
    service = _service(tmp_path, backend, store, config=_config(
        max_retries=max_retries, retention_scan_slice_ms=5000,
    ))

    assert service.run(reason="admin").failure_code == "CURATOR_SCHEMA_INVALID"
    assert len(backend.prompts) == (1 if repair is None else 2)
    assert service.state_store.load().per_thread_cursors == {}
    assert not service.candidate_service.list()

    follow_up = _ScriptedBackend([_valid_output(thread.thread_id, [message_id])])
    replay = _service(tmp_path, follow_up, store, config=_config(retention_scan_slice_ms=5000))
    assert replay.run(reason="admin").status == "succeeded"
    assert len(follow_up.prompts) == 1
    assert replay.state_store.load().per_thread_cursors == {thread.thread_id: message_id}


def test_curator_repair_keeps_explicit_conflict_from_frozen_batch():
    formal = CuratorFormalMemoryInput(
        authority_type="long_term", authority_id="formal-test-id", authority_ref="ref-test",
        content_preview="已登记的事实", content_hash="test-hash", subject_key="profile.os",
        scope_type="personal", scope_key="", updated_at="2026-11-19", authority_version=1,
    )
    batch = replace(_batch(1), formal_memories=(formal,))
    message = batch.messages[0]
    draft = _valid_output(message.thread_id, [message.message_id])
    draft["candidates"][0].pop("conflicts_with")
    backend = _ScriptedBackend([draft, {"$.candidates[0].conflicts_with": [formal.authority_id]}])

    attempt = extract_with_retries(backend, MemoryCuratorConfig(timeout_seconds=30, max_retries=1), batch)

    assert attempt.batch is batch
    assert attempt.extraction.candidates[0].conflicts_with == (formal.authority_id,)
    assert attempt.extraction.candidates[0].content == draft["candidates"][0]["content"]
    assert len(backend.prompts) == 2
    assert formal.authority_id in backend.prompts[1]


def test_mixed_background_response_requests_one_repair():
    params = _params()
    params.archive_tool_calls.append({
        "tool": "read_file", "call_id": "tool-call-1", "ok": True,
        "tool_result_envelope": {"effect_outcome": "completed"},
    })
    response, _ = _response(params, "说明：\n" + _body(public="初始响应"))
    action, rejected = receipt.prepare_background_public_receipt(params, response)
    assert action == "repair"
    assert rejected.text == "" and rejected.public_receipt is None
    facts = pending_natural_user_reply(params)["facts"]
    assert facts["expected_fields"] == ["schema", "internal_result", "notification_intent", "public_reply"]
    assert facts["expected_type"] == "object"
    assert facts["tool_result_facts"][0]["effect_outcome"] == "completed"
    model_params = natural_user_reply_model_params(params)
    assert model_params.allowed_tools == []
    assert "初始响应" not in model_params.user_prompt
    repair, _ = _response(params, _body(public="修复后的回执"),
                          purpose="background_receipt_repair", call_id="call-2")

    verdict, final = _natural_user_reply_step(None, params, repair)

    assert verdict == "finish" and final.text == "修复后的回执"
    assert final.public_receipt["execution_source"]["call_id"] == "call-1"
    assert final.public_receipt["public_source"]["call_id"] == "call-2"
    assert pending_natural_user_reply(params) is None
    assert receipt.prepare_background_public_receipt(params, response)[0] == "finish"
    assert pending_natural_user_reply(params) is None


@pytest.mark.parametrize("tool_call", [False, True])
def test_bad_background_repair_cannot_publish_or_retry(tool_call):
    params = _params()
    response, _ = _response(params, "前言\n" + _body())
    assert receipt.prepare_background_public_receipt(params, response)[0] == "repair"
    repaired_text = _body() if tool_call else "说明\n" + _body()
    repair, _ = _response(params, repaired_text, purpose="background_receipt_repair", call_id="call-2")
    if tool_call:
        repair.tool_use_blocks = [{"id": "unexecuted", "name": "write_file", "input": {}}]

    verdict, final = _natural_user_reply_step(None, params, repair)

    assert verdict == "finish" and final.text == "" and final.runtime_status == "failed"
    assert final.public_receipt is None and pending_natural_user_reply(params) is None
    assert not params.archive_tool_calls


@pytest.mark.parametrize("origin", ["structured_output", "tool_result_envelope"])
def test_structured_background_receipt_precedes_mixed_text(origin):
    params = _params()
    response, _ = _response(params, "前言\n" + _body(public="未校验正文"))
    payload = json.loads(_body(public="结构化回执"))
    if origin == "structured_output":
        response.structured_output = payload
    else:
        params.archive_tool_calls.append({"ok": True, "tool_result_envelope": {"background_result": payload}})

    action, final = receipt.prepare_background_public_receipt(params, response)

    assert action == "finish" and final.text == "结构化回执"
    assert pending_natural_user_reply(params) is None


@pytest.mark.parametrize("text", [
    '前言 {"schema":"valid"}', '{"schema":"valid"} 后记', '{"schema":"valid"} {"schema":"valid"}',
    '{"schema":"valid","schema":"other"}', '{"schema":NaN}', '{"other":{}}',
])
def test_structured_envelope_never_extracts_arbitrary_braces(text):
    payload, error = parse_structured_envelope(text, expected_fields=("schema",))
    assert payload is None and error
