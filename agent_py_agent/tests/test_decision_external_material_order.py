# LLM: 测试只替换决策供应边界；输入使用 canonical 回执，核对关闭等价、来源不变和 text/native 同一展示。
# 模块用途: 离线验证已归档页面的可选阅读提示，不联网、不补读 artifact、不修改原工具事实。
from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service as loop
from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_context import external_material_order as module
from agent_py_agent.agent.agent_core.tool_context.reducer import render_tool_result_for_live_prompt
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.backends.decision_protocol import (
    DecisionAnswer,
    DecisionBinding,
    DecisionResponse,
)
from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.tooling.output_projection import project_tool_output_body
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult, ToolSuccessFacts
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    make_test_model_spec,
    make_test_protocol_snapshot,
    runtime_snapshot_for_model_specs,
)


# LLM: 使用真实不可变 call/result 与原 runtime 参数；页面和归档字段来自现行 extract 合同，不读取真实文件。
# 函数用途: 构造一个含成功页、失败项和未完成项的已归档批次，给消费者与原宿主接缝共用。
@pytest.fixture
def prepared(tmp_path):
    params = ToolLoopExecuteParams(
        user_prompt="对比三份已经读取的说明。token=prompt-secret-value",
        memories=[], runtime_injections=[], prompt_files=[], tool_catalog_section="",
        tool_recommendations_section="", tool_context=[], effective_on_chunk=None,
        allowed_tools=["web_fetch"], write_boundary=None, task_attributes={"agent_thread_id": "thread-1"},
        request_id="request-1", run_id="run-1", task_id="task-1", save=False,
        one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="run-1"),
        tool_runtime_snapshot=runtime_snapshot_for_model_specs((make_test_model_spec("web_fetch"),), run_id="run-1"),
    )
    pages = [{"url": f"https://example.test/{index}?api_key=url-secret-value", "status": 200,
              "title": f"材料 {index}", "preview": f"已读摘录 {index} token=preview-secret-value",
              "artifact_ref": str(tmp_path / f"page-{index}.txt"), "content_hash": str(index) * 64,
              "content_type": "text/plain", "bytes": 123} for index in range(1, 4)]
    details = {"mode": "extract", "pages": pages, "failures": [{"error_code": "NETWORK_REQUEST_FAILED"}],
               "complete": False, "remaining_urls": ["https://pending.test/?token=pending-secret"], "stop_reason": "budget"}
    raw = json.dumps(details, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    call = canonical_history_call("web_fetch", {"mode": "extract", "urls": [page["url"] for page in pages],
                                               "headers": {"Authorization": "Bearer header-secret"}, "body": "body-secret"},
                                  call_id="fetch-1", run_id="run-1", turn_id="turn-1", attempt_id="attempt-1")
    result = ToolResult.succeeded(call, project_tool_output_body(tool="web_fetch", output=raw,
                                                               trust="external_data", redaction="default"),
                                  facts=ToolSuccessFacts(output_trust="external_data", metadata={
                                      "handler_details": details, "raw_output_sha256": digest}))
    archive = {"tool": "web_fetch", "id": call.call_id, "run_id": call.run_id, "task_id": params.task_id,
               "scoped_call_id": "run-1:fetch-1",
               "output_hash": digest, "output_externalized": False, "tool_result_envelope": deepcopy(details)}
    host = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(), config=SimpleNamespace(auto_save_memory=False))
    return host, ToolCallRecordParams(params=params, tool_rounds=1, idx=1, call=call, result=result), archive


