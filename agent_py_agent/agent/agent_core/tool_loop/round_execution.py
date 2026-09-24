# LLM: 本模块执行一轮 canonical ToolCall；仅将连续段判定交给窄查询，审批、并发、取消与 provider 顺序记账仍由原链负责。
# 模块用途: 装配实时调度事实并执行工具轮，隔离线程依赖、输出配对结果，用户拒绝后阻止相同调用重复弹框。

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, replace
from functools import partial
from typing import ClassVar, Literal

from ...backends import ModelResponse
from ...concurrency.interrupt import is_interrupted, register_interrupt_callback
from ...contracts.required_actions import required_action_assessment_failed
from ...contracts.tool_approval import (
    ToolApprovalDecision,
    build_tool_approval_request,
)
from ...conversation.authority import conversation_transcript_is_authoritative
from ...memory_archive import estimate_tokens
from ...tooling.action_policy import ActionDecision
from ...tooling.concurrency import ToolConcurrencyDescriptor, describe_tool_concurrency
from ...tooling.executor import ToolExecution
from ...tooling.runtime_contracts import (
    AppliedToolApproval,
    ToolCall,
    ToolFailureFacts,
    ToolResult,
)
from .._runtime_params import ToolLoopExecuteParams
from ..model.context_pressure import should_compact_before_more_tool_output
from ..runtime.context_compactor import runtime_compact_policy
from ..runtime.live_archive import archive_assistant_tool_round_if_enabled
from ..tool_context.call_reducer import (
    AssistantToolRoundContextRequest,
    render_assistant_tool_round_context,
)
from .segment_planning import (
    parallel_segment_end,
    parse_optional_batch_limit,
    resolve_parallel_batch_limit,
)

# 无动作闸只由执行轮消费和计数；收口层读取原halt事实，不另设阈值副本。
_NO_ACTION_GATE_STREAK_ATTR = "_no_action_gate_streak"
_NO_ACTION_GATE_HALT_LIMIT = 2

_STATEFUL_ORCHESTRATION_TOOLS = {
    "create_subagents",
}
_DEPENDENT_ORCHESTRATION_TOOLS = {
    "create_subagents",
    "send_guidance",
}
_CONTENT_OUTPUT_TOOLS = {
    "controlled_exec",
    "exec_command",
    "find_files",
    "list_files",
    "read_artifact",
    "read_file",
    "search_text",
    "shell",
    "shell_command",
    "watch_stream",
    "web_fetch",
    "web_search",
}


# LLM: A tool record keeps the executed call and the provider-authored call separate. Host-completed
# identity/path bindings belong to execution/audit only and must never be replayed as model input.
# 类用途: 保存一次工具执行的双视图：底座实际执行参数用于账本，模型原始参数用于后续上下文回放。
@dataclass(frozen=True)
class ToolCallRecordParams:
    __test__: ClassVar[bool] = False

    params: ToolLoopExecuteParams
    tool_rounds: int
    idx: int
    call: ToolCall
    result: ToolResult
    execution_states: tuple[str, ...] = ()
    model_call: ToolCall | None = None

    @property
    def payload(self) -> dict[str, object]:
        """Read-only projection for adjacent reporters; execution authority is ``call``."""

        return _tool_call_payload(self.call)

    @property
    def model_visible_call(self) -> ToolCall:
        """Return only the provider-authored call for prompt/native-history replay."""

        return self.model_call or self.call

    @property
    def model_payload(self) -> dict[str, object]:
        """Project the provider-authored arguments without host-only completion fields."""

        return _tool_call_payload(self.model_visible_call)


# LLM: This immutable execution request carries the admitted call identity into the sole executor;
# callers must not mutate it while approval or parallel scheduling is in flight.
# 类用途: 固定一条即将执行的工具调用及其轮次位置，供权限、并发和执行链共用。
@dataclass(frozen=True)
class ToolCallExecuteParams:
    __test__: ClassVar[bool] = False

    params: ToolLoopExecuteParams
    tool_rounds: int
    idx: int
    call: ToolCall
    model_call: ToolCall | None = None


@dataclass(frozen=True)
class ToolRoundExecutionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: ToolLoopExecuteParams
    tool_rounds: int
    response: ModelResponse
    calls: list[ToolCall]
    execute_one: Callable[[ToolCallExecuteParams], ToolExecution]
    record_one: Callable[[ToolCallRecordParams], None]
    current_prompt: str = ""


@dataclass(frozen=True)
class ToolProgressEvent:
    request: ToolRoundExecutionRequest
    idx: int
    call: ToolCall
    phase: Literal["deferred", "started", "finished", "interrupted"]
    status: str
    started_at: float | None = None
    result: ToolResult | None = None


@dataclass
class _ToolRoundProgress:
    subagent_output_written: bool = False
    stateful_orchestration_seen: bool = False
    handled_count: int = 0
    deferred_reason: str = ""


# LLM: 每段原位绑定实时 Compact/描述查询，不能把整轮调度事实提前冻结；实际完成顺序不影响 provider 顺序记账，联测取消与审批。
# 函数用途: 执行一轮 canonical 工具调用，保留只读并发段和顺序屏障，为执行、拒绝、延后与取消写入配对 ToolResult。
def execute_tool_round(request: ToolRoundExecutionRequest) -> bool:
    before_context_count = len(getattr(request.params, "tool_context", []) or [])
    _append_assistant_tool_round_context(request)
    calls = list(request.calls)
    # no-action 结构化闸(复核 seq 339):评估判 informational(requires_action=False)
    # 时模型仍提出的 ToolCall 一律不进 handler——全部转结构化拦截结果
    # (TOOL_ACTION_NOT_REQUIRED, handler_executed=False),并做有界计数。
    if _no_action_gate_active(request.params):
        return _gate_all_calls_for_no_action(request, calls)
    progress = _ToolRoundProgress()
    position = 0
    while position < len(calls):
        if _defer_unstarted_calls_if_needed(request, calls, position, progress):
            break
        parallel_end = parallel_segment_end(
            calls,
            position,
            batch_limit=_effective_parallel_batch_limit(request),
            defer_for_compact=partial(_should_defer_for_compact, request),
            describe_call=partial(_describe_round_call_concurrency, request),
        )
        if parallel_end - position >= 2:
            position = _execute_parallel_step(
                request,
                calls,
                position,
                parallel_end,
                before_context_count,
                progress,
            )
            if progress.deferred_reason:
                break
            continue
        _execute_serial_step(
            request,
            calls,
            position,
            before_context_count,
            progress,
        )
        if progress.deferred_reason:
            break
        position += 1
    _append_deferred_tool_call_notice(
        request,
        handled_count=progress.handled_count,
        reason=progress.deferred_reason,
    )
    _enforce_turn_context_budget(request.params, before_context_count)
    return progress.subagent_output_written


