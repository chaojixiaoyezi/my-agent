"""冻结真实原生工具 IR 的恢复安全点：只替换 HTTP，不伪造完整外层续跑。"""
from __future__ import annotations

import json
from dataclasses import replace
from functools import partial

import pytest

from agent_py_agent.agent.agent_core import compact_request_recovery as recovery_core
from agent_py_agent.agent.agent_core.runtime import loop_support
from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    ToolCallExecuteParams,
    ToolCallRecordParams,
)
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, ToolResult
from agent_py_agent.agent.conversation import active_turn_compact, background_execution, compact
from agent_py_agent.agent.conversation.background_compact_context import (
    apply_background_compact_context,
)
from agent_py_agent.agent.conversation.background_context import (
    prepare_background_context,
    render_background_context,
)
from agent_py_agent.agent.conversation.background_history_seed import (
    prepare_background_history_or_raise,
)
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_summary_view import resolve_compact_summary_view
from agent_py_agent.agent.conversation.runtime import _run_params
from agent_py_agent.tests._tool_runtime_harness import canonical_history_call
from agent_py_agent.tests.test_background_compact_recovery import _background
from agent_py_agent.tests.test_mixed_compact_recovery import _wire_from_material
from agent_py_agent.tests.test_subagent_compact_recovery import _http


# LLM: 原后台完整准备仍只执行一次；read_file 经原执行器与 recorder 生成工具对，随后恢复宿主在同次冻结输入上裁决。
# 函数用途: 为纯 IR、空 transcript 的恢复安全点安装真实文件读取记录，并保留原 HTTP builder 与 CAS。
def _native_ir_attempt(tmp_path, monkeypatch, *, backend: str, full_result: str, archive_present: bool = True):
    agent, store, thread, request, _, sink = _background(
        tmp_path, backend=backend, detached=True, with_history=False,
    )
    request = replace(request, reason="audit_finding")
    agent.config.enable_tools = True
    agent.config.tool_output_preview_chars = 64
    agent.config.tool_output_externalize_min_chars = 10_000_000
    source_path = agent.home_paths.owner_workspace_dir / "source.txt"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(full_result, encoding="utf-8")
    execution = background_execution.BackgroundExecutionDependencies(
        agent, store, partial(_run_params, request=request, agent=agent),
    )
    history = prepare_background_history_or_raise(agent, store, thread, request)
    assert history.seed is None and not history.compact_source.messages
    assert history.compact_context.scope.kind == "turn"
    recorded = []
    original_loop = loop_support._tool_loop_execute_params

    def freeze_ir(*args, **kwargs):
        params = original_loop(*args, **kwargs)
        assert not params.archive_tool_calls
        call = canonical_history_call(
            "read_file", {"path": str(source_path)},
            call_id="native-source-call", run_id=params.run_id,
            attempt_id=params.attempt_id, turn_id=f"{params.run_id}:source-turn",
        )
        runtime = params.tool_runtime_snapshot.runtime("read_file")
        assert runtime is not None
        call = replace(call, schema_hash=runtime.model_spec.schema_hash)
        from agent_py_agent.agent.agent_core._tool_loop_service import (
            _record_tool_call,
            execute_one_tool_call,
        )

        executed = execute_one_tool_call(agent, ToolCallExecuteParams(params, 1, 0, call))
        assert executed.result.ok and full_result in executed.result.output

        _record_tool_call(agent, ToolCallRecordParams(
            params=params, tool_rounds=1, idx=0, call=executed.call,
            result=executed.result, execution_states=executed.states, model_call=call,
        ))
        assert isinstance(params.tool_ir_history[-2], AssistantTurn)
        assert isinstance(params.tool_ir_history[-1], ToolResult)
        recorded.append((params.tool_ir_history[-2:], params.archive_tool_calls[-1]))
        if not archive_present:
            params.archive_tool_calls.clear()
        return params

    monkeypatch.setattr(loop_support, "_tool_loop_execute_params", freeze_ir)
    params = execution.prepare_run(thread.thread_id, thread=thread, history_seed=history.seed)
    params.compact_context = history.compact_context
    context = prepare_background_context(
        agent=agent, store=store, thread=thread, request=request,
        proactive_delivery_available=False, include_recent_messages=False,
        context_bundle=history.context_bundle,
    )
    context = apply_background_compact_context(context, history.compact_context, thread.compact_generation)
    params.inject = [render_background_context(context)]
    params.on_chunk = sink
    sink.begin_model_attempt(1)
    return agent, store, thread, execution, sink, history, params, context, recorded


