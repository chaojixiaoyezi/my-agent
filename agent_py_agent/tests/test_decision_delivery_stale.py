# LLM: J10 沿原 canonical 结果、归档、验证账与决策 worker 测试；只替换 Jev 后端，不执行真实供应商请求。
# 模块用途: 核对写入后多个验证焦点过期时的软提示、每条记录一次、失效与 text/native 同源展示。
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service as loop
from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
from agent_py_agent.agent.agent_core.tool_context import decision_delivery_quality as module
from agent_py_agent.agent.agent_core.tool_context.reducer import render_tool_result_for_live_prompt
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult
from agent_py_agent.agent.verification.runtime import record_tool_verification
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
)
from agent_py_agent.tests.test_decision_delivery_quality import (
    _HINT_11,
    _OTHER_ROOT,
    _ROOT,
    archive_row,
    install,
)
from agent_py_agent.tests.test_decision_delivery_quality import prepared as prepared
from agent_py_agent.tests.test_decision_delivery_quality_integration import (
    install_backend,
    project_at,
    verified_host,
)
from agent_py_agent.tests.test_decision_external_material_order import isolate_recording


# LLM: 当前写入的验证状态与归档信封共用原对象；已有三种验证均改为 passed，避免旧失败资格掩盖新触发。
# 函数用途: 把原验证样本续接为一次成功文件写入，默认令同项目三个焦点过期。
def write_record(prepared, tool="edit_file"):
    host, previous, _archive = prepared
    params = previous.params
    params.archive_tool_calls[:] = [row for row in params.archive_tool_calls if row["tool"] == "run_command"]
    for row in params.archive_tool_calls:
        row["tool_result_envelope"]["verification_evidence"].update(status="passed", exit_code=0)
    state = [{"status": "stale", "root": _ROOT, "changed_paths": [_ROOT + "/login.py"],
              "last_verification_id": 13, "last_verification_status": "passed"}]
    call = canonical_history_call(tool, {"path": _ROOT + "/login.py"}, call_id="write-4", run_id="run-1",
                                  turn_id="turn-4", attempt_id="attempt-1")
    result = canonical_history_result(call, "已修改 private-output", handler_details={"verification_state": state})
    archive = archive_row(call.call_id, tool, {"verification_state": result.metadata["handler_details"]["verification_state"]})
    params.archive_tool_calls.append(archive)
    return host, replace(previous, call=call, result=result), archive


@pytest.fixture
def stale_write(prepared):
    return write_record(prepared)


@pytest.mark.parametrize("tool", ["write_file", "edit_file", "apply_patch"])
def test_successful_write_triggers_one_stale_review(stale_write, monkeypatch, tool):
    host, record, archive = stale_write
    record = replace(record, call=replace(record.call, tool_name=tool), result=replace(record.result, tool_name=tool))
    archive["tool"] = tool
    before = deepcopy((record.result.to_dict(), record.params.archive_tool_calls))
    calls = install(monkeypatch)
    hint = module.delivery_quality_hint(host, record, archive)
    assert hint.endswith("#11（test/targeted，passed，其后有修改）；范围与结果以原事实为准，targeted 不代表全量。")
    assert len(calls) == 1 and calls[0][2]["source_refs"] == (archive["scoped_call_id"],)
    assert all(row["edited_after"] for row in calls[0][2]["state"]["focuses"])
    assert (record.result.to_dict(), record.params.archive_tool_calls) == before


def test_only_one_stale_focus_does_not_open_a_decision_stage(stale_write, monkeypatch):
    host, record, archive = stale_write
    record.params.archive_tool_calls[0]["tool_result_envelope"]["verification_evidence"]["root"] = _OTHER_ROOT
    record.params.archive_tool_calls[1]["tool_result_envelope"]["verification_evidence"]["root"] = _OTHER_ROOT
    calls = install(monkeypatch)
    monkeypatch.setattr(module, "begin_decision_stage", lambda *_a, **_k: pytest.fail("单个 stale 无需请 Jev"))
    assert module.delivery_quality_hint(host, record, archive) == "" and not calls