# no-action 闸激活条件:与 tool_choice_for_required_actions 的 informational 分支
# 完全一致(无 actions + 评估非 failed + source=model_structured + requires_action=False),
# 纯结构化信号判定,不解析模型话术。actions 存在(open 或 settled)时闸不激活——
# settled 场景模型仍需自主调用验证(修完文件后跑测试等),不能被误拦。
def _no_action_gate_active(params: ToolLoopExecuteParams) -> bool:
    snapshot = getattr(params, "effective_contract_snapshot", None)
    if snapshot is None:
        return False
    if tuple(getattr(snapshot, "required_actions", ()) or ()):
        return False
    if required_action_assessment_failed(snapshot):
        return False
    assessment = getattr(snapshot, "required_action_assessment", None)
    return (
        isinstance(assessment, dict)
        and assessment.get("source") == "model_structured"
        and assessment.get("requires_action") is False
    )


def _no_action_gated_result(call: ToolCall) -> ToolResult:
    payload = json.dumps(
        {
            "error": "本条消息被宿主评估为信息性陈述(requires_action=false),不执行任何操作。",
            "hint": "如需执行操作,请由用户明确指示后重新发起。",
        },
        ensure_ascii=False,
    )
    return ToolResult.failed(
        call,
        payload,
        error_code="TOOL_ACTION_NOT_REQUIRED",
        failure_stage="runtime_gate",
        facts=ToolFailureFacts(status="failed"),
    )


# LLM: 无动作闸的计数和阈值唯一归执行轮；只读结构化assessment，保留逐调用失败记录、halt时机及零handler执行。
# 函数用途: informational 轮对模型提出的全部调用做有界结构化拦截——handler 不执行、
# 每调用一条拦截结果(模型可读),连续 _NO_ACTION_GATE_HALT_LIMIT 轮拦截后设
# no_action_gate_halt,由 _tool_step_or_limit 收口轮接管(剥工具调用,等用户明确指示)。
def _gate_all_calls_for_no_action(
    request: ToolRoundExecutionRequest,
    calls: list[ToolCall],
) -> bool:
    streak = 0
    try:
        streak = int(getattr(request.params, _NO_ACTION_GATE_STREAK_ATTR, 0) or 0)
    except (TypeError, ValueError):
        streak = 0
    streak += 1
    object.__setattr__(request.params, _NO_ACTION_GATE_STREAK_ATTR, streak)
    for idx, call in enumerate(calls, start=1):
        result = _no_action_gated_result(call)
        _emit_tool_progress(
            ToolProgressEvent(request, idx, call, "deferred", "未执行", result=result)
        )
        request.record_one(
            ToolCallRecordParams(
                request.params,
                request.tool_rounds,
                idx,
                call,
                result,
                ("received", "gated", "persisted", "projected"),
                model_call=_model_visible_call(request, idx, call),
            )
        )
    if streak >= _NO_ACTION_GATE_HALT_LIMIT:
        object.__setattr__(request.params, "no_action_gate_halt", True)
    request.params.tool_context.append(
        "[tool-system:no-action-gate]\n"
        "本条用户消息被评估为信息性陈述(requires_action=false)，"
        f"系统已拦截本轮 {len(calls)} 个工具调用（TOOL_ACTION_NOT_REQUIRED，未执行）。"
        "请不要为这条消息执行任何操作或写入任何文件；直接如实回答即可，"
        "如需执行操作请等用户明确指示。"
    )
    return False


def _defer_unstarted_calls_if_needed(
    request: ToolRoundExecutionRequest,
    calls: list[ToolCall],
    position: int,
    progress: _ToolRoundProgress,
) -> bool:
    idx = position + 1
    call = calls[position]
    # 取消是顺序屏障：当前及后续未启动调用都得到 cancelled 结果，不能留下孤儿 ToolCall。
    if _round_cancelled(request):
        _record_unstarted_calls(
            request,
            calls,
            start_idx=idx,
            result_factory=_interrupted_result,
            phase="interrupted",
            status="中断",
        )
        progress.deferred_reason = "任务已被取消"
        return True
    if not _should_defer_for_compact(request, call.tool_name):
        return False
    _record_unstarted_calls(
        request,
        calls,
        start_idx=idx,
        result_factory=_compact_deferred_result,
        phase="deferred",
        status="延后",
    )
    _append_compact_deferred_notice(request, call.tool_name, idx)
    progress.deferred_reason = "上下文需要先 compact/resume"
    return True


def _execute_parallel_step(
    request: ToolRoundExecutionRequest,
    calls: list[ToolCall],
    position: int,
    parallel_end: int,
    before_context_count: int,
    progress: _ToolRoundProgress,
) -> int:
    outcomes = _execute_parallel_segment(
        request,
        calls[position:parallel_end],
        start_idx=position + 1,
    )
    transition_index = 0
    for outcome_idx, outcome_call, started_at, execution in outcomes:
        execution = _resolve_tool_approval(
            request,
            outcome_idx,
            outcome_call,
            execution,
        )
        wrote_output, transition = _record_execution(
            request,
            outcome_idx,
            started_at,
            execution,
        )
        progress.subagent_output_written |= wrote_output
        if transition and not transition_index:
            _remember_runtime_transition(
                request,
                transition,
                tool_name=outcome_call.tool_name,
                idx=outcome_idx,
            )
            transition_index = outcome_idx
    progress.handled_count = parallel_end
    if transition_index:
        _record_runtime_transition_deferred_calls(
            request,
            calls,
            start_idx=parallel_end + 1,
        )
        progress.deferred_reason = "前一个工具改变了耐久运行上下文"
    elif _round_cancelled(request):
        _record_unstarted_calls(
            request,
            calls,
            start_idx=parallel_end + 1,
            result_factory=_interrupted_result,
            phase="interrupted",
            status="中断",
        )
        progress.deferred_reason = "任务已被取消"
    elif _round_context_over_compact_budget(request, before_context_count):
        _record_remaining_content_calls_as_deferred(
            request,
            calls,
            start_idx=parallel_end + 1,
        )
        progress.deferred_reason = "上下文需要先 compact/resume"
    return parallel_end


# LLM: 串行步骤在首次 ActionPolicy ask 时暂停同一 ToolCall，收到精确批准后才重入 executor；不得先记录 approval_required 再让模型重提。
# 函数用途: 执行、审批并记录一条顺序工具调用。
def _execute_serial_step(
    request: ToolRoundExecutionRequest,
    calls: list[ToolCall],
    position: int,
    before_context_count: int,
    progress: _ToolRoundProgress,
) -> None:
    idx = position + 1
    call = calls[position]
    started_at = time.monotonic()
    _emit_tool_progress(ToolProgressEvent(request, idx, call, "started", "开始"))
    if _should_defer_orchestration(progress.stateful_orchestration_seen, call.tool_name):
        execution = _synthetic_execution(
            call,
            _deferred_orchestration_result(call),
            "ORCHESTRATION_CALL_DEFERRED",
        )
    else:
        execution = request.execute_one(
            ToolCallExecuteParams(
                request.params,
                request.tool_rounds,
                idx,
                call,
                model_call=_model_visible_call(request, idx, call),
            )
        )
        execution = _resolve_tool_approval(request, idx, call, execution)
    wrote_output, transition = _record_execution(request, idx, started_at, execution)
    progress.handled_count = idx
    progress.subagent_output_written |= wrote_output
    progress.stateful_orchestration_seen |= call.tool_name in _STATEFUL_ORCHESTRATION_TOOLS
    if transition:
        _remember_runtime_transition(
            request,
            transition,
            tool_name=call.tool_name,
            idx=idx,
        )
        _record_runtime_transition_deferred_calls(request, calls, start_idx=idx + 1)
        progress.deferred_reason = "前一个工具改变了耐久运行上下文"
    elif _round_context_over_compact_budget(request, before_context_count):
        _record_remaining_content_calls_as_deferred(request, calls, start_idx=idx + 1)
        progress.deferred_reason = "上下文需要先 compact/resume"


