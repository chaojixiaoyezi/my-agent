

from __future__ import annotations

from dataclasses import dataclass

from ..backends import ModelResponse
from ..backends.errors import (
    is_empty_provider_response_error,
    is_incomplete_provider_response_error,
)
from ..concurrency.interrupt import is_interrupted
from ..conversation.authority import conversation_transcript_is_authoritative
from ..memory_archive import estimate_tokens
from ..prompting_parts.builder import ToolSections
from ..settings.runtime_guard_config import runtime_guard_int
from ..subagents.services.session_progress import record_runtime_subagent_tool_progress
from ._runtime_params import ToolLoopExecuteParams
from .delivery_contract_prompting import render_delivery_contract_section
from .native_tool_protocol import native_tool_use_active, resolve_native_tools
from .orchestration.shared_context import (
    refresh_parent_shared_context_cache,
    refresh_parent_shared_context_from_tool_record,
)
from .provider_transient_auto_resume import run_with_provider_transient_auto_resume
from .runner.context import current_task_attributes
from .runner.stage_trace import RunnerToolStageTraceRequest, trace_runner_tool_call_started
from .runtime.goal_accounting import account_goal_model_response, begin_goal_model_turn
from .runtime.guidance import (
    acknowledge_injected_turn_input,
    has_pending_turn_input,
    inject_pending_turn_input,
)
from .runtime.live_archive import (
    archive_tool_call_if_enabled,
    update_runtime_fact_progress_if_enabled,
)
from .subagent.attempt_guard import stale_subagent_attempt_message
from .tool_call_archive_record import archive_tool_call_record
from .tool_call_runtime import (
    ToolCallRuntimeRequest,
    execute_traced_tool_call,
    guarded_tool_call_result,
)
from .tool_context.call_reducer import render_tool_payload_for_live_prompt
from .tool_context.reducer import render_tool_result_for_live_prompt
from .tool_context.window import tool_context_window_max_chars, window_tool_context_params
from .tool_guard.call_guardrail import record_tool_guard_observation
from .tool_guard.loop_hints import (
    append_tool_failure_channel_hint,
    append_tool_guardrail_action_block_hint,
)
from .tool_ir_compact import (
    compact_native_ir_to_char_budget,
    reclaim_oldest_native_ir_pairs,
)
from .tool_ir_history import record_tool_call_ir
from .tool_loop.completion import (
    ToolRoundCompletionRequest,
    completion_response_after_tool_round,
    queue_interim_reply_for_open_subagents,
)
from .tool_loop.natural_user_reply import (
    discard_pending_natural_user_reply,
    finish_natural_user_reply,
    natural_user_reply_model_params,
    natural_user_reply_rejection_reason,
    pending_natural_user_reply,
    retry_natural_user_reply,
)
from .tool_loop.recovery import (
    append_long_content_recovery_context,
    without_tool_call_after_limit,
)
from .tool_loop.response_decision import (
    ToolLoopRepairCounters,
    ToolLoopResponseDecisionRequest,
    tool_loop_response_decision,
)
from .tool_loop.round_execution import (
    ToolCallExecuteParams,
    ToolCallRecordParams,
    ToolRoundExecutionRequest,
    execute_tool_round,
)
from .tool_model_generation import ModelGenerateParams, generate_model_response
from .tool_runtime_ledger import persist_tool_runtime_ledger

_ORCHESTRATION_TOOLS = {
    "create_subagents",
    "dispatch_subagents",
    "schedule_child_subagents",
}


@dataclass(frozen=True)
class _ToolStepRequest:
    params: ToolLoopExecuteParams
    tool_rounds: int
    action: object
    current_prompt: str


@dataclass(frozen=True)
class _PendingDeferredToolDrainResult:
    tool_rounds: int
    final_response: ModelResponse | None = None


def _effective_max_tool_rounds(agent, params: ToolLoopExecuteParams) -> int:
    config = getattr(agent, "config", None)
    if hasattr(config, "max_tool_rounds"):
        effective = getattr(config, "max_tool_rounds", None)
    else:
        effective = runtime_guard_int(
            "max_tool_rounds",
            0,
            policy=getattr(agent, "runtime_guard_policy", None),
        )
    attrs_to_check = params.task_attributes or current_task_attributes(agent)
    if attrs_to_check and "max_tool_rounds" in attrs_to_check:
        effective = attrs_to_check["max_tool_rounds"]
    try:
        return max(0, int(effective))
    except (TypeError, ValueError):
        return 0


def _should_retry_empty_model_response(
    params: ToolLoopExecuteParams,
    exc: Exception,
    provider_response_repairs: int,
) -> bool:
    return (
        is_empty_provider_response_error(exc)
        and bool(params.executed_tools)
        and provider_response_repairs < 1
    )


def _should_retry_incomplete_model_response(
    params: ToolLoopExecuteParams,
    exc: Exception,
    provider_response_repairs: int,
) -> bool:
    # 长期助手 continues a length-truncated tool turn instead of discarding all
    # completed work.  Keep the retry bounded and only enable it after durable
    # tool results exist; a truncated ordinary chat answer remains terminal,
    # matching 会话运行时's response.incomplete handling.
    return (
        is_incomplete_provider_response_error(exc)
        and bool(params.executed_tools)
        and provider_response_repairs < 1
    )


def _pending_turn_input_supersedes_unusable_response(
    agent: object,
    params: ToolLoopExecuteParams,
    exc: Exception,
) -> bool:
    """Keep a 会话运行时 steer in the same turn when the stale call has no usable result."""
    unusable = is_empty_provider_response_error(exc) or is_incomplete_provider_response_error(exc)
    if not unusable or not has_pending_turn_input(agent, params):
        return False
    discard_pending_natural_user_reply(params)
    # Move the durable mailbox item into this turn before retrying.  The normal
    # acknowledgement still happens only after the next model response accepts
    # the prompt, so a crash leaves the guidance replayable.
    return inject_pending_turn_input(agent, params)


