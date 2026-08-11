from __future__ import annotations

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import ClassVar, Literal

from ...backends import ModelResponse
from ...concurrency.interrupt import is_interrupted, register_interrupt_callback
from ...contracts.required_actions import required_action_assessment_failed
from ...conversation.authority import conversation_transcript_is_authoritative
from ...memory_archive import estimate_tokens
from ...tooling.action_policy import ActionDecision
from ...tooling.concurrency import concurrency_conflicts, describe_tool_concurrency
from ...tooling.executor import ToolExecution
from ...tooling.runtime_contracts import ToolCall, ToolFailureFacts, ToolResult
from .._runtime_params import ToolLoopExecuteParams
from ..model.context_pressure import should_compact_before_more_tool_output
from ..runtime.context_compactor import runtime_compact_policy
from ..runtime.live_archive import archive_assistant_tool_round_if_enabled
from ..tool_context.call_reducer import (
    AssistantToolRoundContextRequest,
    render_assistant_tool_round_context,
)
from .round_subagent_output import (
    SubagentOutputWriteCheck,
    is_subagent_output_json_write,
    subagent_output_json_response,
)

_STATEFUL_ORCHESTRATION_TOOLS = {
    "create_subagents",
    "schedule_child_subagents",
}
_DEPENDENT_ORCHESTRATION_TOOLS = {
    "create_subagents",
    "dispatch_subagents",
    "inspect_agent_tree",
    "schedule_child_subagents",
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


@dataclass(frozen=True)
class ToolCallRecordParams:
    __test__: ClassVar[bool] = False

    params: ToolLoopExecuteParams
    tool_rounds: int
    idx: int
    call: ToolCall
    result: ToolResult
    execution_states: tuple[str, ...] = ()

    @property
    def payload(self) -> dict[str, object]:
        """Read-only projection for adjacent reporters; execution authority is ``call``."""

        return _tool_call_payload(self.call)


@dataclass(frozen=True)
class ToolCallExecuteParams:
    __test__: ClassVar[bool] = False

    params: ToolLoopExecuteParams
    tool_rounds: int
    idx: int
    call: ToolCall


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


# LLM: 本入口按 ToolRuntimePolicy 划分只读并发段和顺序屏障；无论实际完成顺序如何，记录顺序始终与 provider 调用顺序一致。
# 函数用途: 执行一轮 canonical 工具调用，并为执行、拒绝、延后与取消都写入配对 ToolResult。
def execute_tool_round(request: ToolRoundExecutionRequest) -> bool:
    before_context_count = len(getattr(request.params, "tool_context", []) or [])
    _append_assistant_tool_round_context(request)
    calls = [
        _bound_conversation_workspace_call(request.agent, call)
        for call in _calls_for_this_execution_round(request)
    ]
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
        parallel_end = _parallel_segment_end(request, calls, position)
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
    _record_round_limit_deferred_calls(request, processed_count=len(calls))
    if len(calls) < len(request.calls) and not progress.deferred_reason:
        progress.deferred_reason = "达到宿主设置的单轮工具调用上限"
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


# 函数用途: informational 轮对模型提出的全部调用做有界结构化拦截——handler 不执行、
# 每调用一条拦截结果(模型可读),连续 _NO_ACTION_GATE_HALT_LIMIT 轮拦截后设
# no_action_gate_halt,由 _tool_step_or_limit 收口轮接管(剥工具调用,等用户明确指示)。
def _gate_all_calls_for_no_action(
    request: ToolRoundExecutionRequest,
    calls: list[ToolCall],
) -> bool:
    from .._tool_loop_service import _NO_ACTION_GATE_HALT_LIMIT, _NO_ACTION_GATE_STREAK_ATTR

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
            ToolCallExecuteParams(request.params, request.tool_rounds, idx, call)
        )
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


@dataclass(frozen=True)
class _ParallelThreadContext:
    transient_values: tuple[tuple[str, object], ...]
    subagent_run_id: str
    subagent_attempt_id: str
    task_attributes: dict[str, object] | None


def _parallel_segment_end(
    request: ToolRoundExecutionRequest,
    calls: list[ToolCall],
    start: int,
) -> int:
    descriptors = []
    position = start
    while position < len(calls) and position - start < _MAX_PARALLEL_TOOL_CALLS:
        call = calls[position]
        if _should_defer_for_compact(request, call.tool_name):
            break
        descriptor = describe_tool_concurrency(
            getattr(request.params, "tool_runtime_snapshot", None),
            call,
        )
        if not descriptor.parallel_eligible:
            break
        if any(concurrency_conflicts(descriptor, prior) for prior in descriptors):
            break
        descriptors.append(descriptor)
        position += 1
    return position


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
        (idx, execution.call, started_at, execution)
        for (idx, _call, started_at), execution in zip(
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
        from ..runner.context import set_current_subagent_context

        previous_runner = set_current_subagent_context(
            request.agent,
            run_id=context.subagent_run_id,
            attempt_id=context.subagent_attempt_id,
            task_attributes=context.task_attributes,
        )
    try:
        return request.execute_one(
            ToolCallExecuteParams(request.params, request.tool_rounds, idx, call)
        )
    finally:
        if previous_runner is not None:
            from ..runner.context import restore_current_subagent_context

            restore_current_subagent_context(request.agent, previous_runner)
        _restore_parallel_thread_context(request.agent, restore_values)


def _capture_parallel_thread_context(agent: object) -> _ParallelThreadContext:
    from ..runner.context import (
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
        )
    )
    wrote_output = is_subagent_output_json_write(
        SubagentOutputWriteCheck(
            request.agent,
            request.params,
            _tool_call_payload(call),
            result,
        )
    )
    return wrote_output, _runtime_transition_after_tool(result)


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