def test_write_candidates_exclude_unmodified_projects(stale_write, monkeypatch):
    host, record, archive = stale_write
    record.params.archive_tool_calls[1]["tool_result_envelope"]["verification_evidence"]["root"] = _OTHER_ROOT
    calls = install(monkeypatch)
    assert module.delivery_quality_hint(host, record, archive)
    rows = calls[0][2]["state"]["focuses"]
    assert len(rows) == 2 and all(row["edited_after"] for row in rows)
    assert [row["kind"] for row in rows] == ["test", "test"]


@pytest.mark.parametrize("trigger", ["run_command", "write_file", "edit_file", "apply_patch"])
def test_same_record_never_requests_twice(prepared, monkeypatch, trigger):
    case = prepared if trigger == "run_command" else write_record(prepared, trigger)
    calls = install(monkeypatch, choice="not_needed")
    assert loop._optional_result_hints(*case) == ""
    assert len(calls) == 1
    assert loop._optional_result_hints(*case) == ""
    assert len(calls) == 1, "写入和 run_command 共用每条记录一次，不随非选择结果重试"


def test_failed_optional_request_is_not_retried(stale_write, monkeypatch):
    calls = install(monkeypatch, error=TimeoutError("fixture late"))
    assert module.delivery_quality_hint(*stale_write) == ""
    assert len(calls) == 1
    assert module.delivery_quality_hint(*stale_write) == "" and len(calls) == 1


@pytest.mark.parametrize("missing", [None, {}, [], [{"status": "unverified", "root": _ROOT}],
                                     [{"status": "stale", "root": _ROOT}],
                                     [{"status": "stale", "root": _ROOT, "last_verification_id": True}],
                                     [{"status": "stale", "root": _ROOT, "last_verification_id": "13"}],
                                     [{"status": "stale", "root": _ROOT, "last_verification_id": 999}],
                                     [{"status": "stale", "root": _OTHER_ROOT, "last_verification_id": 13}]])
def test_write_requires_current_structured_stale_and_matching_event(stale_write, monkeypatch, missing):
    host, record, archive = stale_write
    record.result.metadata["handler_details"]["verification_state"] = missing
    archive["tool_result_envelope"]["verification_state"] = missing
    result = canonical_history_result(record.call, "status=stale last_verification_id=13 已复测",
                                      handler_details=record.result.metadata["handler_details"])
    record = replace(record, result=result)
    calls = install(monkeypatch)
    assert module.delivery_quality_hint(host, record, archive) == "" and not calls


@pytest.mark.parametrize("change", ["failed_write", "not_executed", "foreign_run", "foreign_task", "not_archived",
                                   "duplicate_archive", "mismatched_state", "repeated_failure", "unknown", "identical_failure"])
def test_ineligible_writes_make_no_request(stale_write, monkeypatch, change):
    host, record, archive = stale_write
    if change in {"failed_write", "not_executed"}:
        record = replace(record, result=replace(record.result, **({"status": "failed"} if change == "failed_write"
                                                                else {"handler_executed": False})))
    if change in {"foreign_run", "foreign_task"}:
        archive["run_id" if change == "foreign_run" else "task_id"] = "other"
    if change == "not_archived":
        record.params.archive_tool_calls[-1] = deepcopy(archive)
    if change == "duplicate_archive":
        record.params.archive_tool_calls.append(archive)
    if change == "mismatched_state":
        archive["tool_result_envelope"] = {"verification_state": []}
    halt = {"repeated_failure": "repeated_failure_halt", "unknown": "unknown_outcome_halt",
            "identical_failure": "identical_failure_halt"}.get(change)
    if halt:
        object.__setattr__(record.params, halt, ("edit_file", "failure", 3))
    calls = install(monkeypatch)
    assert module.delivery_quality_hint(host, record, archive) == "" and not calls


