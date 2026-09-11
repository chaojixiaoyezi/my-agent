

# LLM: 工具循环用同一 native IR 和 owner/thread Compact 权威；副作用按工具账本结构化结果处理，不能由次数或错误码旁路恢复保护。
# 模块用途: 组装每轮工具请求并协调压缩与提交；不以裁剪改写用户输入、工具账本或任务状态。
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from ..backends import ModelResponse
from ..backends.errors import (
    is_empty_provider_response_error,
)
from ..concurrency.interrupt import is_interrupted
from ..contracts.gates.tool_guardrail import (
    consecutive_same_failure_count,
    failure_class_of_result,
)
from ..conversation.compact_guard import raise_if_compact_interrupted
from ..conversation.compact_progress import (
    COMPACT_AUTHORITY_CONVERSATION,
    COMPACT_AUTHORITY_TURN_LOCAL,
    COMPACT_SOURCE_ACTIVE_TURN,
    COMPACT_SOURCE_TURN_LOCAL,
    CONVERSATION_COMPACT_PROGRESS_SCHEMA,
)
from ..conversation.tool_context_window import record_native_ir_window, window_tool_context_params
from ..prompting_parts.builder import ToolSections, project_runtime_workspace_context
from ..runtime_db.operations import exec_lock_scope
from ..settings.runtime_guard_config import runtime_guard_int
from ..subagents.services.session_progress import record_runtime_subagent_tool_progress
from ..tooling.operation_verification import render_current_turn_execution_facts
from ..tooling.registry_workspace import effective_registry_cwd
from ._runtime_params import ToolLoopExecuteParams
from .delivery_contract_prompting import render_delivery_contract_section
from .native_tool_protocol import native_tool_use_active
from .provider_transient_auto_resume import run_with_provider_transient_auto_resume
from .runner.context import current_task_attributes
from .runner.stage_trace import (
    trace_runner_tool_call_started,
)
from .runtime.conversation_state import (
    conversation_runtime_state_section,
    record_conversation_compact_generation,
)
from .runtime.goal_accounting import account_goal_model_response, begin_goal_model_turn
from .runtime.guidance import (
    acknowledge_injected_turn_input,
    has_pending_turn_input,
    inject_pending_turn_input,
    refresh_runtime_direct_children_snapshot,
    restore_injected_turn_input_for_provider_retry,
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
from .tool_guard.loop_hints import (
    append_tool_failure_channel_hint,
    append_tool_guardrail_action_block_hint,
)
from .tool_ir_compact import (
    compact_native_ir_to_token_budget,
)
from .tool_ir_history import record_tool_call_ir, replace_compaction_summary_ir
from .tool_loop.completion import (
    ToolRoundCompletionRequest,
    completion_response_after_tool_round,
    queue_interim_reply_for_active_named_work,
    queue_interim_reply_for_open_subagents,
    queue_interim_reply_for_tool_round_limit,
    queue_reply_for_audit_prepare,
    task_local_wait_response_for_open_subagents,
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

# 极高级别轮数保护(默认 5000):正常深度任务(整仓换语言复刻)几百轮远够,
# 5000 等效"不限制",但真失控死循环仍有最后硬顶,不会无限烧时间/成本——这是
# 取消 60 轮截停(2026-08-07)后的补位兜底:轮数不再拦可救任务,失控仍有终点。
# 显式配置 max_tool_rounds 或任务属性仍可覆盖(正数=限制,0=不限制)。
_DEFAULT_MAX_TOOL_ROUNDS = 5000

_ORCHESTRATION_TOOLS = {
    "create_subagents",
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


# LLM: This is one immutable candidate for reducing native history; binding and generation are
# point-in-time facts and the plan must be discarded after either commit or rollback.
# 类用途: 固定一次运行中 Compact 的阈值、摘要、线程绑定和工具调用边界，供提交与回滚共用。
@dataclass(frozen=True)
class _NativeCompactPlan:
    policy: object
    binding: object | None
    trigger_tokens: int
    recovery_target_tokens: int
    target_tokens: int
    before_tokens: int
    before_call_ids: tuple[str, ...]
    semantic_summary: str
    progress_generation: int
    progress_operation_id: str
    progress_source_kind: str
    progress_commit_authority: str
    forced: bool


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
    # LLM: 极高级别轮数保护(默认 5000)——正常深度任务几百轮远够,5000 只拦
    # 真失控死循环;普通可恢复工具失败只返给模型修正,不再由宿主结束 turn。
    # 防失控另有 action 级重复调用门、unknown_command_budget 与 compact 防抖。
    # 显式正数仍可限制,显式 0 不限制,任务属性可单任务覆盖。
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


def _pending_turn_input_supersedes_unusable_response(
    agent: object,
    params: ToolLoopExecuteParams,
    exc: Exception,
) -> bool:
    """Keep a 会话运行时 steer in the same turn when the stale call has no usable result."""
    unusable = is_empty_provider_response_error(exc)
    if not unusable or not has_pending_turn_input(agent, params):
        return False
    discard_pending_natural_user_reply(params)
    # Move the durable mailbox item into this turn before retrying. Prompt
    # assembly reserves it; the explicit pre-provider edge records submission,
    # so an ambiguous crash is observable but never replayed into another call.
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


# LLM: Every text/native tool round must pass through the shared context window before provider
# preflight; do not bypass this entry for Gateway conversations or child agents.
# 函数用途: 组装本轮工具模型输入，并在真正调用模型前用统一 Compact 配置压住可见上下文。
def build_tool_loop_prompt(agent, params: ToolLoopExecuteParams) -> str:
    # 子代理状态是当前运行事实，不属于可被 compact 摘要冻结的历史。每次 provider 安全点
    # 原位刷新一个稳定的小尾巴；状态没变时字节不变，保留前缀缓存命中。
    refresh_runtime_direct_children_snapshot(agent, params)
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


# LLM: Workspace Context must mirror the same cwd used by ToolRegistry.  For
# conversations, task_root/output/work are private runtime storage and are
# intentionally omitted so the model cannot mistake them for the project cwd.
# 函数用途: 向模型展示本轮真实工具目录；会话模式只展示用户目录和可见写根。
def _runtime_workspace_context(agent: object, params: ToolLoopExecuteParams) -> str | None:
    """Expose the exact live Tool Gateway cwd without creating another cwd store."""

    snapshot = str(params.workspace_context_snapshot or "")
    boundary = write_boundary_with_runtime_ledger(agent, params)
    boundary = boundary if isinstance(boundary, dict) else {}
    task_root = str(boundary.get("task_root") or "").strip()
    if task_root:
        cwd = _runtime_effective_cwd(agent, boundary)
        conversation_cwd = _conversation_uses_user_cwd(params)
        return project_runtime_workspace_context(
            snapshot,
            effective_cwd=str(cwd),
            allowed_write_roots=(
                _conversation_visible_write_roots(boundary, task_root)
                if conversation_cwd
                else _string_sequence(boundary.get("allowed_write_roots"))
            ),
            task_output_dir=(
                "" if conversation_cwd else str(boundary.get("task_output_dir") or "").strip()
            ),
            task_work_dir=(
                "" if conversation_cwd else str(boundary.get("task_work_dir") or "").strip()
            ),
        )
    execution_cwd = str(boundary.get("execution_cwd") or "").strip()
    if execution_cwd:
        cwd = _runtime_effective_cwd(agent, boundary)
        return project_runtime_workspace_context(
            snapshot,
            effective_cwd=str(cwd),
            allowed_write_roots=_string_sequence(boundary.get("allowed_write_roots")),
        )
    return project_runtime_workspace_context(snapshot) or None


# LLM: Prompt projection and ToolRegistry share this exact cwd selector. The process root is only
# a fallback when the host-authored turn boundary has no execution_cwd.
# 函数用途: 按本轮写边界计算模型和工具共同使用的真实当前目录。
def _runtime_effective_cwd(agent: object, boundary: dict[str, object]) -> Path:
    registry = getattr(agent, "tools", None)
    workspace_root = getattr(registry, "workspace_root", None)
    if workspace_root is None:
        workspace_root = getattr(agent, "effective_workspace_root", getattr(agent, "root", "."))
    return effective_registry_cwd(Path(workspace_root), boundary)


# LLM: cli_run has a transcript for evidence but remains a standalone delivery
# run.  Every other thread-bound run inherits the user/project cwd across
# foreground and background turns.
# 函数用途: 区分会话 cwd 与一次性任务交付目录。
def _conversation_uses_user_cwd(params: object) -> bool:
    source = str(getattr(params, "source", "") or "").strip().lower()
    if source == "cli_run":
        return False
    if source == "gateway":
        return True
    attrs = getattr(params, "task_attributes", None)
    return bool(
        isinstance(attrs, dict)
        and str(attrs.get("conversation_thread_id") or "").strip()
    )


# LLM: Hidden task storage may remain writable for host bookkeeping but should
# not be advertised as a user project root.  Keep external/user roots, including
# an ancestor cwd such as /root, and remove only task_root and its descendants.
# 函数用途: 从会话提示的写入目录中隐藏内部台账目录，保留真正的用户工作区授权。
def _conversation_visible_write_roots(
    boundary: dict[str, object],
    task_root: str,
) -> list[str]:
    try:
        internal = Path(task_root).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return []
    visible: list[str] = []
    for raw in _string_sequence(boundary.get("allowed_write_roots")):
        try:
            candidate = Path(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if candidate == internal or candidate.is_relative_to(internal):
            continue
        text = str(candidate)
        if text not in visible:
            visible.append(text)
    return visible


def _string_sequence(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


# LLM: Native history is reduced only as complete ToolCall/ToolResult pairs. A save-enabled
# authoritative turn must checkpoint and CAS the same ConversationThread before the mutation lives.
# 函数用途: 原生工具历史达到统一阈值时，压缩最旧完整往返并将真实回合提交到唯一 Compact 账本。
def _fit_native_ir_to_shared_budget(
    agent: object,
    params: ToolLoopExecuteParams,
    prompt: object,
    *,
    force: bool = False,
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

    def estimator() -> int:
        return model_visible_context_tokens(agent, params, prompt)

    plan = _prepare_native_compact_plan(
        agent,
        params,
        prompt,
        estimator=estimator,
        force=force,
    )
    if plan is None:
        return 0
    return _apply_native_compact_plan(agent, params, estimator, plan)


# LLM: Policy resolution stays identical for preflight and provider-overflow entrypoints.
# 函数用途: 依据本回合 save/context scope 生成唯一运行中 Compact 策略。
def _native_compact_policy(agent: object, params: ToolLoopExecuteParams) -> object:
    from .runtime.context_compactor import runtime_compact_policy

    save = params.save
    if save is None:
        save = bool(getattr(getattr(agent, "config", None), "auto_save_memory", True))
    return runtime_compact_policy(
        agent,
        save=bool(save),
        context_scope=str(params.context_scope or "default"),
        task_attributes=params.task_attributes,
    )


# LLM: Planning checks the run interrupt before it may call the summary backend; it still cannot
# mutate native IR or advance generation.
# 函数用途: 停止检查通过后算清触发线、健康尾部、线程绑定和完整替代摘要。
def _prepare_native_compact_plan(
    agent: object,
    params: ToolLoopExecuteParams,
    prompt: str,
    *,
    estimator: Callable[[], int],
    force: bool,
) -> _NativeCompactPlan | None:
    from .model.context_pressure import model_visible_context_tokens

    raise_if_compact_interrupted(lambda: _native_compact_interrupted(params))
    policy = _native_compact_policy(agent, params)
    limit = int(
        policy.trigger_tokens
        if policy.allow_persistent_apply
        else policy.context_window_tokens
    )
    before_tokens = estimator()
    before_call_ids = _native_tool_call_ids(params)
    if limit <= 0 or (before_tokens < limit and not force) or len(before_call_ids) <= 1:
        return None
    base_tokens = _native_compact_floor_tokens(agent, params, prompt)
    recent_tail_tokens = max(1, int(policy.recent_tail_tokens or 0))
    recovery_target = (
        max(1, int(policy.recovery_target_tokens or 0))
        if limit == int(policy.trigger_tokens or 0)
        else max(1, limit - recent_tail_tokens)
    )
    # 已完成会话前缀若已经吃掉恢复目标，删当前 turn 的工具往返也无法留出一段完整
    # 近期工作空间。交给 Gateway 的 transcript Compact 替换旧历史，不先烧 live
    # summary 再提交一个下一轮必然重压的薄 generation。
    if base_tokens >= recovery_target:
        return None
    target = min(recovery_target, base_tokens + recent_tail_tokens)
    (
        binding,
        summary,
        progress_generation,
        progress_operation_id,
        progress_source_kind,
        progress_commit_authority,
    ) = _live_compact_binding_and_summary(
        agent,
        params,
        policy,
        provider_prompt=prompt,
        before_tokens=before_tokens,
        trigger_tokens=limit,
        source_messages=len(before_call_ids) * 2,
    )
    return _NativeCompactPlan(
        policy=policy,
        binding=binding,
        trigger_tokens=limit,
        recovery_target_tokens=recovery_target,
        target_tokens=target,
        before_tokens=before_tokens,
        before_call_ids=before_call_ids,
        semantic_summary=summary,
        progress_generation=progress_generation,
        progress_operation_id=progress_operation_id,
        progress_source_kind=progress_source_kind,
        progress_commit_authority=progress_commit_authority,
        forced=force,
    )


# LLM: Eligibility must estimate the same structural deletion later applied to the real IR. An
# empty history would discard UserTurn, each source's newest RuntimeFactsTurn and carried summaries.
# Stale runtime facts in the summarized tool prefix are removable under the shared structural rule,
# unlike those protected items. The probe owns copied
# containers and never records a window, mutates the active turn, or advances Compact generation.
# 函数用途: 在副本上删尽真正可被摘要覆盖的工具轮，算出运行中 Compact 实际能达到的最低上下文水位。
def _native_compact_floor_tokens(
    agent: object,
    params: ToolLoopExecuteParams,
    prompt: object,
) -> int:
    from .model.context_pressure import model_visible_context_tokens

    probe_params = replace(
        params,
        tool_ir_history=list(params.tool_ir_history),
        tool_context=list(params.tool_context),
    )

    def estimator() -> int:
        return model_visible_context_tokens(agent, probe_params, prompt)

    compact_native_ir_to_token_budget(
        probe_params,
        max_tokens=1,
        token_estimator=estimator,
        preserve_newest_pair=False,
        drop_completed_tool_turns=True,
    )
    return estimator()


# LLM: An authoritative turn binds its exact thread before summary generation and rechecks stop
# before classifying an empty result; only real empty summaries consume the shared circuit.
# 函数用途: 找到本代理线程并可中断地生成完整摘要，真实失败时才记录统一熔断事实。
def _live_compact_binding_and_summary(
    agent: object,
    params: ToolLoopExecuteParams,
    policy: object,
    *,
    provider_prompt: str,
    before_tokens: int,
    trigger_tokens: int,
    source_messages: int,
) -> tuple[object | None, str, int, str, str, str]:
    from ..conversation.live_tool_compact import (
        record_live_tool_compact_failure,
        resolve_live_tool_compact_binding,
    )

    binding = resolve_live_tool_compact_binding(
        agent,
        task_attributes=params.task_attributes,
        policy=policy,
    )
    progress_generation = _native_compact_progress_generation(params, binding)
    progress_operation_id = f"live-tool:{uuid.uuid4().hex}"
    if binding is None:
        progress_source_kind = COMPACT_SOURCE_TURN_LOCAL
        progress_commit_authority = COMPACT_AUTHORITY_TURN_LOCAL
    else:
        progress_source_kind = COMPACT_SOURCE_ACTIVE_TURN
        progress_commit_authority = COMPACT_AUTHORITY_CONVERSATION
    progress = {
        "generation": progress_generation,
        "operation_id": progress_operation_id,
        "source_kind": progress_source_kind,
        "commit_authority": progress_commit_authority,
        "before_tokens": before_tokens,
        "trigger_tokens": trigger_tokens,
        "source_messages": source_messages,
    }
    _emit_native_compact_progress(
        params,
        phase="started",
        stage="preparing",
        percent=5,
        **progress,
    )
    _emit_native_compact_progress(
        params,
        phase="progress",
        stage="summarizing",
        percent=20,
        **progress,
    )
    summary = _summarize_live_compact(
        agent,
        params,
        binding,
        progress,
        provider_prompt=provider_prompt,
    )
    raise_if_compact_interrupted(lambda: _native_compact_interrupted(params))
    if binding is None or summary:
        return (
            binding,
            summary,
            progress_generation,
            progress_operation_id,
            progress_source_kind,
            progress_commit_authority,
        )
    from ..conversation.compact_guard import ConversationCompactError

    error = ConversationCompactError(
        "live tool compact backend returned an empty summary",
        code="COMPACT_EMPTY_SUMMARY",
    )
    record_live_tool_compact_failure(binding, error)
    _emit_native_compact_progress(
        params,
        phase="failed",
        stage="failed",
        percent=0,
        error_code=error.code,
        **progress,
    )
    raise error


# LLM: The slow summary call checks both cancellation sources before and after provider I/O.
# Interruption closes the block neutrally; only real failure enters the live Compact circuit.
# 函数用途: 可中断地调用 Compact 摘要模型，并把停止和真实失败投影成不同终态。
def _summarize_live_compact(
    agent: object,
    params: ToolLoopExecuteParams,
    binding: object | None,
    progress: dict[str, object],
    *,
    provider_prompt: str,
) -> str:
    from ..conversation.compact_guard import compact_exception_code
    from ..conversation.live_tool_compact import record_live_tool_compact_failure

    interrupt_check = partial(_native_compact_interrupted, params)
    try:
        raise_if_compact_interrupted(interrupt_check)
        from ._finalization_service import _request_memory_curator

        _request_memory_curator(agent, "pre_compact")
        summary = _native_tool_history_summary(
            agent,
            params,
            previous_summary=(binding.thread.summary if binding is not None else ""),
            provider_prompt=provider_prompt,
        )
        raise_if_compact_interrupted(interrupt_check)
        return summary
    except InterruptedError:
        _emit_native_compact_progress(
            params,
            phase="superseded",
            stage="candidate_discarded",
            percent=0,
            **progress,
        )
        raise
    except Exception as exc:
        try:
            raise_if_compact_interrupted(interrupt_check)
        except InterruptedError:
            _emit_native_compact_progress(
                params,
                phase="superseded",
                stage="candidate_discarded",
                percent=0,
                **progress,
            )
            raise
        record_live_tool_compact_failure(binding, exc)
        _emit_native_compact_progress(
            params,
            phase="failed",
            stage="failed",
            percent=0,
            error_code=compact_exception_code(exc),
            **progress,
        )
        raise


# LLM: Mutation is transactional in memory: interruption or failure restores IR and its readable
# marker. Only real summary/checkpoint/CAS errors share the failure circuit.
# 函数用途: 按计划成对删减工具历史；停止或提交失败都把模型上下文完整恢复到操作前。
def _apply_native_compact_plan(
    agent: object,
    params: ToolLoopExecuteParams,
    estimator: Callable[[], int],
    plan: _NativeCompactPlan,
) -> int:
    from ..conversation.compact_guard import compact_exception_code
    from ..conversation.live_tool_compact import record_live_tool_compact_failure

    original_ir = list(params.tool_ir_history)
    original_tool_context = list(params.tool_context)
    try:
        raise_if_compact_interrupted(lambda: _native_compact_interrupted(params))
        dropped = compact_native_ir_to_token_budget(
            params,
            max_tokens=max(1, plan.target_tokens),
            token_estimator=estimator,
            drop_completed_tool_turns=bool(plan.semantic_summary),
        )
        if not dropped:
            _emit_native_compact_superseded(params, plan)
            return 0
        if plan.semantic_summary:
            replace_compaction_summary_ir(params, plan.semantic_summary)
        dropped, after_tokens = _settle_native_ir_window(
            params=params,
            estimator=estimator,
            target=plan.target_tokens,
            dropped=dropped,
            summary_covers_window=bool(plan.semantic_summary),
        )
        raise_if_compact_interrupted(lambda: _native_compact_interrupted(params))
        _emit_native_compact_progress(
            params,
            phase="progress",
            stage="measuring",
            percent=65,
            **_native_compact_progress_values(plan, after_tokens=after_tokens),
        )
        if after_tokens >= plan.trigger_tokens:
            # 摘要或必须保留的最新往返连真实触发线都未降到：恢复原 IR，交给
            # transcript Compact。恢复目标只是优选目标，低于触发线的有效候选必须提交，
            # 否则慢模型会重复烧摘要却永远不推进 canonical generation。
            _restore_native_compact_candidate(params, original_ir, original_tool_context)
            _emit_native_compact_superseded(params, plan, after_tokens=after_tokens)
            return 0
        return _commit_and_publish_native_compact(
            agent, params, plan, dropped=dropped, after_tokens=after_tokens
        )
    except InterruptedError:
        _restore_native_compact_candidate(params, original_ir, original_tool_context)
        _emit_native_compact_superseded(params, plan)
        raise
    except Exception as exc:
        try:
            raise_if_compact_interrupted(lambda: _native_compact_interrupted(params))
        except InterruptedError:
            _restore_native_compact_candidate(params, original_ir, original_tool_context)
            _emit_native_compact_superseded(params, plan)
            raise
        _restore_native_compact_candidate(params, original_ir, original_tool_context)
        record_live_tool_compact_failure(plan.binding, exc)
        _emit_native_compact_failed(
            params,
            plan,
            error_code=compact_exception_code(exc),
        )
        raise


# LLM: Rollback restores both provider IR and its mechanical guidance projection atomically from
# the caller's snapshots; it never writes ConversationStore or changes the Compact circuit.
# 函数用途: 有效候选未形成或提交失败时恢复压缩前的完整当前回合上下文。
def _restore_native_compact_candidate(
    params: ToolLoopExecuteParams,
    original_ir: list[object],
    original_tool_context: list[str],
) -> None:
    params.tool_ir_history[:] = original_ir
    params.tool_context[:] = original_tool_context


# LLM: Settling is a pure in-memory candidate step. It must not commit a generation or publish an
# event until the caller verifies the full provider-visible request is below the shared trigger.
# 函数用途: 把摘要和保留工具往返重新计量，继续成对裁剪，并返回尚未提交的候选数字。
def _settle_native_ir_window(
    *,
    params: ToolLoopExecuteParams,
    estimator: Callable[[], int],
    target: int,
    dropped: int,
    summary_covers_window: bool,
) -> tuple[int, int]:
    dropped = _reduce_native_ir_to_target(
        params,
        estimator=estimator,
        target=target,
        dropped=dropped,
        summary_covers_window=summary_covers_window,
    )
    return dropped, estimator()


# LLM: A live-tool generation becomes durable only after the settled candidate passes another
# interrupt check and its checkpoint/CAS wins; UI completion is emitted afterwards.
# 函数用途: 停止检查通过后提交健康的运行中 Compact，再把已确认代次和 token 数投给 TUI。
def _commit_and_publish_native_compact(
    agent: object,
    params: ToolLoopExecuteParams,
    plan: _NativeCompactPlan,
    *,
    dropped: int,
    after_tokens: int,
) -> int:
    raise_if_compact_interrupted(lambda: _native_compact_interrupted(params))
    preserved_pairs = _native_tool_result_count(params)
    if plan.binding is not None:
        _emit_native_compact_progress(
            params,
            phase="progress",
            stage="checkpointing",
            percent=82,
            **_native_compact_progress_values(plan, after_tokens=after_tokens),
        )
    canonical_generation = _commit_native_ir_generation(
        agent,
        params,
        plan,
        after_tokens=after_tokens,
    )
    # 历史已按完整工具对改写，上一次 provider 实际用量不再能作为追加基线。
    from .model.context_pressure import invalidate_provider_context_observation

    invalidate_provider_context_observation(params)
    completed_generation = canonical_generation or plan.progress_generation
    _emit_native_compact_progress(
        params,
        phase="completed",
        stage="completed",
        percent=100,
        **{
            **_native_compact_progress_values(plan, after_tokens=after_tokens),
            "generation": completed_generation,
        },
    )
    _publish_native_ir_compaction(
        agent,
        params,
        before_tokens=plan.before_tokens,
        after_tokens=after_tokens,
        trigger_tokens=plan.trigger_tokens,
        dropped_pairs=dropped,
        preserved_pairs=preserved_pairs,
        canonical_generation=canonical_generation,
    )
    _log_native_compact(plan, after_tokens, dropped, preserved_pairs)
    return dropped


# LLM: Logging receives only numeric counters and summary length; summary content never enters logs.
# 函数用途: 记录一次已完成工具历史 Compact 的计量结果，便于性能和成本排查。
def _log_native_compact(
    plan: _NativeCompactPlan,
    after_tokens: int,
    dropped: int,
    preserved_pairs: int,
) -> None:
    _LOGGER.info(
        "native tool history compacted: before_tokens=%d after_tokens=%d "
        "trigger_tokens=%d target_tokens=%d recovery_target_tokens=%d "
        "dropped_pairs=%d preserved_pairs=%d "
        "semantic_summary=%s summary_chars=%d",
        plan.before_tokens,
        after_tokens,
        plan.trigger_tokens,
        plan.target_tokens,
        plan.recovery_target_tokens,
        dropped,
        preserved_pairs,
        bool(plan.semantic_summary),
        len(plan.semantic_summary),
    )


# LLM: Re-estimation includes the replacement summary and marker. Once that complete summary is
# installed, even the newest pair may be released; without a summary ordinary windowing must keep it.
# 函数用途: 把摘要计入预算；完整摘要已经替代旧历史时，必要时连最后一对巨型回执也成对回收。
def _reduce_native_ir_to_target(
    params: ToolLoopExecuteParams,
    *,
    estimator: Callable[[], int],
    target: int,
    dropped: int,
    summary_covers_window: bool,
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
            preserve_newest_pair=not summary_covers_window,
            drop_completed_tool_turns=summary_covers_window,
        )
        if additional <= 0:
            break
        dropped += additional
        record_native_ir_window(
            params,
            omitted_count=dropped,
            preserved_count=_native_tool_result_count(params),
        )
    return dropped


# LLM: This is the only bridge from a settled native window to the canonical thread commit and it
# forwards the run-owned interrupt check across checkpoint/CAS.
# 函数用途: 计算精确调用边界并可中断地把稳定窗口提交为下一代 Compact。
def _commit_native_ir_generation(
    agent: object,
    params: ToolLoopExecuteParams,
    plan: _NativeCompactPlan,
    *,
    after_tokens: int,
) -> int:
    if plan.binding is None:
        return 0
    from ..conversation.live_tool_compact import (
        LiveToolCompactCommitRequest,
        commit_live_tool_compact,
    )

    retained_call_ids = _native_tool_call_ids(params)
    retained_call_id_set = set(retained_call_ids)
    source_call_ids = tuple(
        call_id
        for call_id in plan.before_call_ids
        if call_id not in retained_call_id_set
    )
    updated_thread = commit_live_tool_compact(
        agent,
        plan.binding,
        LiveToolCompactCommitRequest(
            summary=plan.semantic_summary,
            source_tool_call_ids=source_call_ids,
            retained_tool_call_ids=retained_call_ids,
            projected_tokens_before=plan.before_tokens,
            projected_tokens_after=after_tokens,
            policy=plan.policy,
            request_id=params.request_id,
            attempt_id=params.attempt_id,
            forced=plan.forced,
            interrupt_check=lambda: _native_compact_interrupted(params),
            after_checkpoint=lambda: _emit_native_compact_progress(
                params,
                phase="progress",
                stage="committing",
                percent=92,
                **_native_compact_progress_values(plan, after_tokens=after_tokens),
            ),
        ),
    )
    return max(0, int(updated_thread.compact_generation or 0))


# LLM: Native Compact observes both the runner thread interrupt and the run-owned cancellation
# token. The callback is intentionally side-effect free and is interpreted fail-closed by the
# shared Compact guard.
# 函数用途: 汇总当前工具循环的两种结构化停止信号，供摘要、内存候选和 checkpoint/CAS 共用。
def _native_compact_interrupted(params: ToolLoopExecuteParams) -> bool:
    token = getattr(params, "cancellation_token", None)
    return is_interrupted() or bool(token is not None and token.cancelled)


# LLM: The display generation must be chosen before a potentially slow summary call and must
# match the canonical CAS generation when a thread binding exists. Ephemeral no-save reductions
# use only the current turn-local counter and never mutate ConversationStore.
# 函数用途: 为运行中 Compact 的开始、进度和结束事件预先生成同一个稳定块编号。
def _native_compact_progress_generation(
    params: ToolLoopExecuteParams,
    binding: object | None,
) -> int:
    if binding is not None:
        return max(0, int(getattr(binding.thread, "compact_generation", 0) or 0)) + 1
    state = params.live_archive_state
    if isinstance(state, dict):
        return max(0, int(state.get("_native_ir_compact_generation") or 0)) + 1
    return 1


# LLM: All stages for one native Compact reuse these exact counters and block generation; callers
# may override only generation after a successful canonical CAS confirms the same next value.
# 函数用途: 生成运行中 Compact 每个进度事件共用的公开数字，避免阶段间字段漂移。
def _native_compact_progress_values(
    plan: _NativeCompactPlan,
    *,
    after_tokens: int = 0,
) -> dict[str, object]:
    return {
        "generation": plan.progress_generation,
        "operation_id": plan.progress_operation_id,
        "source_kind": plan.progress_source_kind,
        "commit_authority": plan.progress_commit_authority,
        "before_tokens": plan.before_tokens,
        "after_tokens": max(0, int(after_tokens or 0)),
        "trigger_tokens": plan.trigger_tokens,
        "source_messages": len(plan.before_call_ids) * 2,
    }


# LLM: A failed candidate terminates its existing progress block with the same identity and typed
# error code. This is display-only and must run after the original IR has been restored.
# 函数用途: 统一结束失败的运行中 Compact 进度条，并显示结构化原因而不留下永久动画。
def _emit_native_compact_failed(
    params: ToolLoopExecuteParams,
    plan: _NativeCompactPlan,
    *,
    after_tokens: int = 0,
    error_code: str = "",
) -> bool:
    return _emit_native_compact_progress(
        params,
        phase="failed",
        stage="failed",
        percent=0,
        error_code=error_code,
        **_native_compact_progress_values(plan, after_tokens=after_tokens),
    )


# LLM: An ineffective in-memory candidate is a normal no-commit outcome, not a Compact failure.
# Close its display operation without advancing generation or touching the failure circuit so a
# later transcript attempt can own the same generation.
# 函数用途: 工具历史候选没有腾出足够空间时静默收起进度条，继续原上下文或交给完整会话压缩。
def _emit_native_compact_superseded(
    params: ToolLoopExecuteParams,
    plan: _NativeCompactPlan,
    *,
    after_tokens: int = 0,
) -> bool:
    return _emit_native_compact_progress(
        params,
        phase="superseded",
        stage="candidate_discarded",
        percent=0,
        **_native_compact_progress_values(plan, after_tokens=after_tokens),
    )


# LLM: Live-tool and transcript Compact share one content-free progress schema. Milestones and a
# typed failure code come only from real pipeline facts; projection cannot alter compact state.
# 函数用途: 把运行中工具历史 Compact 的真实阶段及失败码送进主代理或子代理 TUI 进度条。
def _emit_native_compact_progress(
    params: ToolLoopExecuteParams,
    *,
    phase: str,
    stage: str,
    percent: int,
    generation: int,
    operation_id: str,
    source_kind: str,
    commit_authority: str,
    before_tokens: int,
    trigger_tokens: int,
    source_messages: int,
    after_tokens: int = 0,
    error_code: str = "",
) -> bool:
    sink = params.effective_on_chunk
    writer = getattr(sink, "write_conversation_compact_progress", None)
    if not callable(writer):
        return False
    payload = {
        "schema": CONVERSATION_COMPACT_PROGRESS_SCHEMA,
        "phase": str(phase),
        "stage": str(stage),
        "percent": min(100, max(0, int(percent or 0))),
        "generation": max(1, int(generation or 1)),
        "operation_id": str(operation_id or "").strip(),
        "source_kind": str(source_kind or "").strip(),
        "commit_authority": str(commit_authority or "").strip(),
        "before_tokens": max(0, int(before_tokens or 0)),
        "after_tokens": max(0, int(after_tokens or 0)),
        "trigger_tokens": max(0, int(trigger_tokens or 0)),
        "source_messages": max(0, int(source_messages or 0)),
        "error_code": str(error_code or "").strip(),
    }
    try:
        return writer(payload) is not False
    except Exception:
        _LOGGER.debug("native compact progress projection failed", exc_info=True)
        return False


# LLM: The event projects a committed ConversationThread generation when available; auxiliary
# no-save turns use only an ephemeral per-turn generation and never mutate task state.
# 函数用途: 把真实压缩代次和 token 前后值投给客户端；辅助回合只展示临时次数。
def _publish_native_ir_compaction(
    _agent: object,
    params: ToolLoopExecuteParams,
    *,
    before_tokens: int,
    after_tokens: int,
    trigger_tokens: int,
    dropped_pairs: int,
    preserved_pairs: int,
    canonical_generation: int = 0,
) -> bool:
    state = params.live_archive_state
    generation = max(0, int(canonical_generation or 0))
    if generation <= 0 and isinstance(state, dict):
        generation = max(0, int(state.get("_native_ir_compact_generation") or 0)) + 1
        state["_native_ir_compact_generation"] = generation
    elif generation <= 0:
        generation = 1
    record_conversation_compact_generation(
        params,
        generation,
        canonical=canonical_generation > 0,
    )
    payload = {
        "schema": "model_visible_context_compaction.v1",
        "generation": generation,
        "before_tokens": max(0, int(before_tokens or 0)),
        "after_tokens": max(0, int(after_tokens or 0)),
        "trigger_tokens": max(0, int(trigger_tokens or 0)),
        "dropped_pairs": max(0, int(dropped_pairs or 0)),
        "preserved_pairs": max(0, int(preserved_pairs or 0)),
    }
    sink = params.effective_on_chunk
    writer = getattr(sink, "write_context_compaction", None)
    if not callable(writer):
        return False
    try:
        return writer(payload) is not False
    except Exception:
        _LOGGER.debug("context compaction projection failed", exc_info=True)
        return False


# LLM: Count only canonical ToolResult items after pairwise reduction; callers use this for
# human/model handoff metadata, never as an execution or completion authority.
# 函数用途: 统计窗口化后还保留了多少条原生工具结果，用于生成准确的上下文说明。
def _native_tool_result_count(params: ToolLoopExecuteParams) -> int:
    from ..backends.tool_ir import ToolResult

    return sum(
        isinstance(item, ToolResult)
        for item in list(getattr(params, "tool_ir_history", None) or [])
    )


# LLM: Exact ordered call ids are structural compact boundaries and must come from ToolResult IR.
# 函数用途: 按真实执行顺序列出当前原生工具往返编号，供 checkpoint 精确记录移除与保留范围。
def _native_tool_call_ids(params: ToolLoopExecuteParams) -> tuple[str, ...]:
    from ..backends.tool_ir import ToolResult

    return tuple(
        item.call_id
        for item in list(getattr(params, "tool_ir_history", None) or [])
        if isinstance(item, ToolResult) and str(item.call_id or "").strip()
    )


# LLM: native compact passes the full typed IR to the memory-archive summarizer; that boundary
# removes only duplicate task/thread projections and must retain an independent carried handoff.
# 函数用途: 在旧工具对尚未回收时生成可持续回放的当前 turn 续接摘要，并保留真实交接上下文。
def _native_tool_history_summary(
    agent: object,
    params: ToolLoopExecuteParams,
    *,
    previous_summary: str = "",
    provider_prompt: str = "",
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
    from ..model_guidance import provider_system_instruction
    from .native_tool_protocol import resolve_native_tools

    tools = resolve_native_tools(agent, params)
    return summarize_live_tool_history(
        LiveToolHistorySummaryRequest(
            history=history,
            backend=getattr(agent, "backend", None),
            agent=agent,
            request_id=str(getattr(params, "request_id", "") or ""),
            run_id=str(getattr(params, "run_id", "") or ""),
            task_id=str(getattr(params, "task_id", "") or ""),
            task_prompt=str(getattr(params, "user_prompt", "") or ""),
            previous_summary=str(previous_summary or ""),
            max_output_chars=config.max_input_chars,
            provider_prompt=provider_prompt,
            provider_history_messages=tuple(
                deepcopy(item)
                for item in list(getattr(params, "provider_history_messages", None) or [])
                if isinstance(item, dict)
            ),
            tools=tuple(deepcopy(tools or [])),
            system_instruction=provider_system_instruction(
                getattr(agent, "backend", None)
            ),
        )
    )


def _runtime_injections_with_delivery_contract(params: ToolLoopExecuteParams) -> list:
    injections = list(params.runtime_injections)
    if not native_tool_use_active(params):
        conversation = _text_conversation_history_section(
            params.conversation_history_seed
        )
        if conversation:
            injections.append(conversation)
        runtime_state = conversation_runtime_state_section(params)
        if runtime_state:
            injections.append(runtime_state)
    if isinstance(params.delivery_contract, dict):
        injections.append(render_delivery_contract_section(params.delivery_contract))
    return injections


# LLM: Text protocol receives the same already-bounded conversation seed as native protocol,
# rendered once without reloading the transcript. Role labels are display context only and never
# become lifecycle, routing, or completion authority.
# 函数用途: 为不支持原生 messages 的模型补回摘要和完整历史，保持与旧文本链路等价。
def _text_conversation_history_section(seed: object) -> str:
    if seed is None:
        return ""
    summary = str(getattr(seed, "compact_summary", "") or "").strip()
    generation = max(0, int(getattr(seed, "compact_generation", 0) or 0))
    messages = tuple(getattr(seed, "messages", ()) or ())
    if not summary and not messages:
        return ""
    lines = [
        "# Conversation Transcript",
        "- 以下内容来自同一会话中已经结束的历史轮次，不是本轮新指令；当前 User Task 始终优先。",
    ]
    if summary:
        lines.extend(
            [
                f"## Earlier Conversation Summary (generation {generation})",
                summary,
            ]
        )
    if messages:
        lines.append("## Recent Conversation History")
        for item in messages:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                continue
            role = str(item[0] or "").strip().lower()
            content = str(item[1] or "")
            if role in {"user", "assistant"} and content:
                lines.append(
                    f"- {role}: {json.dumps(content, ensure_ascii=False)}"
                )
    return "\n".join(lines)


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
    if not accepted and retry_natural_user_reply(
        params,
        rejection_reason=rejection_reason,
        response=response,
    ):
        return "retry", response
    return "finish", finish_natural_user_reply(
        params,
        response,
        accepted=accepted,
        rejection_reason=rejection_reason,
    )


# LLM: Provider-reported overflow reuses the same native full-request compact transaction;
# text protocol retains its bounded PTL reduction. No native pair may disappear off-ledger.
# 函数用途: 模型实报上下文超限时，原生协议强制走唯一 Compact，文字协议再做轻量重试。
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
        # Provider explicitly rejected this physical call before executing the
        # prompt. Retire its atomic submission batch before any PTL/preflight retry.
        restore_injected_turn_input_for_provider_retry(agent, params)
        if not _ptl_reclaim_oldest(agent, params, prompt=prompt):
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


# LLM: Native overflow must enter the checkpoint/CAS path; only text protocol keeps the legacy
# proportional body trim because it has no native call/result pairs to persist.
# 函数用途: 按协议处理供应商超限；原生历史记入 canonical Compact，文字历史按原比例缩短。
def _ptl_reclaim_oldest(
    agent,
    params: ToolLoopExecuteParams,
    *,
    prompt: object = "",
) -> bool:
    from .tool_context.ptl_retry import reclaim_oldest_tool_results_for_ptl

    if native_tool_use_active(params):
        return _fit_native_ir_to_shared_budget(
            agent,
            params,
            prompt or "",
            force=True,
        ) > 0
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


def _renew_exec_lock_if_held(agent, params: ToolLoopExecuteParams) -> None:
    """R1-03 工具循环每轮续租执行权锁（lease 60s，fail-silent）。

    锁语义：create_attempt 已原子取得 scope=attempt-exec:{agent_run_id}；
    工具轮跨时可能超过 lease，必须续租，否则发现层/另一进程会误判
    worker 死亡而接管（kill-9 接管至多一次的代价前提是活 worker 一直在续）。
    同进程内换代（compact/续跑轮）= 同一 worker 续跑，CAS 自然命中；
    已被接管（holder/generation 失配）→ renew CAS 拒，静默跳过——
    旧 worker 的 settle 自会被 stale_attempt 闸拦住，不靠 renew 兜。
    """
    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    run_id = str(getattr(params, "run_id", "") or "")
    attempt_id = str(getattr(params, "attempt_id", "") or "")
    if repo is None or not run_id or not attempt_id:
        return
    try:
        row = repo.agent_run_for_run_id(run_id)
        if row is None:
            return
        scope = exec_lock_scope(str(row["agent_run_id"]))
        lock = repo.lock_for_scope(scope)
        if lock is None:
            return  # 无锁（LOCAL_UNMANAGED/未建执行权）→ 无需续租
        repo.renew_lock(
            canonical_scope=scope,
            holder_instance=repo.instance_id,
            attempt_id=attempt_id,
            attempt_generation=int(lock["attempt_generation"]),
        )
    except Exception:  # noqa: BLE001 续租失败绝不反噬执行路径
        pass


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


# LLM: 工作片只按结构化响应/中断/预算推进，不能从有无正文猜停工或改写用户请求；重复观察走工具结果账。
# 函数用途: 执行主子共用的模型工具循环，保留连续工具工作；等待直属孩子时安全让出而不假报完成。
def _execute_tool_loop_service(service: ToolLoopService, params: ToolLoopExecuteParams):
    final_prompt, final_response = "", None
    tool_rounds = params.tool_rounds
    repair_counters, provider_response_repairs = ToolLoopRepairCounters(), 0

    while True:
        if is_interrupted():
            final_response = _interrupted_conversation_response(service._agent)
            break
        # R1-03：每轮续租执行权锁（fail-silent），活 worker 持续占有防误接管。
        _renew_exec_lock_if_held(service._agent, params)
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
            if wait_response := task_local_wait_response_for_open_subagents(
                service._agent,
                params,
                response=final_response,
            ):
                final_response = wait_response
                break
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
            if queue_reply_for_audit_prepare(
                service._agent,
                params,
                response=final_response,
                tool_rounds=tool_rounds,
            ):
                continue
            # 会话运行时 式自然收口：模型看过本轮真实工具结果后给出 plain final，
            # 当前 turn 就结束。operation/task_progress 继续作为审计和工作记忆，
            # 不能据此覆盖正文、强制返工或另起后台续跑。
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

    def _final_response_after_unknown_outcome_halt(
        self, params: ToolLoopExecuteParams, tool_rounds: int
    ):
        return _final_response_after_unknown_outcome_halt(self._agent, params, tool_rounds)

    def _final_response_after_no_action_gate(
        self, params: ToolLoopExecuteParams, tool_rounds: int
    ):
        return _final_response_after_no_action_gate(self._agent, params, tool_rounds)

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
            retry_guard=lambda: not _active_turn_input_delivery_is_ambiguous(
                loop_params
            ),
        )
        account_goal_model_response(agent, loop_params, response)
        # Provider submission is already durable and therefore never blindly
        # replayed after an ambiguous transport failure. Successful response is
        # the only edge that advances the exact batch to consumed.
        if _model_response_deferred_prompt_consumption(response):
            if str(getattr(response, "runtime_source", "") or "") == "provider_error":
                restore_injected_turn_input_for_provider_retry(agent, loop_params)
        else:
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


# LLM: A transient exception after active-turn input reached provider admission is delivery
# unknown. The model-turn auto-resumer must not rebuild the prompt under a new provider call id.
# 函数用途: 判断本轮是否携带不能安全自动重发的用户补充消息。
def _active_turn_input_delivery_is_ambiguous(loop_params: object) -> bool:
    state = getattr(loop_params, "live_archive_state", None)
    if not isinstance(state, dict):
        return False
    pending = state.get("_guidance_ack_ids")
    return bool(
        str(state.get("_guidance_submission_id") or "").strip()
        or (isinstance(pending, set) and pending)
    )


# LLM: Context-pressure responses are lifecycle signals, not provider acceptance of the prompt.
# Preflight never crossed I/O; provider_error is an explicit pre-execution rejection safe to retry.
# 函数用途: 判断本次结构化响应是否要求先压缩并保留待确认输入，而不是确认已消费。
def _model_response_deferred_prompt_consumption(response: object) -> bool:
    return str(getattr(response, "runtime_status", "") or "") == "context_overflow"


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


# LLM: 执行一批工具后只尊重 typed interrupt、预算和显式安全/硬门；普通可恢复
# 工具失败必须留在当前模型循环内返工，不能由宿主按次数替模型结束 turn。
# 函数用途: 推进一个工具轮，并在明确的中断或硬边界命中时返回收口响应。
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
    # 这里只处理部署者显式开启的 hard_failure_halt。默认同类失败只会把
    # 结构化错误和换路提示交还模型，不会设置 repeated_failure_halt。
    if request.params.repeated_failure_halt is not None:
        final_prompt, final_response = service._final_response_after_repeated_failure(
            request.params,
            next_round,
        )
        return final_prompt, final_response, next_round
    # unknown 副作用收口(T-USER-001):副作用结果不确定(单次即收口,对齐错误合同
    # retryable=False)后不再给模型工具权,按未完成收口并如实汇报
    # (与 repeated_failure_halt 正交,不自动续跑)。
    if getattr(request.params, "unknown_outcome_halt", None) is not None:
        final_prompt, final_response = service._final_response_after_unknown_outcome_halt(
            request.params,
            next_round,
        )
        return final_prompt, final_response, next_round
    # no-action 闸收口(复核 seq 339):informational 轮模型反复抗拦截仍提调用,
    # 连续达限后不再给工具权,收口轮等用户明确指示(与 unknown 收口正交)。
    if getattr(request.params, "no_action_gate_halt", False):
        final_prompt, final_response = service._final_response_after_no_action_gate(
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


# LLM: 工具轮数耗尽后只允许模型基于真实已执行结果收口；若本轮创建了 child，
# wait for the exact lifecycle wake. Only an explicit active Goal promises a
# future host-driven turn; ordinary chat must honestly stop for user input.
# 函数用途: 工具轮数到顶后生成诚实阶段回复，并区分等待子代理、Goal 续跑和普通暂停。
def _final_response_after_tool_limit(agent, params: ToolLoopExecuteParams, tool_rounds: int):
    waiting_for_children = _executed_subagent_orchestration(params)
    if waiting_for_children:
        params.tool_context.append(
            "[tool-system]\n"
            "本轮已达到最大工具轮数限制；请根据真实工具记录简短说明当前阶段，"
            "然后结束本回合等待子代理生命周期事件。不要用 shell sleep 或查询工具轮询；"
            "子代理终态会精确唤醒同一任务，系统不替主代理生成最终结论。"
        )
    elif _active_goal_continuation_available(agent, params):
        params.tool_context.append(
            "[tool-system]\n"
            "本轮已达到最大工具轮数限制，停止继续调用工具。这是一次未完成的阶段交接，不是任务完成。"
            "请只根据真实工具记录说明已经完成的工作、仍未完成的工作和当前限制；"
            "不要把尚未执行的动作写成正在执行或已经完成。"
            "当前显式 Goal 会保留同一任务并按其持久预算继续。"
        )
    else:
        params.tool_context.append(
            "[tool-system]\n"
            "本轮已达到最大工具轮数限制，停止继续调用工具。这是一次未完成的阶段交接，不是任务完成。"
            "请只根据真实工具记录说明已经完成的工作、仍未完成的工作和当前限制；"
            "不要把尚未执行的动作写成正在执行或已经完成。"
            "本次交接后暂停自动推进；"
            "请如实告诉用户：回复『继续』可让我接着做。"
        )
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
    # L2 阶梯:同工具同类失败连续达阈值即诚实收口(不再跟随 guardrail
    # 3N DENY;默认 8,可经 task_attributes/runtime_guard_policy 覆盖)。
    return configured_repeated_failure_halt_threshold(params)


# LLM: Tool-limit wording may promise another host-driven turn only when the
# exact thread/task owns an active Goal. Never infer this from ordinary progress
# policies or model prose; those legacy policies are retired by the scheduler.
# 函数用途: 判断当前轮是否属于显式活跃 Goal，供轮限回复选择真实承诺文案。
def _active_goal_continuation_available(agent, params: ToolLoopExecuteParams) -> bool:
    try:
        attrs = getattr(params, "task_attributes", None)
        attrs = attrs if isinstance(attrs, dict) else {}
        if str(attrs.get("thread_goal_id") or "").strip():
            return True
        store = getattr(agent, "conversation_store", None)
        task_id = str(getattr(params, "task_id", "") or "").strip()
        thread_id = str(attrs.get("conversation_thread_id") or "").strip()
        if store is None or not task_id or not thread_id:
            return False
        goal = store.load_goal(thread_id, task_id=task_id)
        return bool(
            goal is not None
            and str(getattr(goal, "status", "") or "").strip().lower() == "active"
        )
    except Exception:
        return False


# LLM: 同类失败计数默认只生成模型返工提示；只有显式 hard gate 可写 halt，且
# 同批后到的同工具成功必须撤销早到的 halt。修改时同步检查 action guardrail。
# 函数用途: 记录连续工具失败并告诉模型换办法，不把普通失败次数当任务终态。
def _mark_repeated_failure_halt(agent, record: ToolCallRecordParams) -> None:
    """把连续失败变成模型可见返工提示；只有显式硬门才结束 turn。

    会话运行时 会把普通工具错误作为工具结果交还模型，而不是按失败次数替模型终止
    当前工作。本项目保留同类失败统计用于提示和显式 hard gate，但默认路径只
    清理当前失败段并要求换策略。若同一批稍后的同工具调用已经成功，成功事实
    会撤销这个批次里更早写下的 halt，避免并行完成顺序制造假失败。
    """
    if record.result.ok:
        _clear_active_repeated_failure_halt(record.params, record.call.tool_name)
        return
    if record.params.repeated_failure_halt is not None:
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
    if _hard_halt_hit(record, count):
        object.__setattr__(
            record.params,
            "repeated_failure_halt",
            (record.call.tool_name, failure_class, count),
        )
        object.__setattr__(record.params, "repeated_failure_halt_exhausted", True)
        record.params.tool_context.append(
            "[tool-system]\n"
            f"工具 {record.call.tool_name} 已连续 {count} 次以同一失败类型({failure_class})失败，"
            "并达到部署者显式启用的硬门阈值。本轮按未完成状态收口；请基于"
            "已有工具结果如实说明已做与未做的工作，等待用户提供新思路后继续。"
        )
        return
    # 默认路径只返工、不收口：清掉本段后继续同一工具循环，让模型读取真实错误
    # 并换参数/工具/步骤。精确同参的机械重试仍由 action 级 guardrail 拦截，
    # 极端失控仍受 max_tool_rounds 最后硬顶保护。
    clear_consecutive_failure_segment(agent, record.call.tool_name, failure_class)
    record.params.tool_context.append(
        "[tool-system]\n"
        f"工具 {record.call.tool_name} 已连续 {count} 次以同一失败类型({failure_class})失败，"
        "这些失败结果已经返回给你，系统不会因此结束当前任务。请停止原样重试，"
        "改参数、换工具、拆小步骤或等待冲突资源释放后继续推进；不要总结或宣告完成。"
    )


# LLM: 成功 ToolResult 是更晚的结构化事实，只能清同工具 active halt，不能误清
# 其它工具或 unknown/safety halt；此函数只改当前回合易失参数。
# 函数用途: 同一批后面已经成功时，撤销该工具前面失败留下的过期硬闸。
def _clear_active_repeated_failure_halt(params: object, tool_name: str) -> None:
    """同一批后到的成功事实撤销该工具先前写下的失败 halt。"""
    halt = getattr(params, "repeated_failure_halt", None)
    if not isinstance(halt, tuple) or not halt or str(halt[0]) != tool_name:
        return
    object.__setattr__(params, "repeated_failure_halt", None)
    object.__setattr__(params, "repeated_failure_halt_exhausted", False)


# LLM: 与持久 tool_operations 的 UNKNOWN 恢复规则一致，只读取 effect_outcome；
# 不能凭调用次数或 TOOL_TIMEOUT 声称副作用已确定。已证实退出的超时由 shell 返回 failed。
# 函数用途: 首次遇到真实未知执行结果时记录收口状态并追加一次说明，防止运行中放行、重启却失败。
def _mark_unknown_outcome_halt(agent, record: ToolCallRecordParams) -> None:
    if record.params.repeated_failure_halt is not None:
        return
    if getattr(record.params, "unknown_outcome_halt", None) is not None:
        return
    effect = str(getattr(record.result, "effect_outcome", "") or "").strip().lower()
    if effect != "unknown":
        return
    object.__setattr__(
        record.params,
        "unknown_outcome_halt",
        (
            record.call.tool_name,
            str(
                getattr(record.result, "reported_error_code", "")
                or getattr(record.result, "error_code", "")
                or ""
            ),
            effect,
            bool(getattr(record.result, "handler_executed", False)),
        ),
    )
    record.params.tool_context.append(
        "[tool-system]\n"
        f"工具 {record.call.tool_name} 出现副作用结果不确定"
        "(TOOL_OPERATION_OUTCOME_UNKNOWN)，系统无法确认操作是否生效。"
        "按错误合同该结果不可重试、需人工核对。本轮停止发起任何新的工具调用，"
        "按未完成状态如实汇报：只说明已经执行的操作与不确定的事项，"
        "不要继续尝试、重做或验证。"
    )


# no-action 结构化闸(复核 seq 339 第 2 点):评估判 informational(requires_action=False)
# 时模型仍提出 ToolCall,不能直接进 handler——由执行层拦截(TOOL_ACTION_NOT_REQUIRED,
# handler 不执行),有界拦截(连续 2 轮)后走收口轮等用户明确指示。
# 判定只用结构化信号(assessment 状态 + 调用出现与否 + 轮数),不解析模型话术。
_NO_ACTION_GATE_STREAK_ATTR = "_no_action_gate_streak"
_NO_ACTION_GATE_HALT_LIMIT = 2


# LLM: repeated failure 硬收口不是默认主链；必须由结构化配置显式启用并达到阈值。
# 函数用途: 判断部署者是否明确要求把连续失败升级成人工介入硬门。
def _hard_halt_hit(record: ToolCallRecordParams, count: int) -> bool:
    # L4 真硬门:默认关闭;开启后同类失败达硬阈值即强制收口等用户。
    if not configured_hard_failure_halt_enabled(record.params):
        return False
    return count >= configured_hard_failure_halt_threshold(record.params)


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


# LLM: 只有部署者显式开启的 repeated failure 硬门会进入这里；普通可恢复
# 失败已在当前工具循环内返给模型修正，不能再借此入口替模型结束 turn。
# 函数用途: 显式硬门命中后让模型基于真实工具记录给出未完成交接。
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
    final_response = without_tool_call_after_limit(
        params,
        final_response,
        reason="repeated_failure_exhausted",
    )
    final_response = replace(
        final_response,
        runtime_status="unfinished",
        runtime_reason="REPEATED_TOOL_FAILURE_EXHAUSTED",
        runtime_source="tool_loop",
    )
    return final_prompt, final_response


# T-USER-001 收口:unknown 副作用出现(单次即收口,对齐错误合同)后模型不再有
# 工具权,基于真实工具记录给诚实总结;不自动续跑(用户未必要求继续,续跑会再造
# 一轮不确定副作用)。
def _final_response_after_unknown_outcome_halt(
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
    final_response = without_tool_call_after_limit(
        params, final_response, reason="unknown_outcome"
    )
    # 2026-08-15 3×3 cell1 真机: 错误合同对齐——UNKNOWN 的触发报码若属
    # taxonomy retryable=True 的「结果已知失败」(COMMAND_FAILED 命令失败读
    # 输出修正等), 收口按 REPEATED_TOOL_FAILURE(可续跑族, 模型开新轮读
    # reported_output_preview 修复); 「真未知」(retryable=False: 超时/
    # 执行者死/无码) 保持 TOOL_OPERATION_OUTCOME_UNKNOWN 单次收口不续跑。
    # 双席 seq1992 收紧: 仅凭报码不足——「进程非零」不自动等于「无部分
    # 副作用」。转 REPEATED_TOOL_FAILURE 必须叠加 handler_executed=False
    # 或 effect_outcome 非 unknown(执行器已声明副作用边界): handler 真实
    # 执行过的写命令失败(effect_outcome=unknown) 保持 UNKNOWN 人工核对闸。
    halt = getattr(params, "unknown_outcome_halt", None) or ()
    reported_code = str(halt[1] if len(halt) > 1 else "").strip().upper()
    effect = str(halt[2] if len(halt) > 2 else "").strip().lower()
    handler_executed = bool(halt[3] if len(halt) > 3 else True)
    try:
        from ..contracts.error_taxonomy import error_contract

        contract = error_contract(reported_code) if reported_code else None
        known_retryable_failure = bool(
            contract is not None
            and contract.retryable
            and reported_code
            and (not handler_executed or effect != "unknown")
        )
    except Exception:  # noqa: BLE001 判据失败保守走 unknown 不续跑
        known_retryable_failure = False
    runtime_reason = (
        "REPEATED_TOOL_FAILURE"
        if known_retryable_failure
        else "TOOL_OPERATION_OUTCOME_UNKNOWN"
    )
    final_response = replace(
        final_response,
        runtime_status="unfinished",
        runtime_reason=runtime_reason,
        runtime_source="tool_loop",
    )
    return final_prompt, final_response


# no-action 闸收口:informational 轮模型连续抗拦截提调用达限后不再给工具权,
# 收口轮剥掉工具调用并按未完成交接(等用户明确指示,而非自动执行或自动完成)。
def _final_response_after_no_action_gate(
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
    final_response = without_tool_call_after_limit(
        params, final_response, reason="no_action_gate"
    )
    final_response = replace(
        final_response,
        runtime_status="unfinished",
        runtime_reason="TOOL_ACTION_NOT_REQUIRED",
        runtime_source="tool_loop",
    )
    return final_prompt, final_response


# LLM: Archive once, then feed the same bounded projection to text and native histories before any
# later compact/window logic; do not let raw result refs bypass this choke point.
# 函数用途: 记录一次工具调用、更新运行事实，并把安全结果投影续入下一轮模型上下文。
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
    _mark_unknown_outcome_halt(agent, record)
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
    result_rendered = render_tool_result_for_live_prompt(record.result, archive_record)
    record.params.tool_context.append(
        f"[tool-record round={record.tool_rounds} index={record.idx}]\n"
        f"{render_tool_payload_for_live_prompt(record.model_payload)}\n"
        f"[tool-output-record round={record.tool_rounds} index={record.idx}]\n"
        f"{result_rendered}"
    )
    # 灰度双轨：native 下同时把这次「调用+结果」记进结构化 IR 历史（与上面的文本
    # tool_context 共存），供出站翻成原生 messages；text 协议下完全不走这里。
    _record_tool_call_ir_if_native(record, result_rendered)
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


# LLM: Native history must carry the reducer output, never the raw executor projection; otherwise
# large-output refs bypass the canonical read_artifact recovery contract.
# 函数用途: 把工具调用与有界、可恢复的结果正文追加到原生模型历史。
def _record_tool_call_ir_if_native(
    record: ToolCallRecordParams,
    result_rendered: str,
) -> None:
    """Append the exact canonical pair with the shared bounded model projection."""
    if not native_tool_use_active(record.params):
        return
    record_tool_call_ir(
        record.params,
        tool_rounds=record.tool_rounds,
        call=record.model_visible_call,
        result=record.result.with_live_prompt_projection(result_rendered),
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
