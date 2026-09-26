"""后台 Compact 恢复：真实执行器、会话账和 provider builder，仅替换末端 HTTP。"""
from __future__ import annotations

from dataclasses import replace
from functools import partial

import pytest

from agent_py_agent.agent.agent_core import compact_request_recovery, runtime_mixin
from agent_py_agent.agent.agent_core.tool_request_projection import project_tool_loop_request
from agent_py_agent.agent.backends.base import ProviderRequestOptions
from agent_py_agent.agent.backends.errors import ProviderContextWindowError, ProviderTransientError
from agent_py_agent.agent.backends.tool_ir import (
    AssistantTurn,
    CompactionSummary,
    ToolResult,
    UserTurn,
)
from agent_py_agent.agent.conversation import (
    active_turn_compact,
    background_compact_recovery,
    background_execution,
    compact,
    compact_carry,
)
from agent_py_agent.agent.conversation.agent_activity import BackgroundMainActivitySink
from agent_py_agent.agent.conversation.background_history_seed import (
    prepare_background_history_or_raise,
)
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_summary_view import resolve_compact_summary_view
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_subagent_compact_recovery import _http


# LLM: 使用原 SimpleAgent、唯一会话 store 与后台真实入口；无任务场景不伪造任务绑定，只有 HTTP 响应由替身提供。
# 函数用途: 为普通、拆出或无任务的后台轮建立已结束历史，并返回同一片执行依赖。
def _background(tmp_path, *, backend: str, detached: bool, with_history: bool = True, taskless: bool = False):
    api_base = "https://api.minimaxi.com/anthropic" if backend == "anthropic_compatible" else "https://opencode.ai/zen/go/v1"
    model_name = "MiniMax-M2.7" if backend == "anthropic_compatible" else "deepseek-v4-flash"
    agent = SimpleAgent(AgentConfig(
        model_backend=backend, model_name=model_name, api_base=api_base,
        api_key="fake-private-key", stream_enabled=False, enable_tools=False,
        my_agent_home=str(tmp_path / "home"),
        model_context_window_tokens=200_000, model_context_window_explicit=True,
        max_tool_rounds=1, tool_context_ptl_retry_max=0,
    ), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "owner", "channel": "tui", "channel_conversation_id": "compact-recovery",
        "channel_user_id": "owner", "now": 10.0,
    })
    task_id = "" if taskless else ("detached-1" if detached else "ordinary-1")
    if task_id:
        store.tasks.bind({
            "thread_id": thread.thread_id, "task_id": task_id, "goal": "持续核对旧资料",
            "work_kind": "goal" if detached else "general", "work_name": "旧资料监测",
            "cancellation_scope": "detached" if detached else "shared", "now": 11.0,
        })
    for position, (role, content) in enumerate((
        ("user", "之前需要核对的原始材料" * 100),
        ("assistant", "已核对旧材料，等待下一步" * 100),
    ) if with_history else ()):
        store.messages.append({
            "thread_id": thread.thread_id, "role": role, "content": content,
            "metadata": {"conversation_request_id": f"completed-prior-turn-{position}", "task_id": task_id},
            "now": 12.0 + position,
        })
    request = BackgroundRunRequest(
        thread_id=thread.thread_id, task_id=task_id, reason="scheduled_progress_report",
        route_channel="internal", now=20.0,
    )
    execution = background_execution.BackgroundExecutionDependencies(
        agent=agent, store=store, prepare_run=partial(_run_params, request=request, agent=agent),
    )
    sink = BackgroundMainActivitySink(agent, thread_id=thread.thread_id, task_id=task_id)
    return agent, store, thread, request, execution, sink


