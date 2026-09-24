# LLM: 只替换决策服务边界；验证事件与归档信封沿原公开形状，_record_tool_call 为原实现，核对原结果/归档不变及 text/native 同段提示。
# 模块用途: 离线验证交付复核焦点的资格、脱敏输入、非选择回退、来源复核、取消传播与宿主展示接缝。
from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service as loop
from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_context import decision_delivery_quality as module
from agent_py_agent.agent.agent_core.tool_context.reducer import render_tool_result_for_live_prompt
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.backends.decision_protocol import (
    DecisionAnswer,
    DecisionBinding,
    DecisionInputError,
    DecisionResponse,
)
from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
    make_test_model_spec,
    make_test_protocol_snapshot,
    runtime_snapshot_for_model_specs,
)
from agent_py_agent.tests.test_decision_external_material_order import install as install_reading
from agent_py_agent.tests.test_decision_external_material_order import isolate_recording

_ROOT = "/private-owner/project-secret"
_OTHER_ROOT = "/private-owner/other-secret"
_Q = module._QUESTION
_HINT_11 = ("[delivery-review-focus]\n可选复核建议：交付前可先复核本轮验证事件 "
            "#11（test/targeted，failed，其后有修改）；范围与结果以原事实为准，targeted 不代表全量。")


# LLM: 字段与 verification/runtime.py::_public_evidence 的公开形状一致；root/命令/时间只是本地来源，不应外发。
# 函数用途: 构造一条宿主验证事件，可覆盖 kind/scope/status/root，退出码随状态。
def evidence(event_id, **facts):
    row = {"id": event_id, "status": "failed", "kind": "test", "scope": "full", "root": _ROOT,
           "canonical_command": "pytest --secret-flag", "created_at": f"2026-09-24T00:00:{event_id % 60:02d}+00:00",
           **facts}
    return {**row, "exit_code": 0 if row["status"] == "passed" else 1}


# LLM: 归档行只放本片会读的结构化字段；scoped_call_id 沿原 run:call 形状，但本片不解析它。
# 函数用途: 构造一条同 run/task 的工具归档记录。
def archive_row(call_id, tool, envelope):
    return {"tool": tool, "id": call_id, "run_id": "run-1", "task_id": "task-1", "scoped_call_id": f"run-1:{call_id}",
            "output_hash": "a" * 64, "tool_result_envelope": envelope}


# LLM: 使用真实不可变 call/result 与原 runtime 参数；当前事件与其归档共用同一验证对象，同原归档投影一致。
# 函数用途: 构造“局部测试失败→改文件→lint 通过→全量测试失败”的本轮归档，当前回执为最后一次全量测试。
@pytest.fixture
def prepared(tmp_path):
    specs = (make_test_model_spec("run_command"), make_test_model_spec("edit_file"))
    params = ToolLoopExecuteParams(
        user_prompt="修复登录后再交付。token=prompt-secret-value", memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section="", tool_recommendations_section="", tool_context=[], effective_on_chunk=None,
        allowed_tools=["run_command", "edit_file"], write_boundary=None, task_attributes={"agent_thread_id": "thread-1"},
        request_id="request-1", run_id="run-1", task_id="task-1", save=False,
        one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="run-1"),
        tool_runtime_snapshot=runtime_snapshot_for_model_specs(specs, run_id="run-1"),
    )
    stale = [{"status": "stale", "root": _ROOT, "changed_paths": [_ROOT + "/login.py"],
              "last_verification_id": 11, "last_verification_status": "failed"}]
    call = canonical_history_call("run_command", {"command": "pytest -q --secret-flag", "working_dir": _ROOT},
                                  call_id="cmd-3", run_id="run-1", turn_id="turn-4", attempt_id="attempt-1")
    result = canonical_history_result(call, "FAILED output-secret-text", ok=False, error_code="COMMAND_FAILED",
                                      handler_details={"process": {"status": "exited", "return_code": 1},
                                                       "verification_evidence": evidence(13)})
    details = result.metadata["handler_details"]
    # 原 _compact_result_envelope 直接引用 canonical 结果里的同一验证对象，这里保持同样的共享关系。
    archive = archive_row("cmd-3", "run_command", {"verification_evidence": details["verification_evidence"],
                                                    "process": details["process"]})
    params.archive_tool_calls.extend([
        archive_row("cmd-1", "run_command", {"verification_evidence": evidence(11, scope="targeted")}),
        archive_row("edit-1", "edit_file", {"verification_state": stale}),
        archive_row("cmd-2", "run_command", {"verification_evidence": evidence(12, kind="lint", status="passed")}),
        archive,
    ])
    host = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(), config=SimpleNamespace(auto_save_memory=False))
    return host, ToolCallRecordParams(params=params, tool_rounds=4, idx=1, call=call, result=result), archive