def _empty_model_response_retry_context(params: ToolLoopExecuteParams) -> str:
    tools = ", ".join(str(item) for item in params.executed_tools[-6:]) or "(none)"
    return "\n".join(
        [
            "[tool-system]",
            "上一轮模型接口返回了空文本；真实工具调用和工具结果已经保留在上方 tool-record/tool-output-record 中。",
            f"recent_executed_tools: {tools}",
            "请基于这些已完成结果继续：任务未完成就调用下一步工具，任务已完成才给最终回答。不要从头重复读取同一批材料。",
        ]
    )


def _incomplete_model_response_retry_context(
    params: ToolLoopExecuteParams,
    exc: Exception,
) -> str:
    details = getattr(exc, "details", None)
    details = details if isinstance(details, dict) else {}
    stop_reason = str(details.get("stop_reason") or "unknown")
    try:
        partial_chars = max(0, int(details.get("partial_text_chars") or 0))
    except (TypeError, ValueError):
        partial_chars = 0
    tools = ", ".join(str(item) for item in params.executed_tools[-6:]) or "(none)"
    return "\n".join(
        [
            "[tool-system:model-incomplete-response]",
            "上一轮模型响应被供应商输出上限截断；半截正文和半截工具参数都没有被当作成功或执行。",
            f"stop_reason: {stop_reason}",
            f"discarded_partial_text_chars: {partial_chars}",
            f"recent_executed_tools: {tools}",
            "真实 tool-record/tool-output-record 与已写产物仍然有效。请从当前进度继续，"
            "不要重头复述或重复读取；优先直接发出下一步小而完整的工具调用，"
            "若工作已经完成则只给简洁最终答复。",
        ]
    )


def build_tool_loop_prompt(agent, params: ToolLoopExecuteParams) -> str:
    if params.consume_pending_turn_input:
        inject_pending_turn_input(agent, params)
    conversation_owned = _conversation_owns_compaction(params)
    if not conversation_owned:
        window_tool_context_params(agent, params)
    # native 下文本 tool_context 不发往 provider（IR messages 才发），所以上面的文本
    # 窗口只是为旁路口径；真正决定发出去多大上下文的是 IR。这里按同样的字符预算对 IR
    # 整对窗口化——绝不能只挖结果留 tool_use 头（那就是 Anthropic 400 的孤儿）。
    if not conversation_owned:
        _window_native_ir_to_budget(agent, params)
    prompt = _render_tool_loop_prompt(agent, params)
    if conversation_owned:
        prompt = _compact_live_conversation_tool_context(agent, params, prompt)
    else:
        prompt = _fit_non_conversation_model_input(agent, params, prompt)
    return prompt


def _render_tool_loop_prompt(agent, params: ToolLoopExecuteParams) -> str:
    return agent.prompts.build(
        params.user_prompt,
        params.memories,
        inject=_runtime_injections_with_delivery_contract(params),
        prompt_files=params.prompt_files,
        system_prompt_override=params.system_prompt_override,
        context_scope=params.context_scope,
        workspace_context_override=params.workspace_context_snapshot or None,
        tools=ToolSections(
            tool_catalog_section=params.tool_catalog_section,
            tool_recommendations_section=params.tool_recommendations_section,
            tool_context=params.tool_context,
            # native 下工具往返由原生 messages 携带，prompt 旁路 tool_context 文本折入。
            native_tool_use=native_tool_use_active(agent),
        ),
    )


def _conversation_owns_compaction(params: ToolLoopExecuteParams) -> bool:
    return bool(
        str(getattr(params, "context_scope", "") or "") == "conversation"
        and conversation_transcript_is_authoritative(params.task_attributes)
    )


def _fit_non_conversation_model_input(
    agent: object,
    params: ToolLoopExecuteParams,
    prompt: str,
) -> str:
    """Fit archived tool history against the whole provider input budget.

    The older window bounded only ``tool_context`` characters.  A large tool
    catalog, Persona entries, and the task prompt could therefore push the
    complete request past the model window even though the tool slice itself
    was within its limit.  会话运行时 budgets the model-visible request as a whole;
    reuse the same full-input fitter already used by conversation turns.
    """

    from .runtime.context_compactor import runtime_compact_policy

    save = getattr(params, "save", None)
    if save is None:
        save = bool(getattr(getattr(agent, "config", None), "auto_save_memory", True))
    policy = runtime_compact_policy(
        agent,
        save=bool(save),
        context_scope=str(getattr(params, "context_scope", "") or "default"),
    )
    limit = int(
        policy.trigger_tokens
        if policy.allow_persistent_apply
        else policy.context_window_tokens
    )
    if limit <= 0 or _model_visible_context_tokens(agent, params, prompt) < limit:
        return prompt
    if native_tool_use_active(agent):
        prompt, _ = _fit_native_ir_below_threshold(
            agent,
            params,
            threshold_tokens=limit,
        )
    if _model_visible_context_tokens(agent, params, prompt) >= limit:
        prompt, _ = _fit_tool_context_below_threshold(
            agent,
            params,
            threshold_tokens=limit,
        )
    return prompt