@pytest.mark.parametrize("backend", ["anthropic_compatible", "openai_compatible"])
@pytest.mark.parametrize("detached", [False, True])
def test_background_overflow_reuses_selected_candidate_wire(tmp_path, monkeypatch, backend, detached):
    agent, store, thread, request, execution, sink = _background(tmp_path, backend=backend, detached=detached)
    scope = prepare_background_history_or_raise(agent, store, thread, request).compact_context.scope
    assert scope.kind == ("task" if detached else "thread")
    prepares, prepare_run_indices, candidates, runs, sent_business, sent_summaries = [], [], [], [], [], []
    original_prepare = runtime_mixin._prepare_runtime_context
    original_project = background_compact_recovery._project_background_candidate
    original_run = agent.run
    original_summary = compact._summarize
    in_summary = False

    def prepare(*args, **kwargs):
        prepares.append(args)
        prepare_run_indices.append(len(runs))
        return original_prepare(*args, **kwargs)

    def project(*args):
        material = original_project(*args)
        if args[-1].is_candidate:
            candidates.append(material)
        return material

    def run(*args, **kwargs):
        runs.append(args)
        return original_run(*args, **kwargs)

    def summary(*args, **kwargs):
        nonlocal in_summary
        in_summary = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary = False

    monkeypatch.setattr(runtime_mixin, "_prepare_runtime_context", prepare)
    monkeypatch.setattr(background_compact_recovery, "_project_background_candidate", project)
    monkeypatch.setattr(agent, "run", run)
    monkeypatch.setattr(compact, "_summarize", summary)

    def on_business(wire, _number):
        if in_summary:
            sent_summaries.append(wire)
            return
        sent_business.append(wire)
        if len(sent_business) == 1:
            raise ProviderContextWindowError("测试供应商上下文溢出")
        if len(sent_business) == 2:
            assert len(candidates) == 1
            material = candidates[0]
            projected = material.projection
            frozen = material.request_input
            expected = agent.backend.project_generate_payload(
                projected.provider_prompt, tools=list(frozen.native_tools) or None,
                tool_choice=projected.tool_choice if frozen.native_tools else None,
                messages=projected.messages,
                request_options=ProviderRequestOptions(
                    system_instruction=projected.system_instruction,
                    thinking_disabled=bool(frozen.native_tools) and projected.tool_choice.mode != "auto",
                ),
            )
            assert wire == expected

    business, _ = _http(monkeypatch, backend=backend, on_business=on_business)
    result = background_execution.run_background_turn_with_compact(
        execution, thread, request, user_prompt="继续核对本轮资料", continuation_injection=[],
        proactive_delivery_available=False, activity_sink=sink,
    )
    assert result.runtime_status != "context_overflow"
    assert len(business) == 3
    assert len(sent_business) == 2 and len(sent_summaries) == 1
    assert len(candidates) == 1
    assert len(prepares) == len(runs) == 2
    assert prepare_run_indices == [1, 2]
    assert store.threads.require(thread.thread_id).compact_generation == 1


