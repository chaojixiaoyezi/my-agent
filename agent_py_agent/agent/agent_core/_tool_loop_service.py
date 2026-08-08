

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ..backends import ModelResponse
from ..backends.errors import (
    is_empty_provider_response_error,
    is_incomplete_provider_response_error,
)
from ..concurrency.interrupt import is_interrupted
from ..prompting_parts.builder import ToolSections, project_runtime_workspace_context
from ..settings.runtime_guard_config import runtime_guard_int
from ..subagents.services.session_progress import record_runtime_subagent_tool_progress
from ..tooling.operation_verification import render_current_turn_execution_facts
from ..tooling.registry_workspace import effective_registry_cwd
from ._runtime_params import ToolLoopExecuteParams
from .delivery_contract_prompting import render_delivery_contract_section
from .native_tool_protocol import native_tool_use_active
from .orchestration.shared_context import (
    refresh_parent_shared_context_cache,
    refresh_parent_shared_context_from_tool_record,
)
from .provider_transient_auto_resume import run_with_provider_transient_auto_resume
from .runner.context import current_task_attributes
from .runner.stage_trace import trace_runner_tool_call_started
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
)
from .tool_context.call_reducer import render_tool_payload_for_live_prompt
from .tool_context.reducer import render_tool_result_for_live_prompt
from .tool_context.window import record_native_ir_window, window_tool_context_params
from .tool_guard.call_guardrail import (
    clear_consecutive_failure_segment,
    record_tool_guard_observation,
    tool_guardrail_records,
)
from .tool_guard.call_guardrail_config import (
    hard_failure_halt_enabled as configured_hard_failure_halt_enabled,
)
from .tool_guard.call_guardrail_config import (
    hard_failure_halt_threshold as configured_hard_failure_halt_threshold,
)
from .tool_guard.call_guardrail_config import (
    repeated_failure_halt_threshold as configured_repeated_failure_halt_threshold,
)
from ..contracts.gates.tool_guardrail import (
    consecutive_same_failure_count,
    failure_class_of_result,
)
from .tool_guard.loop_hints import (
    append_tool_failure_channel_hint,
    append_tool_guardrail_action_block_hint,
)
from .tool_ir_compact import (
    compact_native_ir_to_token_budget,
    reclaim_oldest_native_ir_pairs,
)
from .tool_ir_history import record_tool_call_ir, replace_compaction_summary_ir
from .tool_loop.completion import (
    ToolRoundCompletionRequest,
    completion_response_after_tool_round,
    queue_interim_reply_for_active_named_work,
    queue_interim_reply_for_open_subagents,
    queue_interim_reply_for_tool_round_limit,
    queue_reply_for_audit_prepare,
    queue_reply_for_incomplete_final_mutation,
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
from .tool_runtime_ledger import persist_tool_runtime_ledger, write_boundary_with_runtime_ledger

_LOGGER = logging.getLogger(__name__)

# 模型失败后机械重试无上限时的轮数防线。
# 显式配置 max_tool_rounds 或任务属性可覆盖; 显式 0 保留"不限制"逃生。
_DEFAULT_MAX_TOOL_ROUNDS = 60

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
    # LLM: 模型失败后机械重试无上限会把任务拖死(实测"读不存在文件"38+ 轮不收敛)；
    # 参考 长期助手 max_iterations 默认 15 的同一防线, 这里给宽默认 60——复杂多步任务
    # 足够, 死循环 60 轮后走 _final_response_after_tool_limit 诚实交接(未完成+进度持久,
    # 下个 run 续跑), 不会无限烧时间/成本。只有"显式 0"才保留不限制逃生；空值/缺省
    # 一律落到默认防线(与 runtime_guard_config.yaml 的 max_tool_rounds 默认值保持一致)。
    if effective is None:
        return _DEFAULT_MAX_TOOL_ROUNDS
    try:
        effective = int(effective)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_TOOL_ROUNDS
    if effective <= 0:
        return 0
    return effective


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


# LLM: Every text/native tool round must pass through the shared context window before provider
# preflight; do not bypass this entry for Gateway conversations or child agents.
# 函数用途: 组装本轮工具模型输入，并在真正调用模型前用统一 Compact 配置压住可见上下文。
def build_tool_loop_prompt(agent, params: ToolLoopExecuteParams) -> str:
    if params.consume_pending_turn_input:
        inject_pending_turn_input(agent, params)
    window_tool_context_params(agent, params)
    prompt = _render_tool_loop_prompt(agent, params)
    # native 下文本 tool_context 不发往 provider（IR messages 才发）。真正决定整个请求
    # 大小的是 prompt + tools schema + 完整 IR；按这份统一 token 口径整对回收旧往返，
    # 避免大 edit/write 参数被字符近似漏算后触发同 turn 重启。
    _fit_native_ir_to_shared_budget(agent, params, prompt)
    return prompt


def _render_tool_loop_prompt(agent, params: ToolLoopExecuteParams) -> str:
    return agent.prompts.build(
        params.user_prompt,
        params.memories,
        inject=_runtime_injections_with_delivery_contract(params),
        prompt_files=params.prompt_files,
        system_prompt_override=params.system_prompt_override,
        context_scope=params.context_scope,
        workspace_context_override=_runtime_workspace_context(agent, params),
        tools=ToolSections(
            tool_catalog_section=params.tool_catalog_section,
            tool_recommendations_section=params.tool_recommendations_section,
            tool_context=params.tool_context,
            execution_facts_section=(
                render_current_turn_execution_facts(agent, params.archive_tool_calls)
                if params.tool_catalog_section
                else ""
            ),
            # native 下工具往返由原生 messages 携带，prompt 旁路 tool_context 文本折入。
            native_tool_use=native_tool_use_active(params),
        ),
    )


def _runtime_workspace_context(agent: object, params: ToolLoopExecuteParams) -> str | None:
    """Expose the exact live Tool Gateway cwd without creating another cwd store."""

    snapshot = str(params.workspace_context_snapshot or "")
    boundary = write_boundary_with_runtime_ledger(agent, params)
    boundary = boundary if isinstance(boundary, dict) else {}
    task_root = str(boundary.get("task_root") or "").strip()
    if task_root:
        registry = getattr(agent, "tools", None)
        workspace_root = getattr(registry, "workspace_root", None)
        if workspace_root is None:
            workspace_root = getattr(agent, "effective_workspace_root", getattr(agent, "root", "."))
        cwd = effective_registry_cwd(Path(workspace_root), boundary)
        return project_runtime_workspace_context(
            snapshot,
            effective_cwd=str(cwd),
            allowed_write_roots=_string_sequence(boundary.get("allowed_write_roots")),
            task_output_dir=str(boundary.get("task_output_dir") or "").strip(),
            task_work_dir=str(boundary.get("task_work_dir") or "").strip(),
        )
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    return project_runtime_workspace_context(
        snapshot,
        task_workspace_pending=bool(
            str(attrs.get("conversation_thread_id") or "").strip()
            and not isinstance(attrs.get("run_workspace"), dict)
        ),
    ) or None


def _string_sequence(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


# LLM: Native history is reduced only as complete ToolCall/ToolResult pairs, using the exact
# provider-visible estimator and existing RuntimeCompactPolicy; this mutates only current-turn IR.
# 函数用途: 原生工具历史达到统一阈值时，删除最旧完整往返并留下有界恢复提示，避免当前请求被重启。
def _fit_native_ir_to_shared_budget(
    agent: object,
    params: ToolLoopExecuteParams,
    prompt: str,
) -> int:
    """用统一 compact 策略把 native 完整请求收敛到健康的近期尾部。

    会话运行时 依据 provider 可见的完整历史计数，并在 mid-turn compact 后保留一个较小近期
    尾部；长期助手 同样按模型窗口限制单次工具结果。这里复用现有
    ``model_visible_context_tokens``、``RuntimeCompactPolicy.recent_tail_tokens`` 与
    ``compact_semantic_summary``：不新增会话或第二阈值，只在同一 IR 内同时修正完整
    token 计量和被回收旧段的语义续接。
    """
    if not native_tool_use_active(params):
        return 0
    from .model.context_pressure import model_visible_context_tokens
    from .runtime.context_compactor import runtime_compact_policy

    save = params.save
    if save is None:
        save = bool(getattr(getattr(agent, "config", None), "auto_save_memory", True))
    policy = runtime_compact_policy(
        agent,
        save=bool(save),
        context_scope=str(params.context_scope or "default"),
    )
    limit = int(
        policy.trigger_tokens
        if policy.allow_persistent_apply
        else policy.context_window_tokens
    )
    if limit <= 0:
        return 0

    def estimator() -> int:
        return model_visible_context_tokens(agent, params, prompt)

    before_tokens = estimator()
    if before_tokens < limit:
        return 0

    # 先算不可回收的 prompt/schema/当前用户输入基线，再在其上保留统一策略规定的
    # recent tail。这样一次窗口化会获得真实余量，不会每增加一轮就在 90% 线附近抖动。
    base_params = replace(params, tool_ir_history=[])
    base_tokens = model_visible_context_tokens(agent, base_params, prompt)
    target = min(limit - 1, base_tokens + int(policy.recent_tail_tokens or 0))
    semantic_summary = _native_tool_history_summary(agent, params)
    dropped = compact_native_ir_to_token_budget(
        params,
        max_tokens=max(1, target),
        token_estimator=estimator,
    )
    if dropped:
        if semantic_summary:
            replace_compaction_summary_ir(params, semantic_summary)
        dropped = _settle_native_ir_window(
            params=params,
            estimator=estimator,
            target=target,
            dropped=dropped,
            before_tokens=before_tokens,
            trigger_tokens=limit,
            semantic_summary=semantic_summary,
        )
    return dropped


def _settle_native_ir_window(
    *,
    params: ToolLoopExecuteParams,
    estimator: Callable[[], int],
    target: int,
    dropped: int,
    before_tokens: int,
    trigger_tokens: int,
    semantic_summary: str,
) -> int:
    record_native_ir_window(
        params,
        omitted_count=dropped,
        preserved_count=_native_tool_result_count(params),
    )
    # handoff marker 和 summary 本身也要进入同一预算；若重新顶过 target，继续整对回收。
    while estimator() > target:
        additional = compact_native_ir_to_token_budget(
            params,
            max_tokens=max(1, target),
            token_estimator=estimator,
        )
        if additional <= 0:
            break
        dropped += additional
        record_native_ir_window(
            params,
            omitted_count=dropped,
            preserved_count=_native_tool_result_count(params),
        )
    _LOGGER.info(
        "native tool history compacted: before_tokens=%d after_tokens=%d "
        "trigger_tokens=%d dropped_pairs=%d preserved_pairs=%d "
        "semantic_summary=%s summary_chars=%d",
        before_tokens,
        estimator(),
        trigger_tokens,
        dropped,
        _native_tool_result_count(params),
        bool(semantic_summary),
        len(semantic_summary),
    )
    return dropped


# LLM: Count only canonical ToolResult items after pairwise reduction; callers use this for
# human/model handoff metadata, never as an execution or completion authority.
# 函数用途: 统计窗口化后还保留了多少条原生工具结果，用于生成准确的上下文说明。
def _native_tool_result_count(params: ToolLoopExecuteParams) -> int:
    from ..backends.tool_ir import ToolResult

    return sum(
        isinstance(item, ToolResult)
        for item in list(getattr(params, "tool_ir_history", None) or [])
    )


# LLM: native 压缩摘要复用 memory_archive 的唯一通用语义摘要后端；它只生成同一 IR
# 的 replacement item，不创建 task/session compact 或新的事实账本。
# 函数用途: 在旧工具对尚未回收时生成可持续回放的当前 turn 续接摘要。
def _native_tool_history_summary(
    agent: object,
    params: ToolLoopExecuteParams,
) -> str:
    from ..memory_archive.compact_semantic_summary import (
        LiveToolHistorySummaryRequest,
        semantic_summary_config,
        summarize_live_tool_history,
    )

    history = list(getattr(params, "tool_ir_history", None) or [])
    if _native_tool_result_count(params) <= 1:
        return ""
    config = semantic_summary_config(agent)
    if not config.enabled:
        return ""
    return summarize_live_tool_history(
        LiveToolHistorySummaryRequest(
            history=history,
            backend=getattr(agent, "backend", None),
            task_prompt=str(getattr(params, "user_prompt", "") or ""),
            max_output_chars=config.max_input_chars,
        )
    )


def _runtime_injections_with_delivery_contract(params: ToolLoopExecuteParams) -> list:
    if not isinstance(params.delivery_contract, dict):
        return params.runtime_injections
    return [*params.runtime_injections, render_delivery_contract_section(params.delivery_contract)]


def next_tool_loop_model_response(agent, params: ToolLoopExecuteParams, tool_rounds: int):
    _discard_stale_natural_reply_for_pending_turn_input(agent, params)
    model_params = natural_user_reply_model_params(params)
    consumes_task_tool_surface = model_params is params
    prompt = build_tool_loop_prompt(agent, model_params)
    response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=model_params,
            prompt=prompt,
            tool_rounds=tool_rounds,
        )
    )
    result = _retry_after_provider_context_overflow(
        agent,
        model_params,
        tool_rounds,
        first=(prompt, response),
    )
    _consume_ephemeral_loaded_tools(
        model_params,
        result[1],
        tool_surface_was_visible=consumes_task_tool_surface,
    )
    return result