# LLM: fake 沿原阶段和响应合同，只替换模型边界；不绕过消费者的版本、取消或非选择校验。
# 函数用途: 记录实际输入并提供可控结果，精确覆盖各种关闭/失效情形。
def install(monkeypatch, *, mode="apply", status="success", choices=None, mutate=None, error=None):
    calls = []
    stages = []
    monkeypatch.setattr(module, "POINT_RUNTIME_SCOPES", {module._POINT: "thread"})

    def begin(_agent, params, **kwargs):
        assert kwargs["operation_id"].startswith(module._POINT + ":")
        stage = SimpleNamespace(error_code="", deadline=time.monotonic() + 5, run_id=params.run_id,
                                enabled_points=() if mode == "off" else (module._POINT,))
        stages.append((params, stage))
        return stage

    def decide(_agent, params, stage, **kwargs):
        calls.append((params, stage, kwargs))
        if error:
            raise error
        answers = tuple(DecisionAnswer(key, "choice", (choices or ("later", "first", "first"))[index])
                        for index, key in enumerate(kwargs["questions"]))
        response = DecisionResponse(DecisionBinding(module._POINT, "owner", "operation", "policy", kwargs["candidates_revision"]),
                                    "digest", "decision-model", "decision-model", answers, b"{}")
        outcome = SimpleNamespace(may_apply=mode == "apply" and status == "success", response=response,
                                  mode=mode, status=status, deadline=stage.deadline)
        if mutate:
            mutate(outcome)
        return outcome

    monkeypatch.setattr(module, "begin_decision_stage", begin)
    monkeypatch.setattr(module, "decide", decide)
    monkeypatch.setattr(module, "decision_outcome_is_current", lambda *_args: True)
    return calls, stages


def test_unregistered_point_is_strict_noop(prepared, monkeypatch):
    monkeypatch.setattr(module, "POINT_RUNTIME_SCOPES", {})
    monkeypatch.setattr(module, "_eligible", lambda *_args: pytest.fail("unregistered point cannot prepare"))
    assert module.external_material_order_hint(*prepared) == ""


def test_off_does_not_encode_or_project_body(prepared, monkeypatch):
    calls, _ = install(monkeypatch, mode="off")
    monkeypatch.setattr(module, "_material", lambda *_args: pytest.fail("disabled cannot prepare text"))
    monkeypatch.setattr(module, "_safe_excerpt", lambda *_args: pytest.fail("disabled cannot redact text"))
    assert module.external_material_order_hint(*prepared) == ""
    assert not calls


@pytest.mark.parametrize("mode,status", [("observe", "success"), ("apply", "deadline"), ("apply", "cooldown"),
                                        ("apply", "error"), ("apply", "stale")])
def test_observe_and_failure_keep_original_result(prepared, monkeypatch, mode, status):
    before = deepcopy(prepared[1].result.to_dict())
    install(monkeypatch, mode=mode, status=status)
    assert module.external_material_order_hint(*prepared) == ""
    assert prepared[1].result.to_dict() == before


def test_apply_only_returns_complete_stable_priority_hint(prepared, monkeypatch):
    host, record, archive = prepared
    original_result, original_archive = deepcopy(record.result.to_dict()), deepcopy(archive)
    calls, stages = install(monkeypatch)
    hint = module.external_material_order_hint(host, record, archive)
    assert "2 → 3 → 1" in hint and "仅供参考" in hint
    assert len(hint) <= module._MAX_HINT_CHARS
    assert record.result.to_dict() == original_result and archive == original_archive
    assert len(stages) == len(calls) == 1 and calls[0][0] is record.params and calls[0][1] is stages[0][1]
    assert calls[0][2]["source_refs"] == (archive["scoped_call_id"],)


def test_input_uses_only_original_safe_excerpts_and_no_url_or_call_secrets(prepared, monkeypatch):
    calls, _ = install(monkeypatch)
    assert module.external_material_order_hint(*prepared)
    state = calls[0][2]["state"]
    payload = json.dumps({"state": state, "questions": calls[0][2]["questions"]}, ensure_ascii=False)
    for secret in ("url-secret-value", "header-secret", "body-secret", "pending-secret", "preview-secret-value", "prompt-secret-value"):
        assert secret not in payload
    assert "artifact_ref" not in payload and "https://" not in payload and "task_attributes" not in payload
    assert "<untrusted_tool_result" in payload and "已读摘录" in payload
    assert [row["id"] for row in state["pages"]] == ["page_1", "page_2", "page_3"]
    assert [row["content_hash"] for row in state["pages"]] == [str(index) * 64 for index in range(1, 4)]


@pytest.mark.parametrize("choice", ["not_needed", "no_match", "need_data", "abstain", "unknown"])
def test_nonselection_never_drops_pages(prepared, monkeypatch, choice):
    install(monkeypatch, choices=(choice, "first", "normal"))
    assert module.external_material_order_hint(*prepared) == ""
    assert len(prepared[1].result.metadata["handler_details"]["pages"]) == 3