def _compact_live_conversation_tool_context(
    agent: object,
    params: ToolLoopExecuteParams,
    prompt: str,
) -> str:
    """会话运行时 mid-turn replacement of old tool history in one thread.

    The 90% setting remains the exact trigger.  Once crossed, archived tool
    calls are rebuilt as a bounded semantic handoff and recent records, then
    the *whole rendered request* is measured again.  Raw tool records stay in
    the owner archive; no second task/session compact chain is created.
    """

    from .runtime.context_compactor import runtime_compact_policy

    policy = runtime_compact_policy(
        agent,
        save=True,
        context_scope=str(getattr(params, "context_scope", "") or "default"),
    )
    threshold = int(policy.trigger_tokens or 0)
    before_tokens = _model_visible_context_tokens(agent, params, prompt)
    if threshold <= 0 or before_tokens < threshold:
        return prompt
    original = list(params.tool_context or [])
    if not original and not list(getattr(params, "tool_ir_history", None) or []):
        return prompt

    compacted = _semantic_tool_context_replacement(agent, params, original)
    params.tool_context[:] = compacted
    rendered = _render_tool_loop_prompt(agent, params)
    after_tokens = _model_visible_context_tokens(agent, params, rendered)

    # 会话运行时 replaces pressured history with one compact item and then continues
    # against that replacement.  If our semantic replacement still leaves the
    # request at the same effective size, use the protected head/tail handoff
    # instead of repeatedly compacting the same archive snapshot.
    if after_tokens >= threshold or after_tokens >= int(before_tokens * 0.95):
        params.tool_context[:] = _bounded_tool_context_fallback(params, compacted)
        rendered = _render_tool_loop_prompt(agent, params)
        after_tokens = _model_visible_context_tokens(agent, params, rendered)

    if native_tool_use_active(agent) and after_tokens >= threshold:
        rendered, after_tokens = _fit_native_ir_below_threshold(
            agent,
            params,
            threshold_tokens=threshold,
        )

    if after_tokens >= threshold:
        rendered, after_tokens = _fit_tool_context_below_threshold(
            agent,
            params,
            threshold_tokens=threshold,
        )

    _record_live_context_compaction(
        params,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        threshold_tokens=threshold,
        original_entries=len(original),
        retained_entries=len(params.tool_context),
    )
    return rendered


def _model_visible_context_tokens(
    agent: object,
    params: ToolLoopExecuteParams,
    prompt: str,
) -> int:
    """Estimate the whole provider input, including native tool messages."""

    if not native_tool_use_active(agent):
        return estimate_tokens(prompt)
    history = list(getattr(params, "tool_ir_history", None) or [])
    if not history:
        return estimate_tokens(prompt)
    from ..backends.message_adapter import AnthropicMessageAdapter
    from .tool_ir_guidance import unforwarded_runtime_guidance

    messages = AnthropicMessageAdapter().to_provider_messages(history)
    state = getattr(params, "live_archive_state", None)
    already_forwarded = (
        set(state.get("_forwarded_runtime_guidance", set()))
        if isinstance(state, dict)
        else set()
    )
    guidance = unforwarded_runtime_guidance(
        getattr(params, "tool_context", None),
        already_forwarded,
    )
    return estimate_tokens(
        {
            "initial_user_prompt": prompt,
            "messages": messages,
            "pending_runtime_guidance": guidance,
            "tools": resolve_native_tools(agent, params) or [],
        }
    )


def _fit_native_ir_below_threshold(
    agent: object,
    params: ToolLoopExecuteParams,
    *,
    threshold_tokens: int,
) -> tuple[str, int]:
    """Drop oldest native call/result pairs after creating an archive handoff."""

    rendered = _render_tool_loop_prompt(agent, params)
    tokens = _model_visible_context_tokens(agent, params, rendered)
    while tokens >= threshold_tokens:
        removed = reclaim_oldest_native_ir_pairs(params, fraction=0.0)
        if removed <= 0:
            break
        next_tokens = _model_visible_context_tokens(agent, params, rendered)
        if next_tokens >= tokens:
            break
        tokens = next_tokens
    return rendered, tokens


def _semantic_tool_context_replacement(
    agent: object,
    params: ToolLoopExecuteParams,
    original: list[str],
) -> list[str]:
    from .runtime.active_turn_input import active_turn_user_input_texts
    from .runtime.loop_support import _reconstructed_runtime_state

    records = list(params.archive_tool_calls or [])
    reconstructed = (
        _reconstructed_runtime_state(records, agent=agent).tool_context
        if records
        else []
    )
    active_inputs = [
        f"[ACTIVE_TURN_USER_INPUT]\n{text}"
        for text in active_turn_user_input_texts(params.active_turn_user_inputs)
    ]
    # Runtime guidance has no archive record.  Preserve only its recent bounded
    # tail; tool calls/results themselves come from the typed archive above.
    guidance = [item for item in original if _preserve_live_guidance(item)][-6:]
    return _dedupe_context_entries([*reconstructed, *guidance, *active_inputs])


def _preserve_live_guidance(value: object) -> bool:
    text = str(value or "").lstrip()
    return text.startswith(
        (
            "[ACTIVE_TURN_USER_INPUT]",
            "[tool-system]",
            "[tool-loop-guardrail-hint]",
            "[tool-system:long-read-facts]",
        )
    )