def _consume_ephemeral_loaded_tools(
    params: ToolLoopExecuteParams,
    response: object,
    *,
    tool_surface_was_visible: bool = True,
) -> None:
    """A discovered schema is visible for exactly one successful model call."""

    if not tool_surface_was_visible or not params.loaded_tool_names:
        return
    if str(getattr(response, "runtime_status", "") or "") == "context_overflow":
        return
    params.loaded_tool_names.clear()


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

    if native_tool_use_active(params):
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
    call = request.call
    runtime_request = ToolCallRuntimeRequest(agent, request, call)
    trace_runner_tool_call_started(runtime_request.trace_request)
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
        if action.action == "break":
            if queue_interim_reply_for_open_subagents(
                service._agent,
                params,
                tool_rounds=tool_rounds,
            ):
                continue
            if queue_interim_reply_for_active_named_work(
                service._agent,
                params,
                tool_rounds=tool_rounds,
            ):
                continue
            if queue_reply_for_incomplete_final_mutation(
                service._agent,
                params,
                response=final_response,
                tool_rounds=tool_rounds,
            ):
                continue
            if queue_reply_for_audit_prepare(
                service._agent,
                params,
                response=final_response,
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
        # A stop button produces no assistant message.  The typed runtime
        # fields below are the sole authority for clients and persistence.
        text="",
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

    def _final_response_after_repeated_failure(
        self, params: ToolLoopExecuteParams, tool_rounds: int
    ):
        return _final_response_after_repeated_failure(self._agent, params, tool_rounds)

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
        if queue_interim_reply_for_tool_round_limit(
            service._agent,
            params,
            tool_rounds=tool_rounds,
        ):
            return _PendingDeferredToolDrainResult(tool_rounds)
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
    state = loop_params.live_archive_state
    turn_id = (
        str(state.get("_current_model_turn_id") or "")
        if isinstance(state, dict)
        else ""
    )
    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent,
            loop_params,
            response,
            repair_counters,
            turn_id,
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
        if queue_interim_reply_for_tool_round_limit(
            service._agent,
            request.params,
            tool_rounds=request.tool_rounds,
        ):
            return request.current_prompt, None, request.tool_rounds
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
    # 同类失败强制收口优先于本轮自然结束：即使模型又产出了"继续推进"式
    # 回复，也要按未完成交接收口，避免把原地打转标成完成(真机实证:
    # urllib3 复刻 60+ 次同类 send_message 失败后仍可能产出完成态)。
    if request.params.repeated_failure_halt is not None:
        final_prompt, final_response = service._final_response_after_repeated_failure(
            request.params,
            next_round,
        )
        return final_prompt, final_response, next_round
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
# 函数用途: 轮数到顶时让模型只做总结不再用工具；续跑文案按预算如实切换——
# 还有自动续跑预算说「系统会自动继续」，预算耗尽说「请回复『继续』」（真机铁证
# 2026-08-07 celery 复刻:提示词承诺自动续跑但普通任务不续，模型如实转述了没兑现的承诺）。
def _final_response_after_tool_limit(agent, params: ToolLoopExecuteParams, tool_rounds: int):
    if _ordinary_task_resume_available(agent, params) is False:
        params.tool_context.append(
            "[tool-system]\n"
            "本轮已达到最大工具轮数限制，停止继续调用工具。这是一次未完成的阶段交接，不是任务完成。"
            "请只根据真实工具记录说明已经完成的工作、仍未完成的工作和当前限制；"
            "不要把尚未执行的动作写成正在执行或已经完成。"
            "本任务已自动推进多次仍未完成，本次交接后暂停自动推进；"
            "请如实告诉用户：回复『继续』可让我接着做。"
        )
    else:
        params.tool_context.append(
            "[tool-system]\n"
            "本轮已达到最大工具轮数限制，停止继续调用工具。这是一次未完成的阶段交接，不是任务完成。"
            "请只根据真实工具记录说明已经完成的工作、仍未完成的工作和当前限制；"
            "不要把尚未执行的动作写成正在执行或已经完成。运行时会保留同一任务并按持久进度继续。"
        )
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
    final_response = without_tool_call_after_limit(params, final_response)
    final_response = replace(
        final_response,
        runtime_status="unfinished",
        runtime_reason="TOOL_ROUND_LIMIT_REACHED",
        runtime_source="tool_loop",
    )
    return final_prompt, final_response


def _repeated_failure_halt_threshold(params: ToolLoopExecuteParams) -> int:
    # L2 阶梯:同工具同类失败连续达阈值即收口并自动续跑(不再跟随 guardrail
    # 3N DENY;默认 8,可经 task_attributes/runtime_guard_policy 覆盖)。
    return configured_repeated_failure_halt_threshold(params)


def _ordinary_task_resume_available(agent, params: ToolLoopExecuteParams) -> bool | None:
    """轮限收口提示词按续跑预算切换:还有预算说「会自动继续」,耗尽说「请回复『继续』」。

    三值语义:True=本次收口后还有自动续跑预算;False=预算耗尽,本次收口后不再
    自动续跑;None=查不到(沿用乐观文案,与 /goal 无限续跑语义一致)。判定完全用
    结构化信号(policy metadata 的 resume_used/resume_limit),不做自然语言判断。
    """
    try:
        attrs = getattr(params, "task_attributes", None)
        if isinstance(attrs, dict) and str(attrs.get("thread_goal_id") or "").strip():
            # /goal 任务无预算概念:用户显式目标即无限续跑授权。
            return True
        store = getattr(agent, "conversation_store", None)
        if store is None or not callable(getattr(store, "list_progress_policies", None)):
            return None
        from .runtime.task_identity import progress_ledger_id

        task_id = str(progress_ledger_id(agent, params) or "").strip()
        if not task_id:
            return None
        matching = [
            policy
            for policy in store.list_progress_policies(enabled_only=True)
            if policy.task_id == task_id
            and str((policy.metadata or {}).get("kind") or "") == "ordinary_task_resume"
        ]
        if not matching:
            # 还没有 policy = 本次收口将创建第一次续跑 = 有预算。
            return True
        try:
            used = int((matching[0].metadata or {}).get("resume_used") or 0)
        except (TypeError, ValueError):
            used = 0
        try:
            resume_limit = int((matching[0].metadata or {}).get("resume_limit") or 0)
        except (TypeError, ValueError):
            resume_limit = 0
        if resume_limit <= 0:
            return True
        return used < resume_limit
    except Exception:
        return None


def _mark_repeated_failure_halt(agent, record: ToolCallRecordParams) -> None:
    if record.params.repeated_failure_halt is not None or record.result.ok:
        return
    failure_class = failure_class_of_result(record.result)
    # guardrail 自身拦截(TOOL_GUARDRAIL_*_BLOCKED)不是模型错误:不计入连续段,
    # 也不触发 halt(拦截后模型换策略是正常路径)。
    if failure_class.startswith("code:TOOL_GUARDRAIL"):
        return
    count = consecutive_same_failure_count(
        tool_guardrail_records(agent),
        record.call.tool_name,
        failure_class,
    )
    _soft_hint_after_failures(agent, record, count, failure_class)
    if count < _repeated_failure_halt_threshold(record.params):
        return
    exhausted = _repeated_failure_exhausted(agent, record, count)
    object.__setattr__(
        record.params,
        "repeated_failure_halt",
        (record.call.tool_name, failure_class, count),
    )
    object.__setattr__(record.params, "repeated_failure_halt_exhausted", exhausted)
    if exhausted:
        record.params.tool_context.append(
            "[tool-system]\n"
            f"工具 {record.call.tool_name} 已连续 {count} 次以同一失败类型({failure_class})失败，"
            f"且已连续 {_repeated_failure_streak(agent)} 轮未取得任何成功工具结果"
            f"{'(达到硬门阈值)' if _hard_halt_hit(agent, record, count) else ''}。"
            "判定为无法自行脱困。本轮按未完成状态收口；请基于已有工具结果如实说明"
            "已做与未做的工作，等待用户提供新思路后继续。"
        )
        return
    # 软收口:本轮停止该工具,任务保持未完成并自动续跑;清掉该失败段,
    # 下轮换策略后重新尝试同一工具时从 0 重新累计,不会一碰就再收口。
    clear_consecutive_failure_segment(agent, record.call.tool_name, failure_class)
    record.params.tool_context.append(
        "[tool-system]\n"
        f"工具 {record.call.tool_name} 已连续 {count} 次以同一失败类型({failure_class})失败，"
        "本轮停止调用该工具。任务未完成：请彻底更换策略，从另一个角度继续推进任务；"
        "不要总结或宣告完成。"
    )


# LLM: 四级阶梯的 L3 收益递减判定:连续多轮收口且每轮都没有任何成功工具结果
# → 判定模型自愈不了,本轮起不再自动续跑,等用户介入换思路。
def _repeated_failure_exhausted(
    agent, record: ToolCallRecordParams, count: int
) -> bool:
    if _hard_halt_hit(agent, record, count):
        return True
    streak = _repeated_failure_streak(agent)
    progressed = len(record.params.executed_tools) > 0
    next_streak = 1 if progressed else streak + 1
    _set_repeated_failure_streak(agent, next_streak)
    return next_streak >= _REPEATED_FAILURE_EXHAUST_STREAK


def _hard_halt_hit(agent, record: ToolCallRecordParams, count: int) -> bool:
    # L4 真硬门:默认关闭;开启后同类失败达硬阈值即强制收口等用户。
    if not configured_hard_failure_halt_enabled(record.params):
        return False
    return count >= configured_hard_failure_halt_threshold(record.params)


# L3:连续软收口轮数上限,超过即判定自愈不了。
_REPEATED_FAILURE_EXHAUST_STREAK = 3
_STREAK_ATTR = "_repeated_failure_halt_streak"


def _repeated_failure_streak(agent) -> int:
    value = getattr(agent, _STREAK_ATTR, 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _set_repeated_failure_streak(agent, value: int) -> None:
    object.__setattr__(agent, _STREAK_ATTR, value)


# L1 软提示:同工具同类失败第 2 次即注入按失败类别的恢复指引(只提示一次,
# 不拦调用)。指引按结构化 error_code 前缀分类,不依赖自然语言匹配。
_RECOVERY_HINT_BY_CODE_PREFIX: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("code:TOOL_PARAMETER", "code:TOOL_ARGUMENT", "category:parameter"),
        "工具参数类型或格式不正确。检查参数名称、类型与必填项，参考工具说明重新构造参数；不要原样重试。",
    ),
    (
        ("code:TOOL_PERMISSION", "code:TOOL_AUTHORIZATION", "category:permission"),
        "缺少执行权限或授权。换一个不需要该权限的途径完成任务，或向用户说明需要授权。",
    ),
    (
        ("code:TOOL_EXECUTION_TIMEOUT", "code:TOOL_TIMEOUT", "category:timeout"),
        "执行超时。把操作拆成更小步骤，或换一种实现方式（如直接读写文件代替长时间命令）。",
    ),
    (
        ("code:TOOL_NOT_FOUND", "code:UNKNOWN_TOOL", "code:TOOL_UNKNOWN"),
        "调用了不存在的工具。改用已注册工具，不要继续尝试该工具名。",
    ),
)