# LLM: fake 沿原阶段/响应合同，只替换决策服务边界；不绕过消费者的版本、取消或非选择校验。
# 函数用途: 记录实际决策输入并返回可控结果；fake 可设 mode/status/choice/answers/mutate/error/stage_error/stage_run_id。
def install(monkeypatch, **fake):
    calls = []
    mode = fake.get("mode", "apply")
    monkeypatch.setattr(module, "POINT_RUNTIME_SCOPES", {module._POINT: "thread"})

    def begin(_agent, run_params, **kwargs):
        assert kwargs["operation_id"].startswith(module._POINT + ":")
        return SimpleNamespace(error_code=fake.get("stage_error", ""), deadline=time.monotonic() + 5,
                               run_id=fake.get("stage_run_id", run_params.run_id),
                               enabled_points=() if mode == "off" else (module._POINT,))

    def decide(_agent, run_params, stage, **kwargs):
        calls.append((run_params, stage, kwargs))
        if fake.get("error") is not None:
            raise fake["error"]
        answers = fake["answers"] if "answers" in fake else (DecisionAnswer(_Q, "choice", fake.get("choice", "focus_1")),)
        binding = DecisionBinding(module._POINT, "owner", "operation", "policy", kwargs["candidates_revision"])
        status = fake.get("status", "success")
        outcome = SimpleNamespace(may_apply=mode == "apply" and status == "success", mode=mode, status=status,
                                  deadline=stage.deadline, response=DecisionResponse(
                                      binding, "digest", "decision-model", "decision-model", answers, b"{}"))
        fake.get("mutate", lambda *_args: None)(outcome, stage)
        return outcome

    monkeypatch.setattr(module, "begin_decision_stage", begin)
    monkeypatch.setattr(module, "decide", decide)
    monkeypatch.setattr(module, "decision_outcome_is_current", lambda *_args: True)
    return calls


# LLM: 场景变换只改结构化来源；返回可能替换的不可变 record 与同一归档对象。
# 函数用途: 生成“改一个归档字段”的变换。
def set_archive(key, value):
    def change(record, archive):
        archive[key] = value
        return record, archive
    return change


# 函数用途: 生成“改一个运行参数字段”的变换，冻结 dataclass 沿原宿主的 object.__setattr__ 写法。
def set_params(name, value):
    def change(record, archive):
        object.__setattr__(record.params, name, value)
        return record, archive
    return change


# 函数用途: 生成“在本轮最前面补若干个互不相同的已通过焦点”的变换，用于数量上下界。
def extra_focuses(count):
    def change(record, archive):
        record.params.archive_tool_calls[:0] = [archive_row(f"check-{index}", "run_command", {
            "verification_evidence": evidence(100 + index, kind=f"check{index}", status="passed")}) for index in range(count)]
        return record, archive
    return change


# 函数用途: 只保留当前这一条归档，使本轮只剩一个焦点。
def only_current(record, archive):
    del record.params.archive_tool_calls[:-1]
    return record, archive


# 函数用途: 把本轮所有焦点改为已通过且没有其后修改。
def all_passing(record, archive):
    for item in record.params.archive_tool_calls:
        item["tool_result_envelope"].pop("verification_state", None)
        item["tool_result_envelope"].get("verification_evidence", {})["status"] = "passed"
    return record, archive


# 函数用途: 把当前工具换成非 run_command，call/result/归档三处一致。
def other_tool(record, archive):
    archive["tool"] = "shell"
    call, result = replace(record.call, tool_name="shell"), replace(record.result, tool_name="shell")
    return replace(record, call=call, result=result), archive