# LLM: 无 task 的后台工作片由第一次真实模型尝试解析 request_id；第二次 prepare 不得换身份或借 carry 倒灌执行权。
# 函数用途: 两协议用真实后台循环和原恢复器证明空请求编号的 overflow 能沿同一请求续接。
@pytest.mark.parametrize("backend", ["anthropic_compatible", "openai_compatible"])
def test_taskless_background_overflow_keeps_first_resolved_request_id(tmp_path, monkeypatch, backend):
    agent, store, thread, request, execution, sink = _background(
        tmp_path, backend=backend, detached=False, taskless=True,
    )
    assert request.task_id == ""
    assert store.tasks.list(thread.thread_id) == []
    from agent_py_agent.agent.conversation.runtime import _run_params

    assert _run_params(thread.thread_id, request, agent).request_id == ""
    history = prepare_background_history_or_raise(agent, store, thread, request)
    assert history.compact_source is not None and history.compact_source.messages
    original_prepare_run = execution.prepare_run
    original_run = agent.run
    original_model_attempt = background_execution._run_background_model_attempt
    original_restore = compact_carry.restore_native_compact_carry
    original_summary = compact._summarize
    prepared_ids, sent_ids, bound_ids, restored, business, summaries = [], [], [], [], [], []
    in_summary = False

    def prepare_run(*args, **kwargs):
        params = original_prepare_run(*args, **kwargs)
        prepared_ids.append(params.request_id)
        return params

    def run(*args, **kwargs):
        params = kwargs["params"]
        sent_ids.append((params.request_id, params.run_id, params.attempt_id))
        return original_run(*args, **kwargs)

    def model_attempt(*args, **kwargs):
        result, bound = original_model_attempt(*args, **kwargs)
        bound_ids.append((bound.request_id, bound.run_id, bound.attempt_id))
        return result, bound

    def restore(agent_arg, params):
        carry = params.native_compact_carry
        if carry is not None:
            restored.append((carry.request_id, params.request_id, carry.source_attempt_id))
        return original_restore(agent_arg, params)

    def summary(*args, **kwargs):
        nonlocal in_summary
        in_summary = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary = False

    execution = replace(execution, prepare_run=prepare_run)
    monkeypatch.setattr(agent, "run", run)
    monkeypatch.setattr(background_execution, "_run_background_model_attempt", model_attempt)
    monkeypatch.setattr(compact_carry, "restore_native_compact_carry", restore)
    monkeypatch.setattr(compact, "_summarize", summary)

    def on_business(wire, _number):
        if in_summary:
            summaries.append(wire)
            return
        business.append(wire)
        if len(business) == 1:
            raise ProviderContextWindowError("测试无 task 后台首请求溢出")

    all_wires, _ = _http(monkeypatch, backend=backend, on_business=on_business)
    result = background_execution.run_background_turn_with_compact(
        execution, thread, request, user_prompt="继续核对本轮资料", continuation_injection=[],
        proactive_delivery_available=False, activity_sink=sink,
    )

    assert result.runtime_status != "context_overflow"
    assert prepared_ids == ["", ""]
    assert len(sent_ids) == 2 and sent_ids[0][0] and sent_ids[0][0] == sent_ids[1][0]
    assert sent_ids[0][1] == sent_ids[1][1]
    assert sent_ids[0][2] != sent_ids[1][2]
    assert len(bound_ids) == 2 and bound_ids[0][0] == bound_ids[1][0] == sent_ids[0][0]
    assert bound_ids[0][2] != bound_ids[1][2]
    assert len(restored) == 1 and restored[0][0] == restored[0][1] == sent_ids[0][0]
    assert restored[0][2] == bound_ids[0][2]
    assert store.tasks.list(thread.thread_id) == []
    assert len(business) == 2 and len(summaries) == 1 and len(all_wires) == 3
    assert store.threads.require(thread.thread_id).compact_generation == 1