# LLM: 审批 consumer 是 effective_on_chunk 上的宿主能力；无 consumer/unavailable 保留旧 approval_required 终态，绝不擅自批准。
# 函数用途: 等待一条审批决定，并在批准时用原 call identity 重新执行同一工具调用。
def _resolve_tool_approval(
    request: ToolRoundExecutionRequest,
    idx: int,
    original_call: ToolCall,
    execution: ToolExecution,
) -> ToolExecution:
    if execution.decision.status != "ask":
        return execution
    on_chunk = getattr(request.params, "effective_on_chunk", None)
    consumer = getattr(on_chunk, "request_permission", None)
    if not callable(consumer):
        return execution
    approval_request = build_tool_approval_request(
        execution.call,
        request_id=request.params.request_id,
        round_number=request.tool_rounds,
        call_index=idx,
        description=_approval_description(request, idx, execution.call),
    )
    prior_rejection = _matching_runtime_rejection(request.params, approval_request.binding)
    if prior_rejection is not None:
        prior_decision = str(prior_rejection.get("decision") or "denied")
        if prior_decision not in {"denied", "cancelled"}:
            prior_decision = "denied"
        return _rejected_approval_execution(
            execution,
            ToolApprovalDecision(approval_request.permission_id, prior_decision),
            repeated=True,
        )
    try:
        raw_decision = consumer(
            approval_request.to_dict(),
            cancellation_token=request.params.cancellation_token,
        )
        decision = (
            raw_decision
            if isinstance(raw_decision, ToolApprovalDecision)
            else ToolApprovalDecision.from_mapping(raw_decision)
            if isinstance(raw_decision, Mapping)
            else ToolApprovalDecision(approval_request.permission_id, "unavailable")
        )
    except (TypeError, ValueError):
        decision = ToolApprovalDecision(approval_request.permission_id, "unavailable")
    if decision.permission_id != approval_request.permission_id:
        return execution
    if decision.approved:
        approved_actions = getattr(request.params, "runtime_approved_actions", None)
        if not isinstance(approved_actions, list):
            return execution
        approved_actions.append(
            approval_request.approved_binding(decision)
        )
        resumed = request.execute_one(
            ToolCallExecuteParams(
                request.params,
                request.tool_rounds,
                idx,
                original_call,
                model_call=_model_visible_call(request, idx, original_call),
            )
        )
        resumed = _with_applied_approval_fact(
            resumed,
            approval_request=approval_request,
            decision=decision,
        )
        if decision.feedback:
            tool_context = getattr(request.params, "tool_context", None)
            if isinstance(tool_context, list):
                tool_context.append(
                    "[tool-approval-feedback]\n"
                    f"用户批准 {original_call.tool_name} 时补充：{decision.feedback}"
                )
        return resumed
    if decision.decision == "unavailable":
        return execution
    rejected_actions = getattr(request.params, "runtime_rejected_actions", None)
    if isinstance(rejected_actions, list):
        rejected_actions.append(approval_request.rejected_binding(decision))
    return _rejected_approval_execution(execution, decision)


# LLM: A resumed exact call must carry the host-owned approval outcome into the canonical result.
# The provider may describe it, but it cannot infer or overwrite this fact from timing or prose.
# 函数用途: 给审批后执行的同一工具结果附上已应用的批准事实，供后续模型和审计准确区分“待审批”与“已执行”。
def _with_applied_approval_fact(
    execution: ToolExecution,
    *,
    approval_request: object,
    decision: ToolApprovalDecision,
) -> ToolExecution:
    applied = AppliedToolApproval(
        permission_id=str(getattr(approval_request, "permission_id", "") or ""),
        decision=decision.decision,
    )
    return replace(execution, result=replace(execution.result, applied_approval=applied))


# LLM: 重复拒绝只比较当前 run 中宿主记录的 tool_name + args_hash；自然语言反馈、展示说明和 provider call id 都不能改变裁决。
# 函数用途: 查找用户是否已拒绝或取消过完全相同的工具参数，命中后直接复用拒绝而不再次询问。
def _matching_runtime_rejection(
    params: object,
    binding: Mapping[str, str],
) -> dict[str, str] | None:
    tool_name = str(binding.get("tool_name") or "").strip()
    args_hash = str(binding.get("args_hash") or "").strip()
    if not tool_name or not args_hash:
        return None
    runtime_items = getattr(params, "runtime_rejected_actions", None)
    if not isinstance(runtime_items, list):
        return None
    for item in reversed(runtime_items):
        if not isinstance(item, Mapping):
            continue
        if (
            str(item.get("tool_name") or "").strip() == tool_name
            and str(item.get("args_hash") or "").strip() == args_hash
            and str(item.get("decision") or "").strip() in {"denied", "cancelled"}
        ):
            return {str(key): str(value or "") for key, value in item.items()}
    return None


# LLM: 审批说明复用现有公开进度脱敏链，仅显示 tool 与有界 path/command 摘要；完整 arguments 不进入 UI event。
# 函数用途: 生成 终端交互 工具确认框中的一行调用说明。
def _approval_description(
    request: ToolRoundExecutionRequest,
    idx: int,
    call: ToolCall,
) -> str:
    detail = _payload_progress_detail(call)
    public_detail = _public_progress_text(
        ToolProgressEvent(request, idx, call, "started", "等待审批"),
        detail,
        max_chars=240,
    )
    return f"{call.tool_name}({public_detail})" if public_detail else call.tool_name


# LLM: 拒绝/取消必须形成与原 tool_use 配对的单一终态结果，handler_executed=False；repeated 仅改变说明，授权仍来自结构化决定。
# 函数用途: 把首次或重复的用户拒绝/取消转换为标准 ToolExecution，并明确告知模型不要重试相同调用。
def _rejected_approval_execution(
    execution: ToolExecution,
    decision: ToolApprovalDecision,
    *,
    repeated: bool = False,
) -> ToolExecution:
    cancelled = decision.decision == "cancelled"
    code = "CANCELLED" if cancelled else "APPROVAL_REJECTED"
    status = "cancelled" if cancelled else "failed"
    if repeated:
        message = (
            "用户此前已经取消了完全相同的工具调用，本次没有再次询问，也没有执行。"
            if cancelled
            else "用户此前已经拒绝了完全相同的工具调用，本次没有再次询问，也没有执行。"
        )
    else:
        message = "用户取消了这次工具调用，工具没有执行。" if cancelled else "用户拒绝了这次工具调用，工具没有执行。"
    if decision.feedback:
        message = (
            f"{message}\n用户反馈：{decision.feedback}\n"
            "请按反馈调整做法，不要再次尝试完全相同的工具调用。"
        )
    else:
        message = (
            f"{message}\n停止当前操作并等待用户说明下一步；"
            "不要再次尝试完全相同的工具调用。"
        )
    action_decision = ActionDecision(
        "deny",
        (code,),
        {
            "failure_stage": "authorization",
            "approval_id": decision.permission_id,
            "approval_decision": decision.decision,
            "repeated_rejection": repeated,
        },
        resolved_effect=execution.decision.resolved_effect,
    )
    result = ToolResult.failed(
        execution.call,
        message,
        error_code=code,
        failure_stage="authorization",
        facts=ToolFailureFacts(
            status=status,
            metadata={
                "action_decision": action_decision.to_dict(),
                "approval_decision": decision.to_dict(),
            },
        ),
    )
    return ToolExecution(
        execution.call,
        action_decision,
        result,
        (
            "received",
            "normalized",
            "validated",
            "authorized",
            "approval_pending",
            "cancelled" if cancelled else "user_denied",
            "persisted",
            "projected",
        ),
    )