# 函数用途: 修改指定归档位置的信封，供构造畸形或他人来源。
def envelope_at(index, **changes):
    def change(record, archive):
        record.params.archive_tool_calls[index]["tool_result_envelope"].update(changes)
        return record, archive
    return change


# 函数用途: 修改指定归档位置的验证事件字段。
def event_at(index, **changes):
    def change(record, archive):
        record.params.archive_tool_calls[index]["tool_result_envelope"]["verification_evidence"].update(changes)
        return record, archive
    return change


# 函数用途: 修改指定归档位置的归属字段。
def row_at(index, **changes):
    def change(record, archive):
        record.params.archive_tool_calls[index].update(changes)
        return record, archive
    return change


_INELIGIBLE = {
    "single_focus": only_current,
    "all_passing": all_passing,
    "thirteen_focuses": extra_focuses(10),
    "other_tool": other_tool,
    "archive_id": set_archive("id", "other-call"),
    "archive_run": set_archive("run_id", "other-run"),
    "archive_task": set_archive("task_id", "other-task"),
    "archive_scope": set_archive("scoped_call_id", ""),
    "archive_evidence": set_archive("tool_result_envelope", {"verification_evidence": evidence(99)}),
    "not_archived": lambda record, archive: (record.params.archive_tool_calls.__setitem__(-1, deepcopy(archive)), (record, archive))[1],
    "archived_twice": lambda record, archive: (record.params.archive_tool_calls.append(archive), (record, archive))[1],
    "other_run_sources": lambda record, archive: row_at(2, task_id="other-task")(*row_at(0, run_id="other-run")(record, archive)),
    "repeated_halt": set_params("repeated_failure_halt", ("run_command", "code:COMMAND_FAILED", 3)),
    "unknown_halt": set_params("unknown_outcome_halt", ("run_command", "", "unknown", True)),
    "long_request": set_params("user_prompt", "x" * 1025),
    "empty_request": set_params("user_prompt", ""),
    "no_evidence": lambda record, archive: (record.result.metadata["handler_details"].pop("verification_evidence"),
                                            (record, archive))[1],
    "replayed": lambda record, archive: (replace(record, result=replace(record.result, handler_executed=False)), archive),
    "duplicate_event": event_at(2, id=11),
    "bool_event_id": event_at(0, id=True),
    "malformed_kind": event_at(0, kind="Test Kind"),
    "text_exit_code": event_at(0, exit_code="1"),
    "missing_root": event_at(0, root=""),
    "malformed_state": envelope_at(1, verification_state={"status": "stale", "root": _ROOT}),
    "stale_without_root": envelope_at(1, verification_state=[{"status": "stale"}]),
    "non_stale_state": lambda record, archive: envelope_at(1, verification_state=[
        {"status": "unverified", "root": _ROOT}])(*all_passing(record, archive)),
    "request_query_url": set_params("user_prompt", "参考 https://source.test/read?custom_credential=private 后交付"),
    "request_relative_query_url": set_params("user_prompt", "参考 //source.test/read?custom_credential=private"),
}
_ELIGIBLE = {
    "two_focuses": lambda record, archive: (record.params.archive_tool_calls.pop(2), (record, archive))[1],
    "twelve_focuses": extra_focuses(9),
    "edited_but_passing": lambda record, archive: event_at(0, status="passed")(*event_at(3, status="passed")(record, archive)),
    "failed_without_edits": envelope_at(1, verification_state=[]),
}


def test_unregistered_point_is_strict_noop(prepared, monkeypatch):
    monkeypatch.setattr(module, "POINT_RUNTIME_SCOPES", {})
    monkeypatch.setattr(module, "_eligible", lambda *_args: pytest.fail("unregistered point cannot read sources"))
    assert module.delivery_quality_hint(*prepared) == ""


@pytest.mark.parametrize("stage", [{"mode": "off"}, {"stage_error": "configuration_unavailable"},
                                   {"stage_run_id": "other-run"}], ids=["off", "stage_error", "foreign_run"])