@pytest.mark.parametrize("change", ["missing", "duplicate", "error", "kind", "revision"])
def test_invalid_answer_cannot_change_presentation(prepared, monkeypatch, change):
    def mutate(outcome):
        response = outcome.response
        if change == "missing":
            outcome.response = replace(response, answers=response.answers[:-1])
        elif change == "duplicate":
            outcome.response = replace(response, answers=(response.answers[0], response.answers[0], response.answers[2]))
        elif change == "revision":
            outcome.response = replace(response, binding=replace(response.binding, candidates_revision="old"))
        else:
            answer = replace(response.answers[0], **({"error_code": "bad_answer"} if change == "error" else {"kind": "score"}))
            outcome.response = replace(response, answers=(answer, *response.answers[1:]))
    install(monkeypatch, mutate=mutate)
    assert module.external_material_order_hint(*prepared) == ""


@pytest.mark.parametrize("change", ["preview", "artifact_ref", "content_hash", "url", "archive", "scope", "allowed", "mode"])
def test_waiting_cannot_adopt_changed_sources_or_scope(prepared, monkeypatch, change):
    _, record, archive = prepared
    def mutate(_outcome):
        if change in {"preview", "artifact_ref", "content_hash", "url"}:
            record.result.metadata["handler_details"]["pages"][0][change] = "changed"
        elif change == "archive":
            archive["output_hash"] = "different"
        elif change == "scope":
            record.params.task_attributes["project_id"] = "other"
        elif change == "allowed":
            record.params.allowed_tools.clear()
        else:
            record.result.metadata["handler_details"]["mode"] = "other"
    install(monkeypatch, mutate=mutate)
    assert module.external_material_order_hint(*prepared) == ""


@pytest.mark.parametrize("change", ["wrong_archive", "wrong_run", "wrong_task", "single_page", "missing_hash", "unknown_projection", "replayed", "failure", "raw_json_only"])
def test_unproven_candidates_do_not_call_decision(prepared, monkeypatch, change):
    host, record, archive = prepared
    if change == "wrong_archive":
        archive["id"] = "other-call"
    elif change in {"wrong_run", "wrong_task"}:
        record = replace(record, params=replace(record.params, **({"run_id": "other-run"} if change == "wrong_run" else {"task_id": "other-task"})))
    elif change == "single_page":
        record.result.metadata["handler_details"]["pages"] = record.result.metadata["handler_details"]["pages"][:1]
    elif change == "missing_hash":
        record.result.metadata["handler_details"]["pages"][0].pop("content_hash")
    elif change == "raw_json_only":
        record.result.metadata.pop("handler_details")
    else:
        updates = {"output_redaction": "source_code"} if change == "unknown_projection" else (
            {"handler_executed": False} if change == "replayed" else {"status": "failed"})
        record = replace(record, result=replace(record.result, **updates))
    calls, _ = install(monkeypatch)
    assert module.external_material_order_hint(host, record, archive) == ""
    assert not calls


def test_oversized_material_does_not_cut_or_read_sources(prepared, monkeypatch):
    prepared[1].result.metadata["handler_details"]["pages"][0]["preview"] = "x" * 300_000
    calls, _ = install(monkeypatch)
    assert module.external_material_order_hint(*prepared) == "" and not calls


@pytest.mark.parametrize("field", ["title", "preview", "user_prompt"])
@pytest.mark.parametrize("prefix", ["https://", "//"])
def test_query_urls_inside_excerpts_also_remain_local(prepared, monkeypatch, field, prefix):
    host, record, archive = prepared
    text = "参考 " + prefix + "source.test/read?custom_credential=private-value"
    if field == "user_prompt":
        record = replace(record, params=replace(record.params, user_prompt=text))
    else:
        record.result.metadata["handler_details"]["pages"][0][field] = text
    calls, _ = install(monkeypatch)
    assert module.external_material_order_hint(host, record, archive) == "" and not calls


def test_unchanged_order_needs_no_extra_prompt(prepared, monkeypatch):
    install(monkeypatch, choices=("normal", "normal", "normal"))
    assert module.external_material_order_hint(*prepared) == ""