def _soft_hint_after_failures(
    agent,
    record: ToolCallRecordParams,
    count: int,
    failure_class: str,
) -> None:
    if count != 2:
        return
    hint = _recovery_hint_for_failure_class(failure_class)
    if not hint:
        return
    record.params.tool_context.append(
        "[tool-system]\n"
        f"工具 {record.call.tool_name} 已连续 {count} 次失败（{failure_class}）。{hint}"
    )


def _recovery_hint_for_failure_class(failure_class: str) -> str:
    for prefixes, hint in _RECOVERY_HINT_BY_CODE_PREFIX:
        if failure_class.startswith(prefixes):
            return hint
    return (
        "该工具连续失败。停止原样重试，换一种做法：换工具、拆步骤、或换实现路径。"
    )


# LLM: 软收口(可自愈)时由模型基于真实工具记录给出 nudge 交接——保持未完成,
# 换策略继续;硬收口(自愈不了/硬门)时给出诚实总结等用户。
def _final_response_after_repeated_failure(
    agent, params: ToolLoopExecuteParams, tool_rounds: int
):
    final_prompt = build_tool_loop_prompt(agent, params)
    final_response = generate_model_response(
        ModelGenerateParams(
            agent=agent,
            params=params,
            prompt=final_prompt,
            tool_rounds=tool_rounds,
        )
    )
    exhausted = _is_exhausted_halt(params)
    final_response = without_tool_call_after_limit(
        params,
        final_response,
        reason="repeated_failure" if not exhausted else "repeated_failure_exhausted",
    )
    final_response = replace(
        final_response,
        runtime_status="unfinished",
        runtime_reason=(
            "REPEATED_TOOL_FAILURE_EXHAUSTED" if exhausted else "REPEATED_TOOL_FAILURE"
        ),
        runtime_source="tool_loop",
    )
    return final_prompt, final_response