def test_closed_or_foreign_stage_prepares_no_material(prepared, monkeypatch, stage):
    calls = install(monkeypatch, **stage)
    monkeypatch.setattr(module, "_material", lambda *_args: pytest.fail("closed point cannot prepare material"))
    monkeypatch.setattr(module, "_safe_request", lambda *_args: pytest.fail("closed point cannot project the request"))
    assert module.delivery_quality_hint(*prepared) == "" and not calls


@pytest.mark.parametrize("mode,status", [("observe", "success"), ("apply", "deadline"), ("apply", "cooldown"),
                                         ("apply", "error"), ("apply", "stale")])
def test_observe_and_failed_outcomes_keep_original(prepared, monkeypatch, mode, status):
    before = deepcopy(prepared[1].result.to_dict())
    calls = install(monkeypatch, mode=mode, status=status)
    assert module.delivery_quality_hint(*prepared) == ""
    assert len(calls) == 1 and prepared[1].result.to_dict() == before


@pytest.mark.parametrize("choice,facts", [
    ("focus_1", "#11（test/targeted，failed，其后有修改）；范围与结果以原事实为准，targeted 不代表全量。"),
    ("focus_2", "#12（lint/full，passed）；范围与结果以原事实为准。"),
])
def test_apply_renders_only_the_chosen_focus_host_facts(prepared, monkeypatch, choice, facts):
    host, record, archive = prepared
    before = deepcopy((record.result.to_dict(), archive, record.params.archive_tool_calls))
    calls = install(monkeypatch, choice=choice)
    hint = module.delivery_quality_hint(host, record, archive)
    assert hint == "[delivery-review-focus]\n可选复核建议：交付前可先复核本轮验证事件 " + facts
    assert len(hint) <= module._MAX_HINT_CHARS
    assert (record.result.to_dict(), archive, record.params.archive_tool_calls) == before
    assert len(calls) == 1 and calls[0][0] is record.params
    assert calls[0][2]["point"] == "delivery_quality" and calls[0][2]["source_refs"] == ("run-1:cmd-3",)


def test_request_carries_only_redacted_request_and_focus_facts(prepared, monkeypatch):
    calls = install(monkeypatch)
    assert module.delivery_quality_hint(*prepared) == _HINT_11
    state, questions = calls[0][2]["state"], calls[0][2]["questions"]
    payload = json.dumps({"state": state, "questions": questions}, ensure_ascii=False)
    for secret in ("private-owner", "secret", "login.py", "pytest", "2026-09-24", "run-1", "cmd-", "changed_paths"):
        assert secret not in payload
    assert set(state) == {"current_request", "focuses"} and set(questions) == {_Q}
    assert "<untrusted_tool_result" in state["current_request"] and "修复登录后再交付" in state["current_request"]
    assert state["focuses"] == [
        {"candidate": "focus_1", "project": "project_1", "order": 1, "edited_after": True, "exit_code": 1,
         "kind": "test", "scope": "targeted", "status": "failed"},
        {"candidate": "focus_2", "project": "project_1", "order": 2, "edited_after": False, "exit_code": 0,
         "kind": "lint", "scope": "full", "status": "passed"},
        {"candidate": "focus_3", "project": "project_1", "order": 3, "edited_after": False, "exit_code": 1,
         "kind": "test", "scope": "full", "status": "failed"},
    ]
    assert questions[_Q]["type"] == "choice"
    assert set(questions[_Q]["criteria"]) == {"focus_1", "focus_2", "focus_3", "not_needed", "no_match",
                                              "abstain", "need_data"}


def test_distinct_projects_get_aliases_without_paths(prepared, monkeypatch):
    host, record, archive = prepared
    record.params.archive_tool_calls.insert(0, archive_row("cmd-0", "run_command", {
        "verification_evidence": evidence(10, root=_OTHER_ROOT, status="passed")}))
    calls = install(monkeypatch, choice="focus_1")
    hint = module.delivery_quality_hint(host, record, archive)
    assert hint.endswith("#10（test/full，passed）；范围与结果以原事实为准。")
    assert [row["project"] for row in calls[0][2]["state"]["focuses"]] == ["project_1", "project_2", "project_2", "project_2"]
    assert "other-secret" not in json.dumps(calls[0][2], ensure_ascii=False)