@pytest.mark.parametrize("backend", ["anthropic_compatible", "openai_compatible"])
def test_background_first_request_compacts_after_complete_prepare(tmp_path, monkeypatch, backend):
    agent, store, thread, request, execution, sink = _background(
        tmp_path, backend=backend, detached=False,
    )
    # 窗口只校准测试输入：工具目录随 manage_models（f7029f54c）变长后，15500 装不下压缩后的候选。实测 16000–23000 都能
    # 先触发压缩再装下候选，25000 起不再需要压缩；取中间值给工具目录增减留出余量，断言不放松。
    agent.config.model_context_window_tokens = 19_500
    agent.config.max_tokens = agent.backend.max_tokens = 1_024
    for role in ("user", "assistant"):
        store.messages.append({
            "thread_id": thread.thread_id, "role": role,
            "content": "补充的构建、测试与发布记录" * 250,
            "metadata": {"conversation_request_id": f"completed-extra-{role}", "task_id": request.task_id},
            "now": 14.0 if role == "user" else 15.0,
        })
    original_run = agent.run
    original_prepare = runtime_mixin._prepare_runtime_context
    original_summary = compact._summarize
    runs, prepares, summaries, business, candidates = [], [], [], [], []
    in_summary = False

    def run(*args, **kwargs):
        runs.append(args)
        return original_run(*args, **kwargs)

    def prepare(*args, **kwargs):
        prepares.append(args)
        return original_prepare(*args, **kwargs)

    def summary(*args, **kwargs):
        nonlocal in_summary
        in_summary = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary = False

    original_project = background_compact_recovery._project_background_candidate

    def project(*args):
        material = original_project(*args)
        if args[-1].is_candidate:
            candidates.append(material)
        return material

    monkeypatch.setattr(agent, "run", run)
    monkeypatch.setattr(runtime_mixin, "_prepare_runtime_context", prepare)
    monkeypatch.setattr(compact, "_summarize", summary)
    monkeypatch.setattr(background_compact_recovery, "_project_background_candidate", project)

    def on_http(wire, _number):
        if in_summary:
            summaries.append(wire)
        else:
            business.append((wire, store.threads.require(thread.thread_id).compact_generation))

    all_wires, _ = _http(monkeypatch, backend=backend, on_business=on_http)
    result = background_execution.run_background_turn_with_compact(
        execution, thread, request, user_prompt="继续核对本轮资料", continuation_injection=[],
        proactive_delivery_available=False, activity_sink=sink,
    )
    assert result.runtime_status != "context_overflow"
    assert len(runs) == len(prepares) == 1
    assert len(candidates) == 1 and len(summaries) == 1 and len(business) == 1
    assert len(all_wires) == 2
    assert business[0][1] == store.threads.require(thread.thread_id).compact_generation == 1
    projected = candidates[0].projection
    frozen = candidates[0].request_input
    expected = agent.backend.project_generate_payload(
        projected.provider_prompt, tools=list(frozen.native_tools) or None,
        tool_choice=projected.tool_choice if frozen.native_tools else None,
        messages=projected.messages,
        request_options=ProviderRequestOptions(
            system_instruction=projected.system_instruction,
            thinking_disabled=bool(frozen.native_tools) and projected.tool_choice.mode != "auto",
        ),
    )
    assert business[0][0] == expected


@pytest.mark.parametrize("backend", ["anthropic_compatible", "openai_compatible"])
def test_background_first_request_below_ceiling_uses_same_frozen_wire(tmp_path, monkeypatch, backend):
    agent, store, thread, request, execution, sink = _background(
        tmp_path, backend=backend, detached=False,
    )
    original_factory = background_compact_recovery.prepare_background_compact_recovery
    original_prepare = runtime_mixin._prepare_runtime_context
    hosts, prepares, business = [], [], []

    def factory(*args, **kwargs):
        host = original_factory(*args, **kwargs)
        hosts.append(host)
        return host

    def prepare(*args, **kwargs):
        prepares.append(args)
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(background_compact_recovery, "prepare_background_compact_recovery", factory)
    monkeypatch.setattr(runtime_mixin, "_prepare_runtime_context", prepare)
    monkeypatch.setattr(compact, "_summarize", lambda *_args, **_kwargs: pytest.fail("未超容量却发送摘要"))

    def on_http(wire, _number):
        business.append((wire, store.threads.require(thread.thread_id).compact_generation))

    all_wires, _ = _http(monkeypatch, backend=backend, on_business=on_http)
    result = background_execution.run_background_turn_with_compact(
        execution, thread, request, user_prompt="继续核对本轮资料", continuation_injection=[],
        proactive_delivery_available=False, activity_sink=sink,
    )
    assert result.runtime_status != "context_overflow"
    assert len(prepares) == len(hosts) == len(business) == len(all_wires) == 1
    assert business[0][1] == store.threads.require(thread.thread_id).compact_generation == 0
    host = hosts[0]
    assert not host.committed and host.resolved_input is not None
    projected = project_tool_loop_request(host.resolved_input)
    expected = agent.backend.project_generate_payload(
        projected.provider_prompt, tools=list(host.resolved_input.native_tools) or None,
        tool_choice=projected.tool_choice if host.resolved_input.native_tools else None,
        messages=projected.messages,
        request_options=ProviderRequestOptions(
            system_instruction=projected.system_instruction,
            thinking_disabled=bool(host.resolved_input.native_tools) and projected.tool_choice.mode != "auto",
        ),
    )
    assert business[0][0] == expected