def test_child_write_does_not_scan_sources(stale_write, monkeypatch):
    host, record, archive = stale_write
    calls = install(monkeypatch)
    monkeypatch.setattr(module, "_scan", lambda *_args: pytest.fail("子代理不扫描验证焦点"))
    previous = set_current_subagent_context(host, run_id="child", task_attributes={"agent_thread_id": "thread-1"})
    try:
        assert module.delivery_quality_hint(host, record, archive) == ""
    finally:
        restore_current_subagent_context(host, previous)
    assert not calls


@pytest.mark.parametrize("change", ["policy", "last_id", "changed_paths", "hash", "deadline", "halt",
                                   "arguments", "allowed_tools", "write_boundary"])
def test_write_advice_is_dropped_when_frozen_facts_change(stale_write, monkeypatch, change):
    host, record, archive = stale_write

    def mutate(outcome, _stage):
        state = record.result.metadata["handler_details"]["verification_state"][0]
        if change == "last_id":
            state["last_verification_id"] = 12
        if change == "changed_paths":
            state["changed_paths"].append(_ROOT + "/another.py")
        if change == "hash":
            archive["output_hash"] = "b" * 64
        if change == "deadline":
            outcome.deadline = 0
        if change == "halt":
            object.__setattr__(record.params, "unknown_outcome_halt", ("edit_file", "", "unknown", True))
        if change == "arguments":
            object.__setattr__(record.call, "arguments", {"path": _ROOT + "/another.py"})
        if change == "allowed_tools":
            record.params.allowed_tools.clear()
        if change == "write_boundary":
            object.__setattr__(record.params, "write_boundary", {"allowed_roots": []})

    calls = install(monkeypatch, mutate=mutate)
    if change == "policy":
        monkeypatch.setattr(module, "decision_outcome_is_current", lambda *_args: False)
    assert module.delivery_quality_hint(host, record, archive) == "" and len(calls) == 1


@pytest.mark.parametrize("mode", ["off", "observe", "apply"])
def test_write_hint_is_identical_in_shared_text_and_native(stale_write, monkeypatch, mode):
    host, record, archive = stale_write
    record.params.archive_tool_calls.pop()
    before = deepcopy(record.result.to_dict())
    calls = install(monkeypatch, mode=mode)
    ledger = isolate_recording(monkeypatch, archive)
    baseline = render_tool_result_for_live_prompt(record.result, archive)
    loop._record_tool_call(host, record)
    ir = next(item for item in record.params.tool_ir_history if isinstance(item, ToolResult))
    messages = AnthropicMessageAdapter().to_provider_messages(record.params.tool_ir_history)
    native = next(block["content"] for message in messages for block in message["content"] if block["type"] == "tool_result")
    text = record.params.tool_context[0].split("[tool-output-record round=4 index=1]\n", 1)[1]
    hint = _HINT_11.replace("failed", "passed")
    assert text == native == ir.render_for_model_prompt() == baseline + ("\n" + hint if mode == "apply" else "")
    assert len(calls) == (0 if mode == "off" else 1)
    assert record.result.to_dict() == before and ledger == [archive]
    assert record.params.repeated_failure_halt is None and getattr(record.params, "unknown_outcome_halt", None) is None


def test_write_cancellation_is_not_swallowed(stale_write, monkeypatch):
    token = stale_write[1].params.cancellation_token
    calls = install(monkeypatch, mutate=lambda *_args: token.cancel("user-stop"))
    with pytest.raises(ToolCancelled):
        module.delivery_quality_hint(*stale_write)
    assert len(calls) == 1