@pytest.mark.parametrize("change", sorted(_INELIGIBLE))
def test_ineligible_sources_make_no_decision_call(prepared, monkeypatch, change):
    host, record, archive = prepared
    record, archive = _INELIGIBLE[change](record, archive)
    calls = install(monkeypatch)
    assert module.delivery_quality_hint(host, record, archive) == "" and not calls


@pytest.mark.parametrize("change", ["other_tool", "replayed", "no_evidence", "archive_evidence", "archive_id",
                                    "archive_run", "archive_task", "archive_scope", "repeated_halt", "unknown_halt",
                                    "long_request", "empty_request"])
def test_mismatched_current_record_returns_before_scanning_the_archive(prepared, monkeypatch, change):
    host, record, archive = prepared
    record, archive = _INELIGIBLE[change](record, archive)
    calls = install(monkeypatch)
    monkeypatch.setattr(module, "_scan", lambda *_args: pytest.fail("mismatched records cannot scan the run archive"))
    assert module.delivery_quality_hint(host, record, archive) == "" and not calls


def test_render_budget_drops_an_oversized_hint():
    focus = {"id": 1, "kind": "k" * 600, "scope": "full", "status": "failed", "edited_after": False, "current": False}
    assert module._render_hint(focus) == ""
    assert module._render_hint({**focus, "kind": "test"}).startswith(module._HINT_TAG)


def test_subagent_run_makes_no_decision_call(prepared, monkeypatch):
    calls = install(monkeypatch)
    monkeypatch.setattr(module, "_scan", lambda *_args: pytest.fail("child runs cannot scan the run archive"))
    previous = set_current_subagent_context(prepared[0], run_id="child-run", task_attributes={"agent_thread_id": "thread-1"})
    try:
        assert module.delivery_quality_hint(*prepared) == ""
    finally:
        restore_current_subagent_context(prepared[0], previous)
    assert not calls


@pytest.mark.parametrize("change", sorted(_ELIGIBLE))
def test_eligible_boundaries_reach_one_decision(prepared, monkeypatch, change):
    host, record, archive = prepared
    record, archive = _ELIGIBLE[change](record, archive)
    calls = install(monkeypatch, choice="not_needed")
    assert module.delivery_quality_hint(host, record, archive) == "" and len(calls) == 1


def test_only_the_latest_event_per_project_kind_and_scope_is_a_focus(prepared, monkeypatch):
    host, record, archive = prepared
    record.params.archive_tool_calls.insert(0, archive_row("cmd-0", "run_command", {
        "verification_evidence": evidence(9, scope="targeted", status="passed")}))
    calls = install(monkeypatch, choice="focus_1")
    assert module.delivery_quality_hint(host, record, archive) == _HINT_11
    focuses = calls[0][2]["state"]["focuses"]
    assert [(row["kind"], row["scope"], row["status"]) for row in focuses] == [
        ("test", "targeted", "failed"), ("lint", "full", "passed"), ("test", "full", "failed")]


@pytest.mark.parametrize("answers", [
    (DecisionAnswer(_Q, "choice", "not_needed"),), (DecisionAnswer(_Q, "choice", "no_match"),),
    (DecisionAnswer(_Q, "choice", "abstain"),), (DecisionAnswer(_Q, "choice", "need_data"),),
    (DecisionAnswer(_Q, "choice", "focus_9"),), (DecisionAnswer(_Q, "choice", "focus_0"),),
    (DecisionAnswer(_Q, "choice", "focus_1"), DecisionAnswer(_Q, "choice", "focus_2")),
    (DecisionAnswer(_Q, "choice", "focus_1", error_code="invalid_answer"),),
    (DecisionAnswer(_Q, "score", "focus_1"),), (DecisionAnswer("other_question", "choice", "focus_1"),), (),
], ids=["not_needed", "no_match", "abstain", "need_data", "out_of_range", "zero", "multiple", "error_code",
        "wrong_kind", "wrong_question", "missing"])
def test_nonselection_and_bad_answers_add_nothing(prepared, monkeypatch, answers):
    calls = install(monkeypatch, answers=answers)
    assert module.delivery_quality_hint(*prepared) == "" and len(calls) == 1


