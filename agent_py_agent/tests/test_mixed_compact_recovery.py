"""联合 Compact 恢复：真实后台入口和 provider 请求，仅在末端替换 HTTP。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core import compact_request_recovery as recovery_core
from agent_py_agent.agent.agent_core.model.context_pressure import (
    projected_model_context_components,
)
from agent_py_agent.agent.agent_core.runtime.context_compactor import runtime_compact_policy
from agent_py_agent.agent.backends.base import ProviderRequestOptions
from agent_py_agent.agent.backends.errors import ProviderContextWindowError, ProviderTransientError
from agent_py_agent.agent.backends.tool_ir import CompactionSummary
from agent_py_agent.agent.conversation import background_execution, compact
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
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_tool_summary import carried_compact_source_text
from agent_py_agent.agent.conversation.runtime import _run_params
from agent_py_agent.tests.test_background_compact_recovery import _background
from agent_py_agent.tests.test_subagent_compact_recovery import _http


# LLM: 使用真实后台运行准备，给同一请求同时提供已结束历史和原始活动归档；不构造伪造的工具 IR。
# 函数用途: 建立联合来源的 HTTP 验收材料，并保留另一任务的消息以检查拆出 scope 隔离。
def _mixed_background(tmp_path, *, backend: str, detached: bool, long_source: bool = False):
    agent, store, thread, request, _, sink = _background(
        tmp_path, backend=backend, detached=detached,
    )
    if detached:
        store.messages.append({
            "thread_id": thread.thread_id, "role": "user", "content": "OTHER_SCOPE_SECRET",
            "metadata": {"conversation_request_id": "other-task-prior", "task_id": "other-1"},
            "now": 14.0,
        })
    archived = [
        {
            "call_id": f"call-{number}", "scoped_call_id": f"run-mixed:call-{number}",
            "run_id": "run-mixed", "attempt_id": "attempt-mixed", "turn_id": "turn-mixed",
            "tool": "read_file", "ok": True,
            "model_parameters": {"path": f"source-{number}.txt"},
            "model_summary": f"MIXED_SOURCE_MARKER_{number} " + "完整工具内容" * 80,
            "output_preview": f"工具结果 {number}",
        }
        for number in range(2)
    ]
    archived.append({
        "tool": "read_file", "ok": True, "model_parameters": {"path": "unknown-identity.txt"},
        "model_summary": "UNKNOWN_IDENTITY_RETAINED", "output_preview": "身份不完整的原记录",
    })
    if long_source:
        archived[0]["model_summary"] = (
            "RUNBOOK package build lint test restore cache release dependency audit result. " * 500
        )

    def prepare(thread_id, *, thread, history_seed):
        params = _run_params(thread_id, request, agent, thread=thread, history_seed=history_seed)
        params.carried_archive_tool_calls = list(archived)
        return params

    execution = background_execution.BackgroundExecutionDependencies(agent, store, prepare)
    return agent, store, thread, request, execution, sink, archived


def _wire_from_material(agent, material):
    projected = material.projection
    frozen = material.request_input
    return agent.backend.project_generate_payload(
        projected.provider_prompt, tools=list(frozen.native_tools) or None,
        tool_choice=projected.tool_choice if frozen.native_tools else None,
        messages=projected.messages,
        request_options=ProviderRequestOptions(
            system_instruction=projected.system_instruction,
            thinking_disabled=bool(frozen.native_tools) and projected.tool_choice.mode != "auto",
        ),
    )


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


@pytest.mark.parametrize("backend", ["anthropic_compatible", "openai_compatible"])
@pytest.mark.parametrize("detached", [False, True])
def test_mixed_recovery_commits_both_sources_and_sends_selected_wire(tmp_path, monkeypatch, backend, detached):
    agent, store, thread, request, execution, sink, archived = _mixed_background(
        tmp_path, backend=backend, detached=detached,
    )
    scope = prepare_background_history_or_raise(agent, store, thread, request).compact_context.scope
    assert scope.kind == ("task" if detached else "thread")
    original_message_ids = [
        row.message_id for row in store.messages.recent(thread.thread_id, limit=0)
        if row.metadata.get("task_id") == request.task_id
    ]
    original_source = recovery_core._recovery_tool_source
    original_project = recovery_core._project_mixed_recovery_material
    original_summary = compact._summarize
    sources, candidates, business, summaries = [], [], [], []
    in_summary = False

    def source(*args):
        value = original_source(*args)
        sources.append(value)
        return value

    def project(*args):
        value = original_project(*args)
        if args[1].is_candidate and args[1].retained_tool_records is not None:
            candidates.append(value)
        return value

    def summary(*args, **kwargs):
        nonlocal in_summary
        in_summary = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary = False


    monkeypatch.setattr(recovery_core, "_recovery_tool_source", source)
    monkeypatch.setattr(recovery_core, "_project_mixed_recovery_material", project)
    monkeypatch.setattr(compact, "_summarize", summary)

    def on_http(wire, _number):
        if in_summary:
            summaries.append(wire)
            return
        business.append(wire)
        if len(business) == 1:
            raise ProviderContextWindowError("测试联合来源溢出")

    all_wires, _ = _http(monkeypatch, backend=backend, on_business=on_http)
    result = background_execution.run_background_turn_with_compact(
        execution, thread, request, user_prompt="继续联合核对", continuation_injection=[],
        proactive_delivery_available=False, activity_sink=sink,
    )
    assert result.runtime_status != "context_overflow"
    assert len(all_wires) == 3 and len(business) == 2 and len(summaries) == 1
    # 初始容量检查与上游溢出各冻结一次来源；同次候选始终复用那一次的完整分区。
    assert len(sources) == 2 and sources[0] is not None and sources[0] == sources[1]
    source = sources[1]
    assert len(source.source_records) == 2
    assert len(source.retained_records) == 1
    assert source.retained_records[0]["model_summary"] == "UNKNOWN_IDENTITY_RETAINED"
    summary_wire = json.dumps(summaries[0], ensure_ascii=False)
    source_text = carried_compact_source_text(source.source_records)
    assert any(source_text in item for item in _strings(summaries[0]))
    selected = [item for item in candidates if _wire_from_material(agent, item) == business[1]]
    assert len(selected) == 1
    material = selected[0]
    assert material.params.archive_tool_calls == archived
    committed = store.threads.require(thread.thread_id)
    assert committed.compact_generation == 1
    checkpoints = committed_compact_checkpoint_chain(agent, committed)
    assert len(checkpoints) == 1
    checkpoint = checkpoints[0]
    assert checkpoint["source_message_ids"] == original_message_ids
    assert checkpoint["source_tool_refs"] == list(source.source_tool_refs)
    assert checkpoint["retained_tool_refs"] == list(source.retained_tool_refs)
    assert "UNKNOWN_IDENTITY_RETAINED" in json.dumps(business[1], ensure_ascii=False)
    if detached:
        assert "OTHER_SCOPE_SECRET" not in json.dumps(business[1], ensure_ascii=False)
        assert "OTHER_SCOPE_SECRET" not in summary_wire


def test_mixed_replacement_fits_when_transcript_only_exceeds_real_input_ceiling(tmp_path, monkeypatch):
    agent, store, thread, request, execution, sink, _ = _mixed_background(
        tmp_path, backend="anthropic_compatible", detached=False, long_source=True,
    )
    agent.config.model_context_window_tokens = 16_000
    agent.config.max_tokens = agent.backend.max_tokens = 1_024
    original_project = recovery_core._project_mixed_recovery_material
    original_summary = compact._summarize
    measured, sent_business, sent_summaries = [], [], []
    in_summary = False

    def project(material, view, max_chars):
        candidate = original_project(material, view, max_chars)
        if view.is_candidate:
            before_handoff = [item.text for item in material.request_input.tool_ir_history
                              if isinstance(item, CompactionSummary) and item.source == "carried_tool_handoff"]
            after_handoff = [item.text for item in candidate.request_input.tool_ir_history
                             if isinstance(item, CompactionSummary) and item.source == "carried_tool_handoff"]
            assert len(before_handoff) == len(after_handoff) == 1
            assert "call-0" in before_handoff[0] and "call-0" not in after_handoff[0]
            assert "UNKNOWN_IDENTITY_RETAINED" in after_handoff[0]
            transcript_only, _ = projected_model_context_components(material.projection)
            mixed, _ = projected_model_context_components(candidate.projection)
            measured.append((transcript_only, mixed, candidate))
        return candidate

    def summary(*args, **kwargs):
        nonlocal in_summary
        in_summary = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary = False

    monkeypatch.setattr(recovery_core, "_project_mixed_recovery_material", project)
    monkeypatch.setattr(compact, "_summarize", summary)

    def on_http(wire, _number):
        if in_summary:
            sent_summaries.append(wire)
            return
        sent_business.append(wire)

    _http(monkeypatch, backend="anthropic_compatible", on_business=on_http)
    # 首轮溢出已由外层用例覆盖；这里从原恢复安全点开始，避免普通预检先压缩这份长输入。
    history = prepare_background_history_or_raise(agent, store, thread, request)
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
    assert store.threads.require(thread.thread_id).compact_generation == 0
    result, _, recovery = background_execution._run_background_recovery_attempt(
        execution, "继续联合核对", params, history, context, recovering=True, activity_sink=sink,
    )
    assert result.runtime_status != "context_overflow"
    assert recovery is not None and recovery.committed
    policy = runtime_compact_policy(agent)
    ceiling = compact._compact_request_input_ceiling(agent, policy)
    assert ceiling < policy.context_window_tokens  # 原请求仍为预留的模型输出留出窗口。
    assert measured and sent_summaries and len(sent_business) == 1
    selected = [(before, after, candidate) for before, after, candidate in measured
                if _wire_from_material(agent, candidate) == sent_business[0]]
    assert len(selected) == 1
    transcript_only, mixed, candidate = selected[0]
    assert len(candidate.params.compact_context.view.summary) < 2_000
    assert mixed < ceiling <= transcript_only
    assert mixed + agent.backend.max_tokens < policy.context_window_tokens <= transcript_only + agent.backend.max_tokens
    assert store.threads.require(thread.thread_id).compact_generation == 1


@pytest.mark.parametrize("failure", ["too_large", "summary_failure", "cancel", "generation_race"])
def test_mixed_recovery_failure_sends_no_second_business(tmp_path, monkeypatch, failure):
    agent, store, thread, request, execution, sink, _ = _mixed_background(
        tmp_path, backend="anthropic_compatible", detached=False,
    )
    original_project = recovery_core._project_mixed_recovery_material
    original_summary = compact._summarize
    business, summaries = [], []
    in_summary = False

    def project(*args):
        if args[1].is_candidate:
            if failure == "cancel":
                raise InterruptedError("测试联合恢复取消")
            if failure == "generation_race":
                store.threads.update_atomic(thread.thread_id, lambda current: replace(
                    current, compact_generation=current.compact_generation + 1,
                    summary="另一提交者获胜",
                ))
        return original_project(*args)

    def summary(*args, **kwargs):
        nonlocal in_summary
        if failure == "summary_failure":
            raise ProviderTransientError("503 temporary overload")
        in_summary = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary = False

    if failure == "too_large":
        original_ceiling = compact._compact_request_input_ceiling
        monkeypatch.setattr(
            compact, "_compact_request_input_ceiling",
            lambda *args: 1 if business else original_ceiling(*args),
        )
    monkeypatch.setattr(recovery_core, "_project_mixed_recovery_material", project)
    monkeypatch.setattr(compact, "_summarize", summary)

    def on_http(wire, _number):
        if in_summary:
            summaries.append(wire)
            return
        business.append(wire)
        if len(business) == 1:
            raise ProviderContextWindowError("测试联合来源溢出")
        pytest.fail("联合来源未提交时发送了恢复业务请求")

    all_wires, _ = _http(monkeypatch, backend="anthropic_compatible", on_business=on_http)
    with pytest.raises((ConversationCompactError, InterruptedError)):
        background_execution.run_background_turn_with_compact(
            execution, thread, request, user_prompt="继续联合核对", continuation_injection=[],
            proactive_delivery_available=False, activity_sink=sink,
        )
    assert len(business) == 1
    assert len(summaries) == (0 if failure == "summary_failure" else 1)
    assert len(all_wires) == 1 + len(summaries)
    committed = store.threads.require(thread.thread_id)
    assert committed.compact_generation == (1 if failure == "generation_race" else 0)