def _dedupe_context_entries(entries: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in entries:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _bounded_tool_context_fallback(
    params: ToolLoopExecuteParams,
    entries: list[str],
) -> list[str]:
    summaries = [item for item in entries if item.startswith("[compact-semantic-summary]")]
    active = [item for item in entries if item.startswith("[ACTIVE_TURN_USER_INPUT]")]
    ordinary = [item for item in entries if item not in summaries and item not in active]
    refs = _recent_tool_archive_refs(params.archive_tool_calls)
    marker = "\n".join(
        [
            "[tool-context-window]",
            "- older tool calls were replaced after crossing the configured compact threshold.",
            "- raw tool records remain authoritative; do not replay completed side effects.",
            f"- recent_archive_refs: {refs}" if refs else "- recent_archive_refs: []",
            *_recent_tool_archive_handoff(params.archive_tool_calls),
        ]
    )
    # 会话运行时 keeps the current input and a bounded recent tail around one
    # compaction summary.  Keep the same shape here; the raw archive is not
    # deleted and can be read back by ref.
    return _dedupe_context_entries(
        [marker, *summaries[-1:], *ordinary[:2], *ordinary[-6:], *active]
    )


def _fit_tool_context_below_threshold(
    agent: object,
    params: ToolLoopExecuteParams,
    *,
    threshold_tokens: int,
) -> tuple[str, int]:
    """Fit the compacted tool handoff against the same configured threshold.

    This is the deterministic failure path after semantic compaction made no
    material progress.  It never changes the 90% trigger and never touches the
    raw archive.  Old mechanical entries go first, then an oversized summary
    is head/tail clipped.  Active user steering and the archive-ref marker are
    protected.  If those protected inputs alone exceed the model budget, the
    caller deliberately leaves the prompt over threshold so the durable
    conversation transcript can compact.
    """

    rendered = _render_tool_loop_prompt(agent, params)
    tokens = _model_visible_context_tokens(agent, params, rendered)
    while tokens >= threshold_tokens:
        removable = _oldest_mechanical_context_index(params.tool_context)
        if removable is not None:
            del params.tool_context[removable]
        else:
            summary_index = _largest_summary_context_index(params.tool_context)
            if summary_index is None:
                break
            summary = str(params.tool_context[summary_index] or "")
            if len(summary) <= 640:
                del params.tool_context[summary_index]
            else:
                params.tool_context[summary_index] = _clip_context_head_tail(
                    summary,
                    max_chars=max(640, len(summary) // 2),
                )
        rendered = _render_tool_loop_prompt(agent, params)
        next_tokens = _model_visible_context_tokens(agent, params, rendered)
        if next_tokens >= tokens and not params.tool_context:
            break
        tokens = next_tokens
    return rendered, tokens


def _oldest_mechanical_context_index(entries: list[str]) -> int | None:
    for index, item in enumerate(entries):
        text = str(item or "").lstrip()
        if text.startswith(("[tool-context-window]", "[ACTIVE_TURN_USER_INPUT]")):
            continue
        if text.startswith("[compact-semantic-summary]"):
            continue
        return index
    return None


def _largest_summary_context_index(entries: list[str]) -> int | None:
    candidates = [
        (len(str(item or "")), index)
        for index, item in enumerate(entries)
        if str(item or "").lstrip().startswith("[compact-semantic-summary]")
    ]
    return max(candidates, default=(0, None))[1]


def _clip_context_head_tail(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    marker = "\n...[compact summary clipped to configured context budget]...\n"
    budget = max(0, max_chars - len(marker))
    head = int(budget * 0.6)
    tail = budget - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def _recent_tool_archive_refs(records: list[dict[str, object]] | None) -> list[str]:
    refs: list[str] = []
    for record in reversed(list(records or [])):
        for key in ("scoped_call_id", "artifact_ref", "output_path"):
            value = str(record.get(key) or "").strip()
            if value and value not in refs:
                refs.append(value)
                break
        if len(refs) >= 8:
            break
    return list(reversed(refs))


def _recent_tool_archive_handoff(
    records: list[dict[str, object]] | None,
) -> list[str]:
    rows: list[str] = []
    for record in list(records or [])[-6:]:
        if not isinstance(record, dict):
            continue
        tool = str(record.get("tool") or "unknown").strip() or "unknown"
        status = "ok" if bool(record.get("ok")) else "error"
        ref = next(
            (
                str(record.get(key) or "").strip()
                for key in ("scoped_call_id", "artifact_ref", "output_path")
                if str(record.get(key) or "").strip()
            ),
            "",
        )
        preview = str(record.get("output_preview") or "").strip().replace("\n", " ")
        if len(preview) > 240:
            preview = preview[:237] + "..."
        detail = f"tool={tool} status={status}"
        if ref:
            detail += f" ref={ref}"
        if preview:
            detail += f" preview={preview}"
        rows.append(f"- recent_tool_fact: {detail}")
    return rows


def _record_live_context_compaction(
    params: ToolLoopExecuteParams,
    *,
    before_tokens: int,
    after_tokens: int,
    threshold_tokens: int,
    original_entries: int,
    retained_entries: int,
) -> None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return
    state["conversation_tool_context_compaction"] = {
        "before_tokens": max(0, int(before_tokens)),
        "after_tokens": max(0, int(after_tokens)),
        "threshold_tokens": max(0, int(threshold_tokens)),
        "original_entries": max(0, int(original_entries)),
        "retained_entries": max(0, int(retained_entries)),
        "archive_record_count": len(list(params.archive_tool_calls or [])),
        "below_threshold": after_tokens < threshold_tokens,
        "material_reduction": after_tokens < int(before_tokens * 0.95),
    }


def _window_native_ir_to_budget(agent, params: ToolLoopExecuteParams) -> None:
    """native 下把 IR 历史按字符预算整对窗口化（text 协议跳过，行为零变）。

    复用 ``tool_context_window_max_chars`` 的预算口径（与文本 window 同源），从最旧
    工具往返开始整对摘除直到落进预算。``drop_tool_call_pairs`` 保证摘的是「ToolCall +
    配对 ToolResult」整对，出站 messages 不留孤儿。
    """
    if not native_tool_use_active(agent):
        return
    compact_native_ir_to_char_budget(params, max_chars=tool_context_window_max_chars(agent))


def _runtime_injections_with_delivery_contract(params: ToolLoopExecuteParams) -> list:
    if not isinstance(params.delivery_contract, dict):
        return params.runtime_injections
    return [*params.runtime_injections, render_delivery_contract_section(params.delivery_contract)]


def next_tool_loop_model_response(agent, params: ToolLoopExecuteParams, tool_rounds: int):
    _discard_stale_natural_reply_for_pending_turn_input(agent, params)
    model_params = natural_user_reply_model_params(params)
    prompt = build_tool_loop_prompt(agent, model_params)
    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=model_params,
            prompt=prompt,
            tool_rounds=tool_rounds,
        )
    )
    return _retry_after_provider_context_overflow(
        agent,
        model_params,
        tool_rounds,
        first=(prompt, response),
    )


def _natural_user_reply_step(
    params: ToolLoopExecuteParams,
    response: ModelResponse,
) -> tuple[str, ModelResponse]:
    if pending_natural_user_reply(params) is None:
        return "normal", response
    rejection_reason = natural_user_reply_rejection_reason(
        response,
        pending_natural_user_reply(params),
    )
    accepted = not rejection_reason
    if not accepted and retry_natural_user_reply(params, rejection_reason=rejection_reason):
        return "retry", response
    return "finish", finish_natural_user_reply(
        params,
        response,
        accepted=accepted,
        rejection_reason=rejection_reason,
    )


# LLM: 单轮 PTL retry（compact 三件套之三，蓝本 终端交互 truncateHeadForPTLRetry）。
#   只接 provider 实报的 context_overflow（runtime_source=provider_error）；preflight
#   预测溢出仍走 compact，不抢跑。每次回收最老 20% 工具结果正文后重拼 prompt 重试，
#   上限 tool_context_ptl_retry_max（0=关闭）；无可回收或仍溢出时返回最后的溢出响应，
#   落回原有 compact/resume 路径，保证永不卡死。
# 函数用途: 模型报"上下文超限"时先丢最老工具输出做轻量重试，省一次重量级 compact。
def _retry_after_provider_context_overflow(
    agent,
    params: ToolLoopExecuteParams,
    tool_rounds: int,
    *,
    first: tuple[str, object],
):
    from .tool_context.ptl_retry import DEFAULT_PTL_RETRY_MAX

    prompt, response = first
    retry_max = int(getattr(getattr(agent, "config", None), "tool_context_ptl_retry_max", DEFAULT_PTL_RETRY_MAX) or 0)
    retries = 0
    while retries < retry_max and _is_provider_context_overflow(response):
        if not _ptl_reclaim_oldest(agent, params):
            break
        retries += 1
        prompt = build_tool_loop_prompt(agent, params)
        response = generate_model_response(
            ModelGenerateParams(
                agent=agent,
                params=params,
                prompt=prompt,
                tool_rounds=tool_rounds,
            )
        )
    return prompt, response


# 函数用途: 判定响应是不是 provider 实报的上下文超限（排除 preflight 预测）。
def _is_provider_context_overflow(response) -> bool:
    return (
        str(getattr(response, "runtime_status", "") or "") == "context_overflow"
        and str(getattr(response, "runtime_source", "") or "") == "provider_error"
    )


# LLM: PTL 单步回收的协议分流。native 下 provider 看到的是 IR 翻出的 messages（不是
#   文本 tool_context），所以必须丢 IR 整对（drop_tool_call_pairs，无孤儿）才真的瘦身；
#   text 协议保持原样丢文本正文。两侧都返回「是否还有可回收」，无可回收即停止 PTL 重试、
#   落回重量级 compact/resume。
# 函数用途: PTL 重试前按协议给「真正发往 provider 的上下文」瘦身一步。
def _ptl_reclaim_oldest(agent, params: ToolLoopExecuteParams) -> bool:
    from .tool_context.ptl_retry import _PTL_DROP_FRACTION, reclaim_oldest_tool_results_for_ptl

    if native_tool_use_active(agent):
        return reclaim_oldest_native_ir_pairs(params, fraction=_PTL_DROP_FRACTION) > 0
    return reclaim_oldest_tool_results_for_ptl(params.tool_context) > 0


def execute_one_tool_call(agent, request: ToolCallExecuteParams):
    sentinel = object()
    previous = getattr(agent, "_current_tool_loop_params", sentinel)
    agent._current_tool_loop_params = request.params
    try:
        return _execute_scoped_tool_call(agent, request)
    finally:
        _restore_tool_loop_params(agent, previous, sentinel)


def _execute_scoped_tool_call(agent, request: ToolCallExecuteParams):
    payload = request.payload
    trace_request = RunnerToolStageTraceRequest(
        agent=agent,
        params=request.params,
        tool_rounds=request.tool_rounds,
        idx=request.idx,
        payload=payload,
    )
    trace_runner_tool_call_started(trace_request)
    runtime_request = ToolCallRuntimeRequest(agent, request, payload, trace_request)
    guard_result = guarded_tool_call_result(runtime_request)
    if guard_result is not None:
        return guard_result
    return execute_traced_tool_call(runtime_request)


def _restore_tool_loop_params(agent, previous: object, sentinel: object) -> None:
    if previous is sentinel:
        try:
            delattr(agent, "_current_tool_loop_params")
        except AttributeError:
            pass
        return
    agent._current_tool_loop_params = previous


def execute_tool_loop(agent, params: ToolLoopExecuteParams):
    return _execute_tool_loop_service(ToolLoopService(agent), params)


def _discard_stale_natural_reply_for_pending_turn_input(agent, params: ToolLoopExecuteParams) -> bool:
    """Keep task input out of the presentation-only receipt round."""
    if pending_natural_user_reply(params) is None or not has_pending_turn_input(agent, params):
        return False
    return discard_pending_natural_user_reply(params)


def _pending_turn_input_invalidates_response(agent, params: ToolLoopExecuteParams) -> bool:
    if not has_pending_turn_input(agent, params):
        return False
    discard_pending_natural_user_reply(params)
    return True


def _execute_tool_loop_service(service: ToolLoopService, params: ToolLoopExecuteParams):
    final_prompt, final_response = "", None
    tool_rounds = params.tool_rounds
    repair_counters, provider_response_repairs = ToolLoopRepairCounters(), 0

    while True:
        if is_interrupted():
            final_response = _interrupted_conversation_response(service._agent)
            break
        tool_rounds, pending_final, drained = _pending_drain_outcome(service, params, tool_rounds)
        if drained and pending_final is None:
            continue
        if drained:
            final_response = pending_final
            break
        (
            final_prompt,
            final_response,
            should_stop,
            retry_after_provider_response,
            provider_response_repairs,
        ) = service._model_turn_or_retry(params, tool_rounds, provider_response_repairs)
        if retry_after_provider_response:
            continue
        if is_interrupted():
            final_response = _interrupted_conversation_response(service._agent)
            break
        if should_stop:
            break
        # /btw 可能在 provider 正在生成时到达；旧响应此时已过期，不能据此开工具或结束任务。
        if _pending_turn_input_invalidates_response(service._agent, params):
            continue
        natural_reply_verdict, final_response = _natural_user_reply_step(params, final_response)
        if natural_reply_verdict == "retry":
            continue
        if natural_reply_verdict == "finish":
            break
        repair_counters, action = _response_action(service._agent, params, final_response, repair_counters)
        # 会话运行时 root/child lifecycle boundary: only a plain final response is
        # deferred.  Real tool calls remain executable while children run.
        if action.action == "break" and queue_interim_reply_for_open_subagents(
            service._agent,
            params,
            tool_rounds=tool_rounds,
        ):
            continue
        verdict, routed_response = _routed_action_step(action)
        if verdict == "continue":
            continue
        if verdict == "stop":
            final_response = routed_response if routed_response is not None else final_response
            break
        final_prompt, final_response, tool_rounds = _tool_step_or_limit(
            service,
            _ToolStepRequest(
                params=params,
                tool_rounds=tool_rounds,
                action=action,
                current_prompt=final_prompt,
            ),
        )
        if final_response:
            break

    return final_prompt, final_response, tool_rounds


# LLM: Interrupt exits the main loop without asking the model to reinterpret a cancellation flag.
# 函数用途：构造统一的用户可见停止结果，阻止中断后继续收口或派发工具。
def _interrupted_conversation_response(agent: object) -> ModelResponse:
    backend = str(getattr(getattr(agent, "backend", None), "name", "") or "tool_loop")
    return ModelResponse(
        text="当前任务已停止。",
        backend=backend,
        runtime_status="cancelled",
        runtime_reason="user_stop",
        runtime_source="conversation_control",
    )


# LLM: 会话运行时 turn semantics: a model final response ends the turn directly;
# runtime errors and structured control states are handled by their own paths, not an artifact scanner.
# 函数用途: 把模型响应的"继续/结束/执行工具"三岔路收成一次裁决,主循环保持扁平。
def _routed_action_step(action):
    if action.action == "continue":
        return "continue", None
    if action.action != "break":
        return "tools", None
    return "stop", action.response


# 函数用途: 把 pending 延迟工具调用的双分支收成一次裁决,返回(轮数, 最终回复, 是否命中)。
def _pending_drain_outcome(service, params, tool_rounds):
    pending = _drain_pending_deferred_tool_calls(service, params, tool_rounds)
    if pending is None:
        return tool_rounds, None, False
    return pending.tool_rounds, pending.final_response, True


class ToolLoopService:
    def __init__(self, agent):
        self._agent = agent

    def execute(self, params: ToolLoopExecuteParams):
        return _execute_tool_loop_service(self, params)

    def _model_turn_or_retry(
        self,
        params: ToolLoopExecuteParams,
        tool_rounds: int,
        provider_response_repairs: int,
    ):
        return _model_turn_or_retry(self._agent, params, tool_rounds, provider_response_repairs)

    def _run_tool_round(self, request: ToolRoundExecutionRequest):
        return _run_tool_round(self._agent, request)

    def _tool_round_limit_reached(self, params: ToolLoopExecuteParams, tool_rounds: int) -> bool:
        return _tool_round_limit_reached(self._agent, params, tool_rounds)

    def _final_response_after_tool_limit(self, params: ToolLoopExecuteParams, tool_rounds: int):
        return _final_response_after_tool_limit(self._agent, params, tool_rounds)

    def _execute_one_tool_call(self, request: ToolCallExecuteParams):
        return execute_one_tool_call(self._agent, request)

    def _record_tool_call(self, record: ToolCallRecordParams) -> None:
        _record_tool_call(self._agent, record)


def _drain_pending_deferred_tool_calls(
    service: ToolLoopService,
    params: ToolLoopExecuteParams,
    tool_rounds: int,
) -> _PendingDeferredToolDrainResult | None:
    pending_calls = _pop_pending_deferred_tool_calls(params)
    if not pending_calls:
        return None
    if service._tool_round_limit_reached(params, tool_rounds):
        final_prompt, final_response = service._final_response_after_tool_limit(params, tool_rounds)
        del final_prompt
        return _PendingDeferredToolDrainResult(tool_rounds, final_response)
    next_round = tool_rounds + 1
    next_round, final_response = service._run_tool_round(
        ToolRoundExecutionRequest(
            service._agent,
            params,
            next_round,
            ModelResponse(text="[PENDING_DEFERRED_TOOL_CALLS]", backend="tool_loop"),
            pending_calls,
            service._execute_one_tool_call,
            service._record_tool_call,
            _deferred_drain_prompt(service._agent, params),
        ),
    )
    return _PendingDeferredToolDrainResult(next_round, final_response)


def _model_turn_or_retry(
    agent,
    loop_params: ToolLoopExecuteParams,
    tool_rounds: int,
    provider_response_repairs: int,
):
    stale_message = stale_subagent_attempt_message(agent)
    if stale_message is not None:
        backend = str(getattr(getattr(agent, "backend", None), "name", "") or "")
        return (
            "",
            ModelResponse(text=stale_message, backend=backend),
            True,
            False,
            provider_response_repairs,
        )
    try:
        begin_goal_model_turn(agent, loop_params)
        prompt, response = run_with_provider_transient_auto_resume(
            lambda: next_tool_loop_model_response(agent, loop_params, tool_rounds),
            on_chunk=loop_params.effective_on_chunk,
            policy=getattr(agent, "runtime_guard_policy", None),
        )
        account_goal_model_response(agent, loop_params, response)
        # Runtime events stay durable while the provider is in flight. Acknowledge
        # only after one model response was successfully generated from the prompt
        # that contained them; a crash/error before this point leaves them retryable.
        acknowledge_injected_turn_input(agent, loop_params)
        # 会话运行时/长期助手 scope stream retries to one sampling request.  A later
        # successful model turn proves the provider recovered, so an isolated
        # empty response many tool rounds later gets its own bounded repair
        # instead of inheriting a stale retry count from the whole long task.
        return prompt, response, False, False, 0
    except Exception as exc:
        # 会话运行时 queues steer input on the active turn.  If the provider call
        # that was already in flight then ends without a usable response, that
        # stale empty output is not the turn's terminal result: consume the
        # typed mailbox item at this safe point and run the same turn again.
        if _pending_turn_input_supersedes_unusable_response(agent, loop_params, exc):
            return "", None, False, True, provider_response_repairs
        if _should_retry_empty_model_response(loop_params, exc, provider_response_repairs):
            context = _empty_model_response_retry_context(loop_params)
        elif _should_retry_incomplete_model_response(
            loop_params,
            exc,
            provider_response_repairs,
        ):
            context = _incomplete_model_response_retry_context(loop_params, exc)
        else:
            raise
        loop_params.tool_context.append(context)
        return (
            build_tool_loop_prompt(agent, loop_params),
            None,
            False,
            True,
            provider_response_repairs + 1,
        )


def _response_action(agent, loop_params: ToolLoopExecuteParams, response, repair_counters: ToolLoopRepairCounters):
    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent,
            loop_params,
            response,
            repair_counters,
        )
    )
    return decision.counters, decision