def test_choosing_the_evidence_of_this_call_adds_nothing(prepared, monkeypatch):
    calls = install(monkeypatch, choice="focus_3")
    assert module.delivery_quality_hint(*prepared) == "" and len(calls) == 1


_LATE_CHANGES = {
    "archive_hash": lambda record, archive: archive.update(output_hash="b" * 64),
    "archive_identity": lambda record, archive: archive.update(id="other-call"),
    "current_status": lambda record, archive: archive["tool_result_envelope"]["verification_evidence"].update(status="passed"),
    "older_focus": event_at(0, scope="full"),
    "new_edit": lambda record, archive: record.params.archive_tool_calls.append(
        archive_row("edit-2", "edit_file", {"verification_state": [{"status": "stale", "root": _ROOT}]})),
    "task_scope": lambda record, archive: record.params.task_attributes.update(project_id="other"),
    "allowed_tools": lambda record, archive: record.params.allowed_tools.clear(),
    "request": set_params("user_prompt", "换一个请求"),
    "halt": set_params("unknown_outcome_halt", ("run_command", "", "unknown", True)),
}


@pytest.mark.parametrize("change", sorted(_LATE_CHANGES))
def test_late_advice_is_rejected_when_sources_change(prepared, monkeypatch, change):
    host, record, archive = prepared
    calls = install(monkeypatch, mutate=lambda *_args: _LATE_CHANGES[change](record, archive))
    assert module.delivery_quality_hint(host, record, archive) == "" and len(calls) == 1


@pytest.mark.parametrize("mutate", [
    lambda outcome, _stage: setattr(outcome, "deadline", 0),
    lambda _outcome, stage: setattr(stage, "deadline", 0),
    lambda outcome, _stage: setattr(outcome, "response", replace(outcome.response, binding=replace(
        outcome.response.binding, candidates_revision="old"))),
], ids=["outcome_deadline", "stage_deadline", "candidates_revision"])
def test_deadline_and_binding_are_checked_before_adoption(prepared, monkeypatch, mutate):
    calls = install(monkeypatch, mutate=mutate)
    assert module.delivery_quality_hint(*prepared) == "" and len(calls) == 1


def test_changed_policy_blocks_adoption(prepared, monkeypatch):
    calls = install(monkeypatch)
    monkeypatch.setattr(module, "decision_outcome_is_current", lambda *_args: False)
    assert module.delivery_quality_hint(*prepared) == "" and len(calls) == 1


def test_sources_changed_during_the_policy_check_are_dropped(prepared, monkeypatch):
    host, record, archive = prepared
    calls = install(monkeypatch)
    monkeypatch.setattr(module, "decision_outcome_is_current", lambda *_args: archive.update(output_hash="c" * 64) is None)
    assert module.delivery_quality_hint(host, record, archive) == "" and len(calls) == 1


def test_consumption_after_the_absolute_deadline_is_dropped(prepared, monkeypatch):
    calls = install(monkeypatch)

    def current(*_args):
        monkeypatch.setattr(module.time, "monotonic", lambda: calls[0][1].deadline + 1)
        return True

    monkeypatch.setattr(module, "decision_outcome_is_current", current)
    assert module.delivery_quality_hint(*prepared) == "" and len(calls) == 1


@pytest.mark.parametrize("error", [RuntimeError("provider failure"), ValueError("bad value"),
                                   DecisionInputError("too large"), TimeoutError("late")])
def test_optional_errors_keep_the_original_result(prepared, monkeypatch, error):
    before = (deepcopy(prepared[1].result.to_dict()), deepcopy(prepared[2]))
    calls = install(monkeypatch, error=error)
    assert module.delivery_quality_hint(*prepared) == "" and len(calls) == 1
    assert (prepared[1].result.to_dict(), prepared[2]) == before


@pytest.mark.parametrize("status", ["success", "deadline", "error", "stale"])
def test_host_cancellation_propagates_before_adoption(prepared, monkeypatch, status):
    token = prepared[1].params.cancellation_token
    install(monkeypatch, status=status, mutate=lambda *_args: token.cancel("user-stop"))
    with pytest.raises(ToolCancelled):
        module.delivery_quality_hint(*prepared)