@dataclass(frozen=True)
class _ParallelThreadContext:
    transient_values: tuple[tuple[str, object], ...]
    subagent_run_id: str
    subagent_attempt_id: str
    task_attributes: dict[str, object] | None


# LLM: 本装配点在该调用的 Compact 查询之后才读原快照、工作根和写边界；这些只是调度事实，不替代 ActionPolicy 授权。
# 函数用途: 为正在扫描的一个工具生成原并发描述，避免提前取快照改变动态边界或查询异常的时序。
def _describe_round_call_concurrency(
    request: ToolRoundExecutionRequest,
    call: ToolCall,
) -> ToolConcurrencyDescriptor:
    return describe_tool_concurrency(
        getattr(request.params, "tool_runtime_snapshot", None),
        call,
        workspace_root=getattr(
            request.agent, "effective_workspace_root", getattr(request.agent, "root", None)
        ),
        write_boundary=getattr(request.params, "write_boundary", None),
    )


# LLM: 保持任务属性优先、缺省才读取对应配置的顺序；max_tool_calls_per_round 仅限制执行批，不能制造未执行 ToolResult。
# 函数用途: 原位读取两项有效数值，再交给窄策略合并批上限，避免未使用的配置提前触发读取或异常。
def _effective_parallel_batch_limit(request: ToolRoundExecutionRequest) -> int:
    parallel_limit = _task_attribute_int(request, "max_parallel_tool_calls")
    if parallel_limit is None:
        parallel_limit = _agent_config_int(request.agent, "max_parallel_tool_calls")
    tool_batch_limit = _task_attribute_int(request, "max_tool_calls_per_round")
    if tool_batch_limit is None:
        tool_batch_limit = _agent_config_int(request.agent, "max_tool_calls_per_round")
    return resolve_parallel_batch_limit(parallel_limit, tool_batch_limit)


# LLM: 每个并行工具都携带独立 Context 副本，继承本工作片模型/权限；线程本地 runner 身份仍走原桥，不共享可进入的 Context。
# 函数用途: 并发执行一组工具并保持原顺序记账，避免模型切换或权限选择在工作线程里退回部署默认。
def _execute_parallel_segment(
    request: ToolRoundExecutionRequest,
    calls: list[ToolCall],
    *,
    start_idx: int,
) -> list[tuple[int, ToolCall, float, ToolExecution]]:
    context = _capture_parallel_thread_context(request.agent)
    scheduled: list[tuple[int, ToolCall, float]] = []
    for offset, call in enumerate(calls):
        idx = start_idx + offset
        started_at = time.monotonic()
        _emit_tool_progress(ToolProgressEvent(request, idx, call, "started", "开始"))
        scheduled.append((idx, call, started_at))
    token = getattr(request.params, "cancellation_token", None)
    cancel = getattr(token, "cancel", None)
    callback = (lambda: cancel("interrupted")) if callable(cancel) else (lambda: None)
    workers = min(8, len(scheduled))
    with register_interrupt_callback(callback):
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="my-agent-tool",
        ) as pool:
            futures = [
                pool.submit(
                    copy_context().run,
                    _execute_parallel_call,
                    request,
                    idx,
                    call,
                    context,
                )
                for idx, call, _started_at in scheduled
            ]
            executions = [future.result() for future in futures]
    return [
        (idx, original_call, started_at, execution)
        for (idx, original_call, started_at), execution in zip(
            scheduled,
            executions,
            strict=True,
        )
    ]


def _execute_parallel_call(
    request: ToolRoundExecutionRequest,
    idx: int,
    call: ToolCall,
    context: _ParallelThreadContext,
) -> ToolExecution:
    restore_values = _install_parallel_thread_context(request.agent, context)
    previous_runner: dict[str, object] | None = None
    if context.subagent_run_id:
        from ...runtime_context import set_current_subagent_context

        previous_runner = set_current_subagent_context(
            request.agent,
            run_id=context.subagent_run_id,
            attempt_id=context.subagent_attempt_id,
            task_attributes=context.task_attributes,
        )
    try:
        return request.execute_one(
            ToolCallExecuteParams(
                request.params,
                request.tool_rounds,
                idx,
                call,
                model_call=_model_visible_call(request, idx, call),
            )
        )
    finally:
        if previous_runner is not None:
            from ...runtime_context import restore_current_subagent_context

            restore_current_subagent_context(request.agent, previous_runner)
        _restore_parallel_thread_context(request.agent, restore_values)


# LLM: 并行工具只继承本线程显式依赖与 runner 身份，不能读取其它会话的模型连接；接收线程必须对称恢复。
# 函数用途: 冻结派工等工具需要的临时上下文，保留显式替身注入但不共享跨线程可变字段。
def _capture_parallel_thread_context(agent: object) -> _ParallelThreadContext:
    from ...runtime_context import (
        ThreadLocalAgentAttribute,
        current_subagent_attempt_id,
        current_subagent_run_id,
        current_task_attributes,
    )

    values: list[tuple[str, object]] = []
    for name in (
        "_current_user_prompt",
        "_current_run_params",
        "_current_run_task_workspace",
        "_current_skill_snapshot",
        "_subagent_worker_backend_override",
    ):
        descriptor = getattr(type(agent), name, None)
        if isinstance(descriptor, ThreadLocalAgentAttribute) and hasattr(agent, name):
            values.append((name, getattr(agent, name)))
    attrs = current_task_attributes(agent)
    return _ParallelThreadContext(
        tuple(values),
        current_subagent_run_id(agent),
        current_subagent_attempt_id(agent),
        dict(attrs) if isinstance(attrs, dict) else None,
    )


def _install_parallel_thread_context(
    agent: object,
    context: _ParallelThreadContext,
) -> tuple[tuple[str, bool, object], ...]:
    previous: list[tuple[str, bool, object]] = []
    for name, value in context.transient_values:
        existed = hasattr(agent, name)
        previous.append((name, existed, getattr(agent, name, None)))
        setattr(agent, name, value)
    return tuple(previous)


def _restore_parallel_thread_context(
    agent: object,
    previous: tuple[tuple[str, bool, object], ...],
) -> None:
    for name, existed, value in reversed(previous):
        if existed:
            setattr(agent, name, value)
        elif hasattr(agent, name):
            delattr(agent, name)