@pytest.mark.parametrize("failure", ["summary_failure", "candidate_cancel", "generation_race"])
def test_uncommitted_background_recovery_sends_no_restored_business(tmp_path, monkeypatch, failure):
    agent, store, thread, request, execution, sink = _background(
        tmp_path, backend="anthropic_compatible", detached=False,
    )
    original_project = background_compact_recovery._project_background_candidate
    original_summary = compact._summarize
    in_summary = False
    sent_business, sent_summaries = [], []

    def project(*args):
        if args[-1].is_candidate:
            if failure == "candidate_cancel":
                raise InterruptedError("测试取消候选")
            if failure == "generation_race":
                store.threads.update_atomic(thread.thread_id, lambda current: replace(
                    current, compact_generation=current.compact_generation + 1,
                    summary="另一个提交者已经获胜",
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

    monkeypatch.setattr(compact, "_summarize", summary)
    monkeypatch.setattr(background_compact_recovery, "_project_background_candidate", project)

    def on_business(wire, _number):
        if in_summary:
            sent_summaries.append(wire)
            return
        sent_business.append(wire)
        if len(sent_business) == 1:
            raise ProviderContextWindowError("测试供应商上下文溢出")
        pytest.fail("未提交恢复时发送了业务模型请求")

    business, _ = _http(monkeypatch, backend="anthropic_compatible", on_business=on_business)
    with pytest.raises((RuntimeError, InterruptedError)):
        background_execution.run_background_turn_with_compact(
            execution, thread, request, user_prompt="继续核对本轮资料", continuation_injection=[],
            proactive_delivery_available=False, activity_sink=sink,
        )
    assert len(business) == (1 if failure == "summary_failure" else 2)
    assert len(sent_business) == 1
    assert len(sent_summaries) == (0 if failure == "summary_failure" else 1)
    refreshed = store.threads.require(thread.thread_id)
    assert refreshed.compact_generation == (1 if failure == "generation_race" else 0)
    if failure == "generation_race":
        assert refreshed.summary == "另一个提交者已经获胜"


# LLM: 两种后台scope只改变原宿主请求和历史种子；工具来源仍是同一四元原归档，不伪造provider工具对。
# 函数用途: 给活动归档的HTTP成功与失败验收准备真实后台执行入口。
def _active_background(tmp_path, *, backend: str, narrow: bool):
    agent, store, thread, request, _, sink = _background(
        tmp_path, backend=backend, detached=narrow, with_history=False,
    )
    if narrow:
        request = replace(request, reason="audit_finding")
    history = prepare_background_history_or_raise(agent, store, thread, request)
    assert history.status == ("disabled" if narrow else "ready")
    assert (history.seed is None) is narrow
    assert not history.compact_source.messages
    assert history.compact_context.scope.kind == ("turn" if narrow else "thread")
    archived = [{
        "call_id": "call-prior", "scoped_call_id": "run-prior:call-prior",
        "run_id": "run-prior", "attempt_id": "attempt-prior", "turn_id": "turn-prior",
        "tool": "read_file", "ok": True, "model_parameters": {"path": "old.txt"},
        "model_summary": "先前已完成的读取结果", "output_preview": "旧材料摘要" * 400,
    }]

    def prepare(thread_id, *, thread, history_seed):
        params = _run_params(thread_id, request, agent, thread=thread, history_seed=history_seed)
        params.carried_archive_tool_calls = list(archived)
        return params

    execution = background_execution.BackgroundExecutionDependencies(agent, store, prepare)
    return agent, store, thread, request, execution, sink, history, archived


@pytest.mark.parametrize("backend", ["anthropic_compatible", "openai_compatible"])
@pytest.mark.parametrize("narrow", [False, True])
def test_active_archive_recovery_sends_selected_wire(tmp_path, monkeypatch, backend, narrow):
    agent, store, thread, request, execution, sink, history, archived = _active_background(
        tmp_path, backend=backend, narrow=narrow,
    )
    original_project = background_compact_recovery._project_background_active_candidate
    original_mixed = compact_request_recovery._project_mixed_recovery_material
    original_summary = active_turn_compact._active_turn_replacement_summary
    candidates, sent_business, sent_summaries = [], [], []
    in_summary = False

    def project(*args):
        frozen_ir = args[3].tool_ir_history
        assert sum(isinstance(item, CompactionSummary) and item.source == "carried_tool_handoff"
                   for item in frozen_ir) == 1
        assert not any(isinstance(item, ToolResult) or (
            isinstance(item, AssistantTurn) and item.tool_calls
        ) for item in frozen_ir)
        return original_project(*args)

    def mixed(material, view, max_chars):
        selected = original_mixed(material, view, max_chars)
        if view.is_candidate:
            candidates.append(selected)
        return selected

    def summary(*args, **kwargs):
        nonlocal in_summary
        in_summary = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary = False

    monkeypatch.setattr(background_compact_recovery, "_project_background_active_candidate", project)
    monkeypatch.setattr(compact_request_recovery, "_project_mixed_recovery_material", mixed)
    monkeypatch.setattr(active_turn_compact, "_active_turn_replacement_summary", summary)

    def on_business(wire, _number):
        if in_summary:
            sent_summaries.append(wire)
            return
        sent_business.append(wire)
        if len(sent_business) == 1:
            raise ProviderContextWindowError("测试供应商上下文溢出")
        if len(sent_business) == 2:
            assert len(candidates) == 1
            material = candidates[0]
            projected = material.projection
            frozen = material.request_input
            expected = agent.backend.project_generate_payload(
                projected.provider_prompt, tools=list(frozen.native_tools) or None,
                tool_choice=projected.tool_choice if frozen.native_tools else None,
                messages=projected.messages,
                request_options=ProviderRequestOptions(
                    system_instruction=projected.system_instruction,
                    thinking_disabled=bool(frozen.native_tools) and projected.tool_choice.mode != "auto",
                ),
            )
            assert wire == expected

    business, _ = _http(monkeypatch, backend=backend, on_business=on_business)
    result = background_execution.run_background_turn_with_compact(
        execution, thread, request, user_prompt="报告本次新发现", continuation_injection=[],
        proactive_delivery_available=False, activity_sink=sink,
    )
    assert result.runtime_status != "context_overflow"
    assert len(sent_business) == 2 and len(sent_summaries) == 1 and len(business) == 3
    assert len(candidates) == 1
    candidate = candidates[0]
    assert sum(isinstance(item, CompactionSummary) and item.source == "applied_compact"
               for item in candidate.request_input.tool_ir_history) == (1 if narrow else 0)
    if not narrow:
        assert any("Earlier Conversation Summary" in str(message.get("content"))
                   for message in candidate.request_input.provider_history_messages)
    assert not any(isinstance(item, CompactionSummary) and item.source == "carried_tool_handoff"
                   for item in candidate.request_input.tool_ir_history)
    assert candidate.params.archive_tool_calls == archived
    committed = store.threads.require(thread.thread_id)
    assert committed.compact_generation == 1
    view = resolve_compact_summary_view(agent, committed, history.compact_context.scope)
    assert view.source_tool_refs == ({
        "run_id": "run-prior", "attempt_id": "attempt-prior",
        "turn_id": "turn-prior", "call_id": "call-prior",
    },)


@pytest.mark.parametrize("failure", [
    "candidate_too_large", "unread_retained_ir", "summary_failure", "candidate_cancel", "generation_race",
])
def test_uncommitted_active_recovery_sends_no_restored_business(tmp_path, monkeypatch, failure):
    agent, store, thread, request, execution, sink, _, _ = _active_background(
        tmp_path, backend="anthropic_compatible", narrow=True,
    )
    original_project = background_compact_recovery._project_background_active_candidate
    original_mixed = compact_request_recovery._project_mixed_recovery_material
    original_summary = active_turn_compact._active_turn_replacement_summary
    sent_business, sent_summaries = [], []
    projected_candidates = []
    in_summary = False

    if failure == "unread_retained_ir":
        from agent_py_agent.agent.agent_core.runtime import loop_support

        original_loop_params = loop_support._tool_loop_execute_params

        def loop_params_with_unread(*args, **kwargs):
            params = original_loop_params(*args, **kwargs)
            if sent_business:
                # 已采用原IR后到达的未覆盖用户原文仍属于下一次完整请求，不能被工具摘要隐藏。
                params.tool_ir_history.append(UserTurn("未覆盖的用户原文。" * 120_000))
            return params

        monkeypatch.setattr(loop_support, "_tool_loop_execute_params", loop_params_with_unread)

    def project(*args):
        if failure == "candidate_cancel":
            raise InterruptedError("测试取消活动摘要候选")
        if failure == "generation_race":
            store.threads.update_atomic(thread.thread_id, lambda current: replace(
                current, compact_generation=current.compact_generation + 1,
                summary="另一个提交者已经获胜",
            ))
        return original_project(*args)

    def mixed(material, view, max_chars):
        selected = original_mixed(material, view, max_chars)
        if view.is_candidate:
            projected_candidates.append(selected)
        return selected

    def summary(*args, **kwargs):
        nonlocal in_summary
        if failure == "summary_failure":
            raise ProviderTransientError("503 temporary overload")
        in_summary = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary = False

    if failure == "candidate_too_large":
        original_ceiling = compact._compact_request_input_ceiling
        monkeypatch.setattr(
            compact, "_compact_request_input_ceiling",
            lambda *args: 1 if sent_business else original_ceiling(*args),
        )
    monkeypatch.setattr(background_compact_recovery, "_project_background_active_candidate", project)
    monkeypatch.setattr(compact_request_recovery, "_project_mixed_recovery_material", mixed)
    monkeypatch.setattr(active_turn_compact, "_active_turn_replacement_summary", summary)

    def on_business(wire, _number):
        if in_summary:
            sent_summaries.append(wire)
            return
        sent_business.append(wire)
        if len(sent_business) == 1:
            raise ProviderContextWindowError("测试供应商上下文溢出")
        pytest.fail("活动摘要未提交时发送了恢复业务请求")

    business, _ = _http(monkeypatch, backend="anthropic_compatible", on_business=on_business)
    with pytest.raises((ConversationCompactError, InterruptedError)):
        background_execution.run_background_turn_with_compact(
            execution, thread, request, user_prompt="报告本次新发现", continuation_injection=[],
            proactive_delivery_available=False, activity_sink=sink,
        )
    assert len(sent_business) == 1
    assert len(sent_summaries) == (0 if failure == "summary_failure" else 1)
    assert len(business) == 1 + len(sent_summaries)
    if failure == "unread_retained_ir":
        assert len(projected_candidates) == 1
        assert any(isinstance(item, UserTurn) and "未覆盖的用户原文" in item.text
                   for item in projected_candidates[0].request_input.tool_ir_history)
    committed = store.threads.require(thread.thread_id)
    assert committed.compact_generation == (1 if failure == "generation_race" else 0)
    if failure == "generation_race":
        assert committed.summary == "另一个提交者已经获胜"