def test_cancellation_during_the_policy_check_propagates(prepared, monkeypatch):
    token = prepared[1].params.cancellation_token
    install(monkeypatch)
    monkeypatch.setattr(module, "decision_outcome_is_current", lambda *_args: token.cancel("user-stop") is None)
    with pytest.raises(ToolCancelled):
        module.delivery_quality_hint(*prepared)


def test_cancellation_is_not_hidden_by_an_optional_error(prepared, monkeypatch):
    token = prepared[1].params.cancellation_token

    def fail(*_args, **_kwargs):
        token.cancel("user-stop")
        raise TimeoutError("optional call also timed out")

    install(monkeypatch)
    monkeypatch.setattr(module, "decide", fail)
    with pytest.raises(ToolCancelled):
        module.delivery_quality_hint(*prepared)


def test_interrupt_from_the_decision_boundary_propagates(prepared, monkeypatch):
    install(monkeypatch, error=InterruptedError("user stop"))
    with pytest.raises(InterruptedError):
        module.delivery_quality_hint(*prepared)


@pytest.mark.parametrize("mode", ["off", "observe", "apply"])
def test_host_text_and_native_share_one_appended_hint(prepared, monkeypatch, mode):
    host, record, archive = prepared
    record.params.archive_tool_calls.pop()  # 原 _record_tool_call 会自行追加这一条归档。
    original_result, original_archive = deepcopy(record.result.to_dict()), deepcopy(archive)
    calls = install(monkeypatch, mode=mode)
    ledger = isolate_recording(monkeypatch, archive)
    baseline = render_tool_result_for_live_prompt(record.result, archive)
    loop._record_tool_call(host, record)
    history_result = next(item for item in record.params.tool_ir_history if isinstance(item, ToolResult))
    messages = AnthropicMessageAdapter().to_provider_messages(record.params.tool_ir_history)
    native = next(block["content"] for message in messages for block in message["content"] if block["type"] == "tool_result")
    text = record.params.tool_context[0].split("[tool-output-record round=4 index=1]\n", 1)[1]
    assert text == native == history_result.render_for_model_prompt()
    assert history_result.output == record.result.output
    assert text == baseline + ("\n" + _HINT_11 if mode == "apply" else "")
    assert text.count("[delivery-review-focus]") == (1 if mode == "apply" else 0)
    assert len(calls) == (0 if mode == "off" else 1)
    assert record.result.to_dict() == original_result and archive == original_archive
    assert ledger == [original_archive] and record.params.archive_tool_calls[-1] is archive


def test_run_command_record_reaches_at_most_one_decision_point(prepared, monkeypatch):
    reading_calls, _ = install_reading(monkeypatch)
    calls = install(monkeypatch)
    assert loop._optional_result_hints(*prepared) == "\n" + _HINT_11
    assert len(calls) == 1 and not reading_calls


def test_a_passing_and_chain_contributes_every_event_as_a_focus(prepared, monkeypatch):
    host, record, archive = prepared
    chain = [evidence(7, kind="build", status="passed"), evidence(8, kind="typecheck", status="passed")]
    record.params.archive_tool_calls.insert(0, archive_row("cmd-0", "run_command", {
        "verification_evidence": chain[-1], "verification_evidence_chain": chain}))
    calls = install(monkeypatch, choice="not_needed")
    module.delivery_quality_hint(host, record, archive)
    focuses = calls[0][2]["state"]["focuses"]
    kinds = [(row["kind"], row["scope"], row["status"]) for row in focuses]
    assert ("build", "full", "passed") in kinds and ("typecheck", "full", "passed") in kinds, \
        "串联里前面几段的事件同样是焦点，不能只看末项"


def test_an_inconsistent_chain_gives_up_without_a_request(prepared, monkeypatch):
    host, record, archive = prepared
    record.params.archive_tool_calls.insert(0, archive_row("cmd-0", "run_command", {
        "verification_evidence": evidence(8, kind="typecheck", status="passed"),
        "verification_evidence_chain": [evidence(7, status="passed")]}))
    calls = install(monkeypatch)
    assert module.delivery_quality_hint(host, record, archive) == "" and calls == []