def _tool_step_or_limit(service: ToolLoopService, request: _ToolStepRequest):
    if is_interrupted():
        return (
            request.current_prompt,
            _interrupted_conversation_response(service._agent),
            request.tool_rounds,
        )
    if has_pending_turn_input(service._agent, request.params):
        return request.current_prompt, None, request.tool_rounds
    if service._tool_round_limit_reached(request.params, request.tool_rounds):
        final_prompt, final_response = service._final_response_after_tool_limit(
            request.params,
            request.tool_rounds,
        )
        return final_prompt, final_response, request.tool_rounds
    next_round = request.tool_rounds + 1
    next_round, final_response = service._run_tool_round(
        ToolRoundExecutionRequest(
            service._agent,
            request.params,
            next_round,
            request.action.response,
            request.action.calls,
            service._execute_one_tool_call,
            service._record_tool_call,
            request.current_prompt,
        ),
    )
    return request.current_prompt, final_response, next_round


def _run_tool_round(agent, request: ToolRoundExecutionRequest):
    before_executed_count = len(request.params.executed_tools)
    subagent_output_written = execute_tool_round(request)
    update_runtime_fact_progress_if_enabled(agent, request.params, tool_round=request.tool_rounds)
    append_tool_guardrail_action_block_hint(request)
    # 检索完备性软引导(R5b/R6c 实锤):同一工具系统失败达阈值即提醒枚举未试渠道。
    append_tool_failure_channel_hint(request)
    final_response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent,
            request.params,
            request.response,
            before_executed_count,
            subagent_output_written,
            request.tool_rounds,
        )
    )
    return request.tool_rounds, final_response