def _bound_conversation_workspace_call(agent: object, call: ToolCall) -> ToolCall:
    """统一改写绑定前 prompt 遗留的占位目录，避免账本续上而产物另起目录。"""
    from ...conversation.task_promotion import rebase_bound_conversation_workspace_params

    projected = {"tool": call.tool_name, **call.arguments}
    rebased = rebase_bound_conversation_workspace_params(agent, projected)
    if not isinstance(rebased, dict):
        return call
    arguments = {
        key: value for key, value in rebased.items() if key not in {"tool", "tool_name", "call_id"}
    }
    return replace(call, arguments=arguments)


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
            )
        )


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
_MAX_PARALLEL_TOOL_CALLS = 8


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


def _record_round_limit_deferred_calls(
    request: ToolRoundExecutionRequest,
    *,
    processed_count: int,
) -> None:
    if processed_count >= len(request.calls):
        return
    # Keep provider order intact. The helper uses ``start_idx`` as both the
    # one-based record index and slice point, so passing an already-sliced tail
    # would slice twice and silently leave calls without terminal results.
    calls = [_bound_conversation_workspace_call(request.agent, call) for call in request.calls]
    _record_unstarted_calls(
        request,
        calls,
        start_idx=processed_count + 1,
        result_factory=_tool_call_limit_deferred_result,
        phase="deferred",
        status="超过本轮上限",
    )


def _tool_call_limit_deferred_result(call: ToolCall) -> ToolResult:
    return ToolResult.failed(
        call,
        "TOOL_CALL_LIMIT_DEFERRED: 本轮调用数量超过宿主上限；本调用没有执行，请在下一模型轮按最新事实重新发起。",
        error_code="TOOL_CALL_LIMIT_DEFERRED",
        failure_stage="runtime_gate",
    )


def _calls_for_this_execution_round(
    request: ToolRoundExecutionRequest,
) -> list[ToolCall]:
    limit = _max_tool_calls_per_round(request)
    if limit <= 0 or len(request.calls) <= limit:
        return request.calls
    return request.calls[:limit]


def _max_tool_calls_per_round(request: ToolRoundExecutionRequest) -> int:
    value = _task_attribute_int(request, "max_tool_calls_per_round")
    if value is None:
        value = _agent_config_int(request.agent, "max_tool_calls_per_round")
    if value is None or value <= 0:
        return 0
    return value


def _task_attribute_int(request: ToolRoundExecutionRequest, key: str) -> int | None:
    attrs = getattr(request.params, "task_attributes", None)
    if not isinstance(attrs, dict) or key not in attrs:
        return None
    return _positiveish_int(attrs.get(key))


def _agent_config_int(agent: object, key: str) -> int | None:
    config = getattr(agent, "config", None)
    if config is None or not hasattr(config, key):
        return None
    return _positiveish_int(getattr(config, key))


def _positiveish_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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


def _conversation_owns_compaction(params: ToolLoopExecuteParams) -> bool:
    return bool(
        str(getattr(params, "context_scope", "") or "") == "conversation"
        and conversation_transcript_is_authoritative(params.task_attributes)
    )


def _persistent_compact_enabled(agent: object, params: object) -> bool:
    save = getattr(params, "save", None)
    if save is not None:
        return bool(save)
    return bool(getattr(getattr(agent, "config", None), "auto_save_memory", True))


def _emit_tool_progress(event: ToolProgressEvent) -> None:
    on_chunk = getattr(event.request.params, "effective_on_chunk", None)
    if not callable(on_chunk):
        return
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
        progress_writer(_structured_tool_progress(event, tool_name, detail), legacy_text)
        return
    try:
        on_chunk(legacy_text)
    except Exception:
        return


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
        output = _public_progress_text(event, event.result.output, max_chars=1600)
        if output:
            payload["output"] = output
    if event.started_at is not None:
        payload["elapsed_seconds"] = round(
            max(0.0, time.monotonic() - event.started_at),
            3,
        )
    return payload


def _public_progress_text(
    event: ToolProgressEvent,
    value: object,
    *,
    max_chars: int,
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
    text = project_user_reply(text).content
    if len(text) <= max_chars:
        return text
    keep_head = max_chars * 2 // 3
    keep_tail = max_chars - keep_head
    return f"{text[:keep_head]}\n…（内容过长，已省略）…\n{text[-keep_tail:]}"


def _finished_status(result: ToolResult) -> str:
    return "完成" if result.ok else f"失败({result.error_code or 'ERROR'})"


def _payload_progress_detail(call: ToolCall) -> str:
    payload = call.arguments
    for key in ("path", "artifact_ref", "root_id", "run_id", "status", "scope"):
        value = str(payload.get(key) or "").strip()
        if value:
            return _shorten(value)
    command = str(payload.get("command") or "").strip()
    if command:
        return _shorten(command)
    items = payload.get("items")
    if isinstance(items, list):
        return f"items={len(items)}"
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
        "下一轮再使用返回的 created_run_ids/actionable_run_ids 调用 dispatch_subagents。",
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