def _is_exhausted_halt(params: ToolLoopExecuteParams) -> bool:
    return bool(getattr(params, "repeated_failure_halt_exhausted", False))


def _record_tool_call(agent, record: ToolCallRecordParams) -> None:
    from ..contracts.required_actions import settle_required_action

    payload = record.payload
    settle_required_action(
        record.params.effective_contract_snapshot,
        record.call,
        record.result,
    )
    guardrail_hint = record_tool_guard_observation(
        agent,
        record.params,
        record.call,
        record.result,
    )
    _mark_repeated_failure_halt(agent, record)
    if record.result.ok:
        record.params.executed_tools.append(record.result.tool_name)
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
        f"{render_tool_payload_for_live_prompt(payload)}\n"
        f"[tool-output-record round={record.tool_rounds} index={record.idx}]\n"
        f"{result_rendered}"
    )
    # 灰度双轨：native 下同时把这次「调用+结果」记进结构化 IR 历史（与上面的文本
    # tool_context 共存），供出站翻成原生 messages；text 协议下完全不走这里。
    _record_tool_call_ir_if_native(record)
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
    record: ToolCallRecordParams,
) -> None:
    """Append the exact canonical pair; IDs are never synthesized at record time."""
    if not native_tool_use_active(record.params):
        return
    record_tool_call_ir(
        record.params,
        tool_rounds=record.tool_rounds,
        call=record.call,
        result=record.result,
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


def _pop_pending_deferred_tool_calls(params: ToolLoopExecuteParams) -> list:
    from ..tooling.runtime_contracts import canonical_tool_call_from_persisted_payload

    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return []
    value = state.pop("pending_deferred_tool_calls", [])
    if not isinstance(value, list):
        return []
    calls = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict) or not str(item.get("tool") or "").strip():
            continue
        try:
            calls.append(
                canonical_tool_call_from_persisted_payload(
                    item,
                    runtime_snapshot=params.tool_runtime_snapshot,
                    protocol_snapshot=params.tool_protocol_snapshot,
                    run_id=params.run_id,
                    turn_id=f"{params.run_id}:deferred-resume",
                    attempt_id=str(
                        params.attempt_id or params.request_id or params.run_id or "attempt"
                    ),
                    fallback_call_id=f"deferred-{index}",
                )
            )
        except ValueError as exc:
            params.tool_context.append(
                "[tool-system:deferred-call-rejected]\n"
                f"{type(exc).__name__}: {exc}"
            )
    return calls


def _deferred_drain_prompt(agent: object, params: ToolLoopExecuteParams) -> str:
    try:
        return build_tool_loop_prompt(agent, params)
    except Exception:
        return ""