def _record_execution(
    request: ToolRoundExecutionRequest,
    idx: int,
    started_at: float,
    execution: ToolExecution,
) -> tuple[bool, dict[str, str] | None]:
    call = execution.call
    result = execution.result
    if result.duration_ms <= 0:
        result = result.with_execution_facts(
            duration_ms=(time.monotonic() - started_at) * 1000,
        )
        execution = replace(execution, result=result)
    _emit_tool_progress(
        ToolProgressEvent(
            request,
            idx,
            call,
            "finished",
            _finished_status(result),
            started_at,
            result,
        )
    )
    request.record_one(
        ToolCallRecordParams(
            request.params,
            request.tool_rounds,
            idx,
            call,
            result,
            execution.states,
            model_call=_model_visible_call(request, idx, call),
        )
    )
    # 会话运行时 式循环中，写任何文件（包括历史 output.json）都只是工具结果；
    # 不能跳过下一次模型采样或由宿主据产物内容强制收口。
    return False, _runtime_transition_after_tool(result)


def _remember_runtime_transition(
    request: ToolRoundExecutionRequest,
    transition: dict[str, str],
    *,
    tool_name: str,
    idx: int,
) -> None:
    state = getattr(request.params, "live_archive_state", None)
    if isinstance(state, dict):
        state["pending_runtime_transition"] = {
            **transition,
            "tool": tool_name,
            "tool_round": request.tool_rounds,
            "tool_index": idx,
        }


def _round_cancelled(request: ToolRoundExecutionRequest) -> bool:
    token = getattr(request.params, "cancellation_token", None)
    return is_interrupted() or bool(token and getattr(token, "cancelled", False))


def _runtime_transition_after_tool(
    result: ToolResult,
) -> dict[str, str] | None:
    if not result.ok:
        return None
    details = result.metadata.get("handler_details")
    transition = details.get("runtime_transition") if isinstance(details, dict) else None
    if not isinstance(transition, dict):
        return None
    kind = str(transition.get("kind") or "").strip()
    reason = str(transition.get("reason") or "").strip()
    resume = str(transition.get("resume") or "").strip()
    if kind != "context_refresh" or resume != "next_durable_slice" or not reason:
        return None
    return {"kind": kind, "reason": reason, "resume": resume}




def _record_unstarted_calls(
    request: ToolRoundExecutionRequest,
    calls: list[ToolCall],
    *,
    start_idx: int,
    result_factory: Callable[[ToolCall], ToolResult],
    phase: Literal["deferred", "interrupted"],
    status: str,
) -> None:
    """Pair every admitted but unstarted call with one host-owned terminal result."""

    if start_idx <= 0:
        return
    for idx, call in enumerate(calls[start_idx - 1 :], start=start_idx):
        result = result_factory(call)
        _emit_tool_progress(ToolProgressEvent(request, idx, call, phase, status, result=result))
        request.record_one(
            ToolCallRecordParams(
                request.params,
                request.tool_rounds,
                idx,
                call,
                result,
                (
                    "received",
                    "cancelled" if result.status == "cancelled" else "failed",
                    "persisted",
                    "projected",
                ),
                model_call=_model_visible_call(request, idx, call),
            )
        )


# LLM: Provider history must replay the exact admitted model arguments, while execution may carry
# rebased paths and trusted bindings. Position plus call identity is the only accepted pairing.
# 函数用途: 按本轮原始调用位置取回模型真正提交的参数，避免把宿主补充字段回灌给模型。
def _model_visible_call(
    request: ToolRoundExecutionRequest,
    idx: int,
    execution_call: ToolCall,
) -> ToolCall:
    position = max(0, int(idx or 0) - 1)
    if position < len(request.calls):
        candidate = request.calls[position]
        if (
            candidate.call_id == execution_call.call_id
            and candidate.tool_name == execution_call.tool_name
        ):
            return candidate
    return execution_call


# 函数用途: 中断时给本工具一条结构化"已中断"结果(模型可读懂并收尾)。
def _interrupted_result(call: ToolCall) -> ToolResult:
    payload = json.dumps(
        {"error": "任务已被取消,本工具未执行。", "hint": "停止派发新动作,保存已有进展后收尾。"},
        ensure_ascii=False,
    )
    return ToolResult.failed(
        call,
        payload,
        error_code="CANCELLED",
        failure_stage="runtime_gate",
        facts=ToolFailureFacts(status="cancelled"),
    )


# LLM: 单回合聚合预算。
#   既有防线只管"单个结果过大就外置";本防线兜"单个都不大、本轮累计巨大"
#   (几十个中型 read/search 同轮返回)。超预算时从最大段开始截断到安全份额,
#   截口落在换行处,并注明恢复路径(重新调用工具/读档案)。纯框架层,模型无感。
_TURN_TOOL_CONTEXT_BUDGET_CHARS = 200_000
_TURN_BUDGET_KEEP_CHARS = 20_000


# 函数用途: 本轮工具输出总量超预算时,把最大的几段裁到安全大小(裁口带提示)。
def _enforce_turn_context_budget(params: ToolLoopExecuteParams, before_context_count: int) -> None:
    context = getattr(params, "tool_context", None)
    if not isinstance(context, list) or len(context) <= before_context_count:
        return
    indexed = list(enumerate(context))[before_context_count:]
    total = sum(len(str(text)) for _, text in indexed)
    for idx, text in sorted(indexed, key=lambda item: len(str(item[1])), reverse=True):
        if total <= _TURN_TOOL_CONTEXT_BUDGET_CHARS:
            return
        body = str(text)
        if len(body) <= _TURN_BUDGET_KEEP_CHARS:
            return
        context[idx] = _clip_at_newline(body, _TURN_BUDGET_KEEP_CHARS) + (
            "\n... [本轮工具输出总量超预算,此结果已截断;"
            "需要完整内容请用更窄的参数重新调用该工具,或按上方锚点读取档案。]"
        )
        total -= len(body) - len(context[idx])