@pytest.mark.parametrize("change", ["outcome_deadline", "stage_deadline", "current", "late_consume"])
def test_absolute_deadline_and_current_policy_are_checked_before_consumption(prepared, monkeypatch, change):
    calls, stages = install(monkeypatch, mutate=(lambda outcome: setattr(outcome, "deadline", 0)) if change == "outcome_deadline" else None)
    if change == "stage_deadline":
        original = module.decide
        def expire(*args, **kwargs):
            result = original(*args, **kwargs)
            stages[0][1].deadline = 0
            return result
        monkeypatch.setattr(module, "decide", expire)
    elif change in {"current", "late_consume"}:
        def current(*_args):
            if change == "late_consume":
                monkeypatch.setattr(module.time, "monotonic", lambda: stages[0][1].deadline + 1)
                return True
            return False
        monkeypatch.setattr(module, "decision_outcome_is_current", current)
    assert module.external_material_order_hint(*prepared) == "" and len(calls) == 1


@pytest.mark.parametrize("error", [RuntimeError("provider failure"), ValueError("invalid material")])
def test_optional_error_keeps_original_result(prepared, monkeypatch, error):
    install(monkeypatch, error=error)
    assert module.external_material_order_hint(*prepared) == ""


@pytest.mark.parametrize("status", ["success", "deadline", "error", "stale", "cooldown"])
def test_host_cancellation_is_propagated_before_adoption(prepared, monkeypatch, status):
    token = prepared[1].params.cancellation_token
    install(monkeypatch, status=status, mutate=lambda _outcome: token.cancel("user-stop"))
    with pytest.raises(ToolCancelled):
        module.external_material_order_hint(*prepared)


def test_cancelled_host_is_not_hidden_by_optional_error(prepared, monkeypatch):
    token = prepared[1].params.cancellation_token
    def fail(*_args, **_kwargs):
        token.cancel("user-stop")
        raise TimeoutError("optional call also timed out")
    install(monkeypatch)
    monkeypatch.setattr(module, "decide", fail)
    with pytest.raises(ToolCancelled):
        module.external_material_order_hint(*prepared)


# LLM: 仅替换已验证的归档/进度副作用；真实 _record_tool_call、reducer 和 IR 追加仍执行，用来核对唯一展示接缝。
# 函数用途: 让宿主测试记录原账本入参，并隔离与本片无关的持久化和进度同步。
def isolate_recording(monkeypatch, archive):
    seen = []
    monkeypatch.setattr(loop, "archive_tool_call_record", lambda _agent, _record: archive)
    monkeypatch.setattr(loop, "record_tool_guard_observation", lambda *_args: "")
    monkeypatch.setattr(loop, "_mark_repeated_failure_halt", lambda *_args: None)
    monkeypatch.setattr(loop, "_mark_unknown_outcome_halt", lambda *_args: None)
    monkeypatch.setattr(loop, "archive_tool_call_if_enabled", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(loop, "persist_tool_runtime_ledger", lambda _agent, record: seen.append(deepcopy(record)))
    monkeypatch.setattr(loop, "update_runtime_fact_progress_if_enabled", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(loop, "append_long_content_recovery_context", lambda *_args: None)
    monkeypatch.setattr(loop, "record_runtime_subagent_tool_progress", lambda *_args: "")
    return seen


@pytest.mark.parametrize("mode", ["off", "observe", "apply"])
def test_host_text_and_native_use_one_append_only_hint(prepared, monkeypatch, mode):
    host, record, archive = prepared
    original_result, original_archive = deepcopy(record.result.to_dict()), deepcopy(archive)
    calls, _ = install(monkeypatch, mode=mode)
    ledger = isolate_recording(monkeypatch, archive)
    baseline = render_tool_result_for_live_prompt(record.result, archive)
    loop._record_tool_call(host, record)
    history_result = next(item for item in record.params.tool_ir_history if isinstance(item, ToolResult))
    messages = AnthropicMessageAdapter().to_provider_messages(record.params.tool_ir_history)
    native = next(block["content"] for message in messages for block in message["content"] if block["type"] == "tool_result")
    text = record.params.tool_context[0].split("[tool-output-record round=1 index=1]\n", 1)[1]
    assert text == native == history_result.render_for_model_prompt()
    assert history_result.output == record.result.output
    if mode == "apply":
        assert text.startswith(baseline + "\n") and "2 → 3 → 1" in text
        assert text.count("[external-material-reading-order]") == 1
    else:
        assert text == baseline
    assert len(calls) == (0 if mode == "off" else 1)
    assert record.result.to_dict() == original_result and archive == original_archive
    assert ledger == [original_archive] and record.params.archive_tool_calls == [original_archive]