def _tool_round_limit_reached(agent, params: ToolLoopExecuteParams, tool_rounds: int) -> bool:
    limit = _effective_max_tool_rounds(agent, params)
    return limit > 0 and tool_rounds >= limit


# LLM: 工具轮数耗尽时由模型基于真实工具记录给出诚实总结；不再交给独立验收器重写正文。
# 函数用途: 轮数到顶时让模型只做总结不再用工具。
def _final_response_after_tool_limit(agent, params: ToolLoopExecuteParams, tool_rounds: int):
    params.tool_context.append("[tool-system]\n已达到最大工具轮数限制，停止继续调用工具。")
    if _executed_subagent_orchestration(params):
        params.tool_context.append("[tool-system]\n子代理调度状态请通过 dispatch_subagents/tree 状态结果继续查看；系统不再替主代理生成最终结论。")
    final_prompt = build_tool_loop_prompt(agent, params)
    final_response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt=final_prompt,
            tool_rounds=tool_rounds,
        )
    )
    final_response = without_tool_call_after_limit(agent, final_response)
    return final_prompt, final_response


def _record_tool_call(agent, record: ToolCallRecordParams) -> None:
    guardrail_hint = record_tool_guard_observation(agent, record.params, record.payload, record.result)
    if record.result.ok and record.result.tool not in {"__parse_error__", "unknown"}:
        record.params.executed_tools.append(record.result.tool)
    archive_record = archive_tool_call_record(agent, record)
    _load_discovered_tools(record.params, archive_record)
    archive_tool_call_if_enabled(
        agent,
        record.params,
        archive_record,
        tool_round=record.tool_rounds,
        tool_index=record.idx,
    )
    persist_tool_runtime_ledger(agent, archive_record)
    record.params.archive_tool_calls.append(archive_record)
    update_runtime_fact_progress_if_enabled(agent, record.params, tool_round=record.tool_rounds)
    refresh_parent_shared_context_cache(agent, record.params.archive_tool_calls)
    refresh_parent_shared_context_from_tool_record(agent, record)
    result_rendered = render_tool_result_for_live_prompt(record.result, archive_record)
    record.params.tool_context.append(
        f"[tool-record round={record.tool_rounds} index={record.idx}]\n"
        f"{render_tool_payload_for_live_prompt(record.payload)}\n"
        f"[tool-output-record round={record.tool_rounds} index={record.idx}]\n"
        f"{result_rendered}"
    )
    # 灰度双轨：native 下同时把这次「调用+结果」记进结构化 IR 历史（与上面的文本
    # tool_context 共存），供出站翻成原生 messages；text 协议下完全不走这里。
    _record_tool_call_ir_if_native(agent, record, archive_record, result_rendered)
    if guardrail_hint:
        record.params.tool_context.append(f"[tool-loop-guardrail-hint]\n{guardrail_hint}")
    append_long_content_recovery_context(record)
    progress = record_runtime_subagent_tool_progress(agent, record)
    if progress:
        record.params.tool_context.append(_task_local_progress_context(progress))