# 函数用途: 把文本裁到限长,裁口尽量落在换行符上(避免半行残句)。
def _clip_at_newline(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text.rfind("\n", max_chars // 2, max_chars)
    return text[: cut if cut > 0 else max_chars]


# LLM: 只对会增加大量上下文的内容工具应用统一 compact 阈值；不得在此维护第二份百分比或 digest 状态。
# 函数用途: 判断当前内容工具是否应等会话先完成 compact 后再执行。
def _should_defer_for_compact(request: ToolRoundExecutionRequest, tool_name: str) -> bool:
    if tool_name not in _CONTENT_OUTPUT_TOOLS:
        return False
    if _conversation_owns_compaction(request.params):
        return False
    return should_compact_before_more_tool_output(
        request.agent,
        request.params,
        request.current_prompt,
    )


def _append_assistant_tool_round_context(request: ToolRoundExecutionRequest) -> None:
    rendered = render_assistant_tool_round_context(
        AssistantToolRoundContextRequest(
            request.response.text,
            [_tool_call_payload(call) for call in request.calls],
        )
    )
    request.params.tool_context.append(f"[assistant-tool-round-{request.tool_rounds}]\n{rendered}")
    # 灰度双轨：native 下先为本轮开一条 AssistantTurn 并落定其可见文本（取该轮真实
    # ModelResponse.text）；同轮工具结果随后由 _record_tool_call 追加进这条 turn。
    _open_assistant_turn_ir_if_native(request)
    archive_assistant_tool_round_if_enabled(
        request.agent,
        request.params,
        tool_round=request.tool_rounds,
        response_text=request.response.text,
        tool_calls=[_tool_call_payload(call) for call in request.calls],
    )


# LLM: 原生工具轮必须把 ModelResponse 的可见 text 与内部有序 content blocks 一起写入 IR；content blocks 不得进入 tool_context 展示文本。
# 函数用途: 在 native 模式下为当前模型轮建立完整的 assistant 历史，供下一轮模型请求续接。
def _open_assistant_turn_ir_if_native(request: ToolRoundExecutionRequest) -> None:
    from ..native_tool_protocol import native_tool_use_active
    from ..tool_ir_history import open_assistant_turn_ir

    if not native_tool_use_active(request.params):
        return
    open_assistant_turn_ir(
        request.params,
        tool_rounds=request.tool_rounds,
        response_text=str(getattr(request.response, "text", "") or ""),
        response_content_blocks=list(
            getattr(request.response, "assistant_content_blocks", None) or []
        ),
    )


# LLM: 该内部记录必须明确“工具未执行”，供恢复轮和后续模型保持幂等；它不会直接投递给用户。
# 函数用途: 在工具上下文中登记因 compact 延后的调用，提醒恢复后从原目标继续。
def _append_compact_deferred_notice(
    request: ToolRoundExecutionRequest,
    tool_name: str,
    idx: int,
) -> None:
    request.params.tool_context.append(
        "[tool-system]\n"
        "当前上下文已达到 compact 阈值；"
        f"本轮第 {idx} 个 {tool_name or 'tool'} 调用已登记为 CONTEXT_COMPACT_DEFERRED，实际没有执行。\n"
        "系统会先走 compact/resume，再继续未执行的读取、搜索或命令；不要把这个工具调用当作已经完成。"
    )


def _compact_deferred_result(call: ToolCall) -> ToolResult:
    return ToolResult.failed(
        call,
        "CONTEXT_COMPACT_DEFERRED: 当前上下文需要先 compact/resume；本次工具调用未执行，恢复后从同一目标继续。",
        error_code="CONTEXT_COMPACT_DEFERRED",
        failure_stage="runtime_gate",
    )


def _record_remaining_content_calls_as_deferred(
    request: ToolRoundExecutionRequest,
    calls: list[ToolCall],
    *,
    start_idx: int,
) -> None:
    deferred = max(0, len(calls) - start_idx + 1)
    _record_unstarted_calls(
        request,
        calls,
        start_idx=start_idx,
        result_factory=_compact_deferred_result,
        phase="deferred",
        status="延后",
    )
    if deferred:
        request.params.tool_context.append(
            "[tool-system]\n"
            f"本轮剩余 {deferred} 个工具已登记为 CONTEXT_COMPACT_DEFERRED；"
            "compact/resume 后系统会按这些结构化记录继续，不需要凭记忆重造调用。"
        )


def _record_runtime_transition_deferred_calls(
    request: ToolRoundExecutionRequest,
    calls: list[ToolCall],
    *,
    start_idx: int,
) -> None:
    _record_unstarted_calls(
        request,
        calls,
        start_idx=start_idx,
        result_factory=_runtime_transition_deferred_result,
        phase="deferred",
        status="等待上下文刷新",
    )


def _runtime_transition_deferred_result(call: ToolCall) -> ToolResult:
    return ToolResult.failed(
        call,
        "RUNTIME_TRANSITION_DEFERRED: 前一调用改变了耐久运行上下文；本调用未执行，必须从新快照重新发起。",
        error_code="RUNTIME_TRANSITION_DEFERRED",
        failure_stage="runtime_gate",
    )


# LLM: 只读取任务属性中被请求的批大小字段，转换合同由 segment_planning 统一维护。
# 函数用途: 读取任务对某一批上限的覆盖值，无有效覆盖时让装配点继续读取配置。
def _task_attribute_int(request: ToolRoundExecutionRequest, key: str) -> int | None:
    attrs = getattr(request.params, "task_attributes", None)
    if not isinstance(attrs, dict) or key not in attrs:
        return None
    return parse_optional_batch_limit(attrs.get(key))


# LLM: 配置查询仍按原 hasattr/getattr 顺序发生，不能将配置对象传进段判定模块。
# 函数用途: 读取缺少任务覆盖的批大小配置，并保留原转换异常行为。
def _agent_config_int(agent: object, key: str) -> int | None:
    config = getattr(agent, "config", None)
    if config is None or not hasattr(config, key):
        return None
    return parse_optional_batch_limit(getattr(config, key))


def _append_deferred_tool_call_notice(
    request: ToolRoundExecutionRequest,
    *,
    handled_count: int,
    reason: str,
) -> None:
    total = len(request.calls)
    if handled_count >= total:
        return
    deferred_count = total - handled_count
    request.params.tool_context.append(
        "[tool-system]\n"
        f"本轮模型请求了 {total} 个工具调用；由于{reason or '运行时边界要求分轮处理'}，"
        f"只处理到前 {handled_count} 个，剩余 {deferred_count} 个没有执行。\n"
        "这些工具已有结构化的未执行回执；回执只用于审计和恢复，不代表工具已经执行。\n"
        "下一轮请继续处理未完成的读取、写入或检查；不要把未执行的工具调用当作已经完成。"
    )


def _round_context_over_compact_budget(
    request: ToolRoundExecutionRequest, before_context_count: int
) -> bool:
    if _conversation_owns_compaction(request.params):
        return False
    if not str(request.current_prompt or ""):
        return False
    if not _persistent_compact_enabled(request.agent, request.params):
        return False
    policy = runtime_compact_policy(
        request.agent,
        save=True,
        context_scope=str(getattr(request.params, "context_scope", "default") or "default"),
    )
    threshold = int(policy.trigger_tokens or 0)
    if threshold <= 0:
        return False
    tool_context = list(getattr(request.params, "tool_context", []) or [])
    new_context = tool_context[before_context_count:]
    prompt_tokens = estimate_tokens(request.current_prompt) + estimate_tokens(new_context)
    return prompt_tokens >= threshold


# LLM: Structured transcript authority, not the presentation scope string, selects the one
# durable Compact owner for both foreground and delegated agent threads.
# 函数用途: 判断工具轮是否应把持久压缩交给 ConversationStore，避免重复归档续跑。
def _conversation_owns_compaction(params: ToolLoopExecuteParams) -> bool:
    return conversation_transcript_is_authoritative(
        getattr(params, "task_attributes", None)
    )


def _persistent_compact_enabled(agent: object, params: object) -> bool:
    save = getattr(params, "save", None)
    if save is not None:
        return bool(save)
    return bool(getattr(getattr(agent, "config", None), "auto_save_memory", True))


# LLM: Typed sink methods are the primary progress protocol; plain callable text
# callbacks are legacy fallback only. Display failures must never fail tool execution.
# 函数用途: 将一次工具阶段发送给结构化 TUI/子代理事件接收器，旧文本 callback 仅作兼容。
def _emit_tool_progress(event: ToolProgressEvent) -> None:
    on_chunk = getattr(event.request.params, "effective_on_chunk", None)
    tool_name = event.call.tool_name or "unknown"
    detail = _payload_progress_detail(event.call)
    elapsed = ""
    if event.started_at is not None:
        elapsed = f" {max(0.0, time.monotonic() - event.started_at):.2f}s"
    suffix = f": {detail}" if detail else ""
    legacy_text = (
        f"\n[工具] round={event.request.tool_rounds} "
        f"#{event.idx} {tool_name} {event.status}{elapsed}{suffix}\n"
    )
    progress_writer = getattr(on_chunk, "write_progress", None)
    if callable(progress_writer):
        try:
            progress_writer(_structured_tool_progress(event, tool_name, detail), legacy_text)
        except Exception:
            return
        return
    if not callable(on_chunk):
        return
    try:
        on_chunk(legacy_text)
    except Exception:
        return


# LLM: A successful tool may explicitly return a canonical task-progress
# snapshot either directly or under task_progress_seed; no prose is parsed.
# 函数用途: 从 task_progress 或 create_subagents 的结构化输出提取清单，供 TUI 固定 Todo 区展示。
def _task_progress_items_from_output(output: object) -> list[dict[str, object]] | None:
    text = str(output or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    return _task_progress_items_from_mapping(payload) if isinstance(payload, Mapping) else None


# LLM: Handler envelopes survive output externalization, so this scalar/list
# parser is the canonical way to recover an explicit Todo snapshot from them.
# 函数用途: 从工具结果对象或 task_progress_seed 中提取有界 Todo 项。
def _task_progress_items_from_mapping(
    payload: Mapping[str, object],
) -> list[dict[str, object]] | None:
    projection = _task_progress_projection_from_mapping(payload)
    items = projection.get("items") if projection is not None else None
    if not isinstance(items, list):
        return None
    clean: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        clean.append(
            {
                "id": str(item.get("id") or ""),
                "title": str(item.get("title") or item.get("summary") or ""),
                "status": str(item.get("status") or "pending"),
            }
        )
    return clean if clean else None


# LLM: A task_progress handler envelope and a create_subagents seed use two
# named containers but the same bounded display contract. The explicit current
# projection wins over full model-visible ledger items; prose is never parsed.
# 函数用途: 从工具结果中找到当前回合的 Todo 展示快照。
def _task_progress_projection_from_mapping(
    payload: Mapping[str, object],
) -> Mapping[str, object] | None:
    projection = payload.get("task_progress_projection")
    if isinstance(projection, Mapping):
        return projection
    seed = payload.get("task_progress_seed")
    if isinstance(seed, Mapping):
        return seed
    return payload if isinstance(payload.get("items"), list) else None


# LLM: 事件仅含公开预览/ref和typed事实；归档先于事件裁剪，缺失只认handler字段，不能改变工具或Todo终态。
# 函数用途: 把工具进度整理成有界显示，完成时保存原文并如实说明采集不完整。
def _structured_tool_progress(
    event: ToolProgressEvent,
    tool_name: str,
    detail: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "round": event.request.tool_rounds,
        "call_index": event.idx,
        "tool": tool_name,
        "phase": event.phase,
        "status": event.status,
    }
    if detail:
        payload["detail"] = _public_progress_text(event, detail, max_chars=240)
    if event.result is not None:
        payload["ok"] = bool(event.result.ok)
        payload["handler_executed"] = bool(event.result.handler_executed)
        payload["duration_ms"] = max(0, int(event.result.duration_ms or 0))
        if event.result.failure_stage:
            payload["failure_stage"] = event.result.failure_stage
        if event.result.error_code:
            payload["error_code"] = event.result.error_code
        output = _tool_progress_output(event)
        if output:
            payload["output"] = output
        handler_details = event.result.metadata.get("handler_details")
        raw_display = (
            handler_details.get("display")
            if isinstance(handler_details, dict)
            else None
        )
        display = _public_progress_display(event, raw_display)
        if display:
            payload["display"] = display
        if event.phase == "finished":
            from .display_archive import archive_tool_display, command_display_incomplete
            reference = archive_tool_display(event, raw_display)
            if reference:
                payload["display_archive_ref"] = reference
            if command_display_incomplete(raw_display):
                payload["history_incomplete"] = True
        if bool(event.result.ok):
            items = _task_progress_items_from_result(event.result)
            if items is not None:
                payload["task_progress_items"] = items
            generation_id, plan_revision = _task_progress_identity_from_result(
                event.result
            )
            if generation_id:
                payload["task_progress_generation_id"] = generation_id
                payload["task_progress_plan_revision"] = plan_revision
    if event.started_at is not None:
        payload["elapsed_seconds"] = round(
            max(0.0, time.monotonic() - event.started_at),
            3,
        )
    return payload


# LLM: 外置输出展示既有安全 preview 或 typed inline text，不读取 blob、不展开 refs；正文仍经统一公开脱敏和大小预算。
# 函数用途: 为主/子工具卡保留真实结果预览，避免 Read/计划等一归档就只剩操作号；归档身份和完整结果不改写。
def _tool_progress_output(event: ToolProgressEvent) -> str:
    result = event.result
    if result is None:
        return ""
    archive = result.metadata.get("archive_output_record")
    if isinstance(archive, Mapping) and archive.get("output_externalized") is True:
        preview = archive.get("output_preview")
        if not isinstance(preview, str) or not preview.strip():
            # ToolResult.output 会把 ref 物理路径也拼入正文；这里只取已经投影好的文字块。
            preview = "\n".join(block.text for block in result.content_blocks if block.type == "text")
        return _public_progress_text(event, preview, max_chars=1600) or "完整输出已归档（暂无文字预览）"
    return _public_progress_text(event, result.output, max_chars=1600)


# LLM: Result envelopes are the durable source for Todo snapshots after large
# output externalization; body parsing exists only for small legacy results.
# 函数用途: 从成功工具结果中读取实时 Todo 快照。
def _task_progress_items_from_result(
    result: ToolResult,
) -> list[dict[str, object]] | None:
    handler_details = result.metadata.get("handler_details")
    items = (
        _task_progress_items_from_mapping(handler_details)
        if isinstance(handler_details, Mapping)
        else None
    )
    return items if items is not None else _task_progress_items_from_output(result.output)


# LLM: Generation identity comes only from the handler's structured envelope;
# legacy textual output may still provide rows but cannot claim stale-plan fencing.
# 函数用途: 读取工具结果中的 Todo 回合标识与修订号。
def _task_progress_identity_from_result(result: ToolResult) -> tuple[str, int]:
    handler_details = result.metadata.get("handler_details")
    if not isinstance(handler_details, Mapping):
        return "", 0
    projection = _task_progress_projection_from_mapping(handler_details)
    if projection is None:
        return "", 0
    generation_id = str(projection.get("generation_id") or "").strip()
    try:
        revision = max(0, int(projection.get("plan_revision") or 0))
    except (TypeError, ValueError):
        revision = 0
    return generation_id, revision


# LLM: 所有工具公开文本共享凭据/内部协议净化；None只取消显示字数限制，不取消净化。
# 函数用途: 生成安全展示文本，完整归档保留未被净化改动的原始缩进和空白。
def _public_progress_text(
    event: ToolProgressEvent,
    value: object,
    *,
    max_chars: int | None,
) -> str:
    from ...conversation.channels import INTERNAL_SIGNAL_PREFIXES, project_user_reply
    from ...tooling.mcp_client import sanitize_credentials

    text = sanitize_credentials(str(value or ""))
    if any(marker in text for marker in INTERNAL_SIGNAL_PREFIXES):
        return "（内部运行状态已省略）"
    owner_home = str(
        getattr(getattr(event.request.agent, "home_paths", None), "owner_home_dir", "") or ""
    )
    if owner_home:
        text = text.replace(owner_home, "~/.my-agent/owner")
    projected = project_user_reply(text).content
    text = text if max_chars is None and projected == text.strip() else projected
    if max_chars is None or len(text) <= max_chars:
        return text
    keep_head = max_chars * 2 // 3
    keep_tail = max_chars - keep_head
    return f"{text[:keep_head]}\n…（内容过长，已省略）…\n{text[-keep_tail:]}"


# LLM: 富展示只白名单投影公开预览；捕获原文归archive，command的预览截断与采集完整性不得混为一谈。
# 函数用途: 对diff、patch、write、command安全限长；未知handler字段不穿透Gateway/TUI。
def _public_progress_display(
    event: ToolProgressEvent,
    value: object,
) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    kind = str(value.get("kind") or "").strip().lower()
    if kind == "diff":
        rows: list[dict[str, object]] = []
        raw_rows = value.get("lines")
        if isinstance(raw_rows, list):
            for raw in raw_rows[:180]:
                if not isinstance(raw, dict):
                    continue
                row_kind = str(raw.get("kind") or "context").strip().lower()
                if row_kind not in {"header", "context", "add", "remove"}:
                    row_kind = "context"
                rows.append(
                    {
                        "kind": row_kind,
                        "old_line": _optional_progress_int(raw.get("old_line")),
                        "new_line": _optional_progress_int(raw.get("new_line")),
                        "text": _public_progress_text(event, raw.get("text"), max_chars=360),
                    }
                )
        return {
            "kind": "diff",
            "path": _public_progress_text(event, value.get("path"), max_chars=240),
            "lines_added": _nonnegative_progress_int(value.get("lines_added")),
            "lines_removed": _nonnegative_progress_int(value.get("lines_removed")),
            "lines": rows,
            "hidden_lines": _nonnegative_progress_int(value.get("hidden_lines")) + max(0, len(raw_rows) - 180 if isinstance(raw_rows, list) else 0),
        }
    if kind == "write":
        raw_lines = value.get("lines")
        lines = [
            _public_progress_text(event, line, max_chars=360)
            for line in (raw_lines[:180] if isinstance(raw_lines, list) else [])
        ]
        return {
            "kind": "write",
            "path": _public_progress_text(event, value.get("path"), max_chars=240),
            "mode": str(value.get("mode") or "overwrite")[:16],
            "bytes": _nonnegative_progress_int(value.get("bytes")),
            "binary": value.get("binary") is True,
            "total_lines": _nonnegative_progress_int(value.get("total_lines")),
            "lines": lines,
            "hidden_lines": _nonnegative_progress_int(value.get("hidden_lines")) + max(0, len(raw_lines) - 180 if isinstance(raw_lines, list) else 0),
        }
    if kind == "patch":
        raw_files = value.get("files")
        source_files = raw_files if isinstance(raw_files, list) else []
        files: list[dict[str, object]] = []
        for raw_file in source_files[:20]:
            projected = _public_progress_display(event, raw_file)
            if projected.get("kind") == "diff":
                files.append(projected)
        return {
            "kind": "patch",
            "files": files,
            "hidden_files": (
                _nonnegative_progress_int(value.get("hidden_files"))
                + max(0, len(source_files) - 20)
            ),
        }
    if kind == "command":
        return {
            "kind": "command",
            "return_code": _optional_progress_int(value.get("return_code")),
            "stdout": _public_progress_text(event, value.get("stdout"), max_chars=8_000),
            "stderr": _public_progress_text(event, value.get("stderr"), max_chars=8_000),
            "stdout_lines": _nonnegative_progress_int(value.get("stdout_lines")),
            "stderr_lines": _nonnegative_progress_int(value.get("stderr_lines")),
            "stdout_truncated": value.get("stdout_truncated") is True or len(str(value.get("stdout") or "")) > 8_000,
            "stderr_truncated": value.get("stderr_truncated") is True or len(str(value.get("stderr") or "")) > 8_000,
            "capture_complete": value.get("capture_complete") is not False,
        }
    summary = _public_progress_text(event, value.get("summary"), max_chars=800)
    return {"kind": kind[:40] or "generic", "summary": summary} if summary else {}


# LLM: display 计数只能由结构化数值进入，畸形值归零且不能通过异常打断真实工具执行。
# 函数用途: 将展示计数规范为非负整数。
def _nonnegative_progress_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: diff 的空侧行号必须保持 None，不能用 0 冒充真实文件行；其它合法数值规范为非负整数。
# 函数用途: 安全读取可选的旧/新文件行号或命令退出码。
def _optional_progress_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _finished_status(result: ToolResult) -> str:
    return "完成" if result.ok else f"失败({result.error_code or 'ERROR'})"


# LLM: 工具卡首选模型原始公共参数做摘要，不能从输出正文或宿主补全字段反推；新增工具只加无敏感值的显式参数。
# 函数用途: 从常见工具参数中挑一个短描述，让执行中和完成后的卡片都说明正在操作什么。
def _payload_progress_detail(call: ToolCall) -> str:
    payload = call.arguments
    for key in ("path", "url", "query", "artifact_ref", "root_id", "run_id", "status", "scope"):
        value = str(payload.get(key) or "").strip()
        if value:
            return _shorten(value)
    command = str(payload.get("command") or "").strip()
    if command:
        return _shorten(command)
    items = payload.get("items")
    if isinstance(items, list):
        return f"items={len(items)}"
    urls = payload.get("urls")
    if isinstance(urls, list):
        return f"urls={len(urls)}"
    return ""


def _tool_call_payload(call: ToolCall) -> dict[str, object]:
    return {
        "tool": call.tool_name,
        "call_id": call.call_id,
        **call.arguments,
    }


def _shorten(value: str, limit: int = 100) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def _deferred_orchestration_result(call: ToolCall) -> ToolResult:
    return ToolResult.failed(
        call,
        "同一轮已经执行过会创建或改变子代理树的工具调用，"
        "后续编排工具已延后。请先读取上一条工具的真实输出，"
        "下一轮先读取上一条创建回执；已创建的下级会自动运行，不要重复创建或催跑。",
        error_code="ORCHESTRATION_CALL_DEFERRED",
        failure_stage="runtime_gate",
    )


def _synthetic_execution(
    call: ToolCall,
    result: ToolResult,
    reason_code: str,
) -> ToolExecution:
    return ToolExecution(
        call=call,
        decision=ActionDecision("deny", (reason_code,), {"failure_stage": "runtime_gate"}),
        result=result,
        states=("received", "normalized", "failed", "persisted", "projected"),
    )


def _should_defer_orchestration(stateful_orchestration_seen: bool, tool_name: str) -> bool:
    return (
        stateful_orchestration_seen
        and tool_name in _DEPENDENT_ORCHESTRATION_TOOLS
        and tool_name != "create_subagents"
    )