@pytest.mark.parametrize("backend,archive_present", [
    ("anthropic_compatible", True),
    ("openai_compatible", True),
    ("anthropic_compatible", False),
])
def test_empty_transcript_native_ir_recovery_summarizes_full_result_once_and_sends_candidate(
    tmp_path, monkeypatch, backend, archive_present,
) -> None:
    marker = "EXACT_FULL_TOOL_BODY_" + "x" * 5_000
    agent, store, thread, execution, sink, history, params, context, recorded = _native_ir_attempt(
        tmp_path, monkeypatch, backend=backend, full_result=marker, archive_present=archive_present,
    )
    candidates, summaries, business, source_plans = [], [], [], []
    cas_calls = []
    original_project = recovery_core._project_mixed_recovery_material
    original_summary = active_turn_compact._active_turn_replacement_summary
    original_cas = store.threads.update_compact_state
    in_summary = False

    def commit(*args, **kwargs):
        cas_calls.append((args, kwargs))
        return original_cas(*args, **kwargs)

    def project(*args):
        material = original_project(*args)
        if args[1].is_candidate:
            candidates.append(material)
        return material

    def summary(*args, **kwargs):
        nonlocal in_summary
        source_plans.append(args[1])
        in_summary = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary = False

    monkeypatch.setattr(recovery_core, "_project_mixed_recovery_material", project)
    monkeypatch.setattr(active_turn_compact, "_active_turn_replacement_summary", summary)
    monkeypatch.setattr(store.threads, "update_compact_state", commit)

    def on_http(wire, _number):
        if in_summary:
            summaries.append(wire)
        else:
            business.append(wire)
            assert len(candidates) == 1 and wire == _wire_from_material(agent, candidates[0])

    all_wires, _ = _http(monkeypatch, backend=backend, on_business=on_http)
    result, _, recovery = background_execution._run_background_recovery_attempt(
        execution, "继续核对完整工具结果", params, history, context,
        recovering=True, activity_sink=sink,
    )

    assert result.runtime_status != "context_overflow"
    assert recovery is not None and recovery.committed
    assert len(cas_calls) == 1
    assert len(recorded) == len(source_plans) == len(candidates) == len(summaries) == len(business) == 1
    assert len(all_wires) == 2
    native_ir, archived = recorded[0]
    assert archived["call_id"] == native_ir[0].tool_calls[0].call_id
    assert len(archived["output_preview"]) < len(native_ir[1].output)
    assert marker in native_ir[1].output
    assert len(source_plans[0].source_records) == int(archive_present)
    assert source_plans[0].source_ir_history == tuple(native_ir)
    assert len(source_plans[0].source_tool_refs) == 1
    summary_messages = json.dumps(summaries[0].get("messages"), ensure_ascii=False)
    assert marker in summary_messages
    assert summary_messages.count("EXACT_FULL_TOOL_BODY_") == 1
    candidate = candidates[0]
    assert not any(isinstance(item, (AssistantTurn, ToolResult)) for item in candidate.request_input.tool_ir_history)
    committed = store.threads.require(thread.thread_id)
    assert committed.compact_generation == 1 and committed.compact_checkpoint_id
    view = resolve_compact_summary_view(agent, committed, history.compact_context.scope)
    assert view.source_tool_refs == tuple(source_plans[0].source_tool_refs)
    assert (archived in candidate.params.archive_tool_calls) is archive_present


@pytest.mark.parametrize("failure", ["cancel", "too_large"])
def test_native_ir_recovery_failure_sends_no_business_or_commit(tmp_path, monkeypatch, failure) -> None:
    agent, store, thread, execution, sink, history, params, context, recorded = _native_ir_attempt(
        tmp_path, monkeypatch, backend="anthropic_compatible", full_result="FULL-CONTENT-" + "y" * 5_000,
    )
    summaries, business = [], []
    original_project = recovery_core._project_mixed_recovery_material
    original_summary = active_turn_compact._active_turn_replacement_summary
    original_ceiling = compact._compact_request_input_ceiling
    original_cas = store.threads.update_compact_state
    cas_calls = []
    in_summary = False

    def commit(*args, **kwargs):
        cas_calls.append((args, kwargs))
        return original_cas(*args, **kwargs)

    def project(*args):
        if failure == "cancel" and args[1].is_candidate:
            raise InterruptedError("测试取消原生IR候选")
        return original_project(*args)

    def summary(*args, **kwargs):
        nonlocal in_summary
        in_summary = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary = False

    monkeypatch.setattr(recovery_core, "_project_mixed_recovery_material", project)
    monkeypatch.setattr(active_turn_compact, "_active_turn_replacement_summary", summary)
    monkeypatch.setattr(store.threads, "update_compact_state", commit)
    if failure == "too_large":
        monkeypatch.setattr(
            compact, "_compact_request_input_ceiling",
            lambda *args: 1 if summaries else original_ceiling(*args),
        )

    def on_http(wire, _number):
        if in_summary:
            summaries.append(wire)
        else:
            business.append(wire)
            pytest.fail("原生 IR 候选未提交时发送了业务请求")

    _http(monkeypatch, backend="anthropic_compatible", on_business=on_http)
    expected_error = InterruptedError if failure == "cancel" else ConversationCompactError
    with pytest.raises(expected_error) as raised:
        background_execution._run_background_recovery_attempt(
            execution, "继续核对完整工具结果", params, history, context,
            recovering=True, activity_sink=sink,
        )
    assert len(recorded) == len(summaries) == 1
    assert not business
    assert not cas_calls
    if failure == "too_large":
        assert raised.value.code == "COMPACT_CANDIDATE_TOO_LARGE"
    committed = store.threads.require(thread.thread_id)
    assert committed.compact_generation == 0 and not committed.compact_checkpoint_id