def _load_discovered_tools(params: ToolLoopExecuteParams, archive_record: dict[str, object]) -> None:
    """Apply only the typed tool_search result envelope to the next model turn."""

    envelope = archive_record.get("tool_result_envelope")
    if not isinstance(envelope, dict):
        return
    search = envelope.get("tool_search")
    if not isinstance(search, dict):
        return
    names = search.get("loaded_tool_names")
    if not isinstance(names, list):
        return
    params.loaded_tool_names.update(str(item).strip() for item in names if str(item).strip())


def _record_tool_call_ir_if_native(
    agent,
    record: ToolCallRecordParams,
    archive_record: dict[str, object],
    result_rendered: str,
) -> None:
    """native 下把这次工具调用的「调用+结果」记进结构化 IR 历史（text 协议跳过）。

    真实 provider tool_use id 取自 ``result.call_id``（archive 已用真实入站 id 覆盖
    合成 id）→ 兜底 ``payload["call_id"]``，保证出站 tool_result 的 tool_use_id 与
    assistant tool_use.id 配对。结果 content 复用与文本链路同源的 ``result_rendered``，
    两轨「给模型看到的结果」口径一致。

    Step 5 韧性补缺：文本兜底（漏成正文的 [TOOL_CALL]）若在执行前就被某道 guard 短路
    （agent budget / 一次性去重 / stale subagent），结果是 guard 直接产的，``call_id``
    为空、payload 也无 call_id。此时回填一个与 ``execute_traced_tool_call`` 同约定的
    确定性合成 id ``round-{N}-tool-{idx}``，绝不让空 id 的 tool_use/tool_result 进 IR
    （空 id 对会被出站孤儿净化拆成悬空 tool_use → Anthropic 400）。
    """
    if not native_tool_use_active(agent):
        return
    call_id = str(getattr(record.result, "call_id", "") or "")
    if not call_id and isinstance(record.payload, dict):
        call_id = str(record.payload.get("call_id") or "")
    if not call_id:
        call_id = f"round-{record.tool_rounds}-tool-{record.idx}"
    record_tool_call_ir(
        record.params,
        tool_rounds=record.tool_rounds,
        payload=record.payload,
        call_id=call_id,
        result_content=result_rendered,
        is_error=not record.result.ok,
    )