# LLM: 沿原验证 SQLite、归档、展示和 worker 接线；命令退出结果为 fixture，不声称执行过项目测试命令。
# 函数用途: 在临时项目里登记两次已通过的验证，再实际改文件并登记多文件写入的 stale 事实。
def record_verified_edits(host, params, tmp_path):
    projects = [project_at(tmp_path / f"workspace-{index}") for index in range(2)]
    steps = [("run_command", {"command": "pytest", "working_dir": str(project)},
              {"process": {"status": "exited", "return_code": 0}}) for project in projects]
    paths = [str(project / "login.py") for project in projects]
    steps.append(("apply_patch", {}, {"files_modified": paths}))
    records = []
    for index, (tool, arguments, details) in enumerate(steps, 1):
        if tool == "apply_patch":
            change_project_files(projects)
        call = canonical_history_call(tool, arguments, call_id=f"call-{index}", run_id="run-1",
                                      turn_id=f"turn-{index}", attempt_id="attempt-1")
        result = canonical_history_result(call, "fixture private-output", handler_details=details)
        records.append(ToolCallRecordParams(params=params, tool_rounds=index, idx=1, call=call,
                                            result=record_tool_verification(host, call, result)))
        loop._record_tool_call(host, records[-1])
    return records


# LLM: 仅改临时夹具的实际文件，不替被测模型补产物；没有供应商调用、进程或生产目录副作用。
# 函数用途: 给后续 record_tool_verification 提供确实发生的临时项目修改。
def change_project_files(projects):
    for project in projects:
        (project / "login.py").write_text("print('changed')\n", encoding="utf-8")


@pytest.mark.parametrize("mode", ["off", "observe", "apply"])
def test_real_archive_and_worker_record_write_review_once(tmp_path, monkeypatch, mode):
    host, params = verified_host(tmp_path, mode)
    calls = install_backend(monkeypatch, host, params)
    records = record_verified_edits(host, params, tmp_path)
    state = records[-1].result.metadata["handler_details"]["verification_state"]
    assert len(state) == 2 and all(row["status"] == "stale" for row in state)
    assert {row["last_verification_id"] for row in state} == {
        item.result.metadata["handler_details"]["verification_evidence"]["id"] for item in records[:2]}
    results = [item for item in params.tool_ir_history if isinstance(item, ToolResult)]
    messages = AnthropicMessageAdapter().to_provider_messages(params.tool_ir_history)
    native = [block["content"] for message in messages for block in message["content"] if block["type"] == "tool_result"]
    text = params.tool_context[-1].split("[tool-output-record round=3 index=1]\n", 1)[1]
    assert text == native[-1] == results[-1].render_for_model_prompt()
    assert ("[delivery-review-focus]" in text) is (mode == "apply")
    assert len(calls) == len(model_call_ledger(host).records()) == (0 if mode == "off" else 1)
    assert results[-1].output == records[-1].result.output
    assert loop._optional_result_hints(host, records[-1], params.archive_tool_calls[-1]) == ""
    assert len(calls) == (0 if mode == "off" else 1)
    for call in calls:
        assert call["request"].binding.source_refs == (params.archive_tool_calls[-1]["scoped_call_id"],)
        assert call["archives"] == json.dumps(params.archive_tool_calls, sort_keys=True, default=str)
        payload = json.dumps(call["payload"], ensure_ascii=False)
        assert all(row["edited_after"] for row in call["payload"]["state"]["focuses"])
        assert not any(secret in payload for secret in (str(tmp_path), "login.py", "pytest", "private-output"))


def test_real_setting_change_during_write_decision_discards_hint(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends.typesafe_decision import TypesafeDecisionBackend
    from agent_py_agent.tests.test_decision_settings import patch

    host, params = verified_host(tmp_path, "apply")
    calls = install_backend(monkeypatch, host, params)
    original = TypesafeDecisionBackend.decide

    def invoke(backend, request, *, deadline):
        response = original(backend, request, deadline=deadline)
        patch(host, {f"points.{module._POINT}.mode": "off"})
        return response

    monkeypatch.setattr(TypesafeDecisionBackend, "decide", invoke)
    records = record_verified_edits(host, params, tmp_path)
    assert len(calls) == len(model_call_ledger(host).records()) == 1
    assert "[delivery-review-focus]" not in params.tool_context[-1]
    assert records[-1].result.ok is True