def _task_local_progress_context(progress: dict[str, object]) -> str:
    load_error = progress.get("load_error")
    status_save_error = progress.get("status_save_error")
    return "\n".join(
        [
            "[task-local-progress]",
            f"summary: {progress.get('summary', '')}",
            f"latest_written_path: {progress.get('latest_written_path', '')}",
            f"headings: {progress.get('headings', [])}",
            f"next_action: {progress.get('next_action', '')}",
            f"latest_tool_progress_ref: {progress.get('latest_tool_progress_ref', '')}",
            f"load_error: {load_error}" if isinstance(load_error, dict) else "",
            f"status_save_error: {status_save_error}" if isinstance(status_save_error, dict) else "",
            "policy: follow next_action; avoid duplicating recorded headings. "
            "If next_action mentions output.json, stop product-body writes and close out with structured refs.",
        ]
    )


def _executed_subagent_orchestration(params: ToolLoopExecuteParams) -> bool:
    return any(str(item or "") in _ORCHESTRATION_TOOLS for item in params.executed_tools or [])


def _pop_pending_deferred_tool_calls(params: ToolLoopExecuteParams) -> list[dict[str, object]]:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return []
    value = state.pop("pending_deferred_tool_calls", [])
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict) and str(item.get("tool") or "").strip()]


def _deferred_drain_prompt(agent: object, params: ToolLoopExecuteParams) -> str:
    try:
        return build_tool_loop_prompt(agent, params)
    except Exception:
        return ""
