
from __future__ import annotations

"""LLM: 所有模型工具必须经过同一运行时准备、任务晋升、权限和审计执行缝隙。

模块用途: 在真正调用工具前冻结工作目录、活动回合和结构化运行事实，再统一记录执行结果。
"""

import json
from dataclasses import dataclass, replace
from pathlib import Path

from ..tooling.models import (
    ToolFailureStage,
    ToolHandlerOutcome,
    apply_tool_execution_facts,
)
from ..tooling.runtime_contracts import ToolCall, ToolResult
from .audit_dispatch import audit_privileged_tool_call
from .parameters import (
    _one_shot_tool_call_is_duplicate,
    _one_shot_tool_call_keys,
)
from .runner.stage_trace import RunnerToolStageTraceRequest, trace_runner_tool_call_finished
from .subagent.attempt_guard import stale_subagent_attempt_result
from .tool_guard.agent_budget_stage import (
    ToolAgentBudgetStageRequest,
    maybe_block_tool_agent_budget,
)
from .tool_loop.round_execution import ToolCallExecuteParams
from .tool_runtime_ledger import write_boundary_with_runtime_ledger


# LLM: 本请求冻结工具参数和宿主运行上下文；文件路径不选择任务身份，也不生成目录执行锁。
# 类用途: 把一次工具调用的模型原参数、执行参数与审计请求放在一起，供共享工具入口读取。
@dataclass(frozen=True)
class ToolCallRuntimeRequest:
    agent: object
    request: ToolCallExecuteParams
    call: ToolCall

    @property
    def trace_request(self) -> RunnerToolStageTraceRequest:
        return RunnerToolStageTraceRequest(
            agent=self.agent,
            params=self.request.params,
            tool_rounds=self.request.tool_rounds,
            idx=self.request.idx,
            call=self.call,
        )

    @property
    def payload(self) -> dict[str, object]:
        return {
            "tool": self.call.tool_name,
            "call_id": self.call.call_id,
            **self.call.arguments,
        }

    # LLM: Archive/provider replay retain the original call; execution has its own typed call
    # snapshot, but task changes must never rebase either snapshot's file arguments.
    # 函数用途: 取得模型最初提交的工具调用，供归档保存安全回放视图。
    @property
    def model_call(self) -> ToolCall:
        """Return the provider-authored call carried beside the executable call."""

        return self.request.model_call or self.call


def guarded_tool_call_result(runtime_request: ToolCallRuntimeRequest):
    # Runtime guards reject before Registry handlers; keep that boundary explicit for recovery and audit.
    request = runtime_request.request
    payload = runtime_request.payload
    worker_scope = _audit_source_worker_tool_scope_result(
        runtime_request.agent,
        payload,
    )
    if worker_scope is not None:
        apply_tool_execution_facts(
            worker_scope,
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )
        return worker_scope
    if _one_shot_tool_call_is_duplicate(payload, request.params.one_shot_tool_calls):
        result = apply_tool_execution_facts(
            _duplicate_one_shot_result(payload),
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )
        return result
    stale_result = stale_subagent_attempt_result(runtime_request.agent, payload)
    if stale_result is not None:
        apply_tool_execution_facts(
            stale_result,
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )
        return stale_result
    return maybe_block_tool_agent_budget(ToolAgentBudgetStageRequest(runtime_request.agent, request, payload))


def _audit_source_worker_tool_scope_result(
    agent: object,
    payload: object,
) -> ToolHandlerOutcome | None:
    """Recheck a source worker's least-privilege scope at the final tool seam."""
    if not isinstance(payload, dict):
        return None
    from ..common.audit_activation import (
        audit_worker_tool_scope,
        current_audit_attributes,
    )

    attrs = current_audit_attributes(agent)
    tool_name = str(payload.get("tool") or "").strip()
    allowed_tools = audit_worker_tool_scope(attrs)
    if not allowed_tools or tool_name in allowed_tools:
        return None
    return ToolHandlerOutcome(
        tool_name,
        False,
        "Audit 来源工作者只能使用当前结构化阶段的最小工具集。",
        error_code="TOOL_NOT_ALLOWED",
    )


# LLM: 最终 Tool Gateway 调用必须携带模型看到的同一 run 快照，不能在执行时重新扩大工具宇宙。
# 首个工作工具在权限快照前登记运行身份，供归档与停止查询使用；晋升前后 cwd 和 owner 文件范围不变。
# 函数用途: 执行并审计一个已追踪工具调用，同时维护任务晋升、幂等记录和被动验收事实。
def execute_traced_tool_call(runtime_request: ToolCallRuntimeRequest):
    from ..conversation.process_events import process_completion_target
    from .tool_call_archive_record import archive_tool_output_projection
    from .tool_loop.recovery import runtime_run_scope

    runtime_request, prepared_promotion_outcome = _prepare_traced_tool_runtime_request(
        runtime_request
    )

    def pre_handler_gate(call):
        # B 切片：外层 fence 已删除（authority_context 懒建/open/close 写路径
        # 由 OperationStore selector + ManagedOperationStore 权威门取代，见
        # tooling/executor._require_operation_authority 与
        # runtime_db/managed_operation_store.require_authority）。任务晋升已在 boundary
        # 冻结前完成；预算、重复调用和 runner 身份 guard 仍只在 canonical executor
        # 完成规范化/授权后运行一次，避免审批重入时重复扣减预算。
        prepared_request = replace(runtime_request, call=call)
        outcome = guarded_tool_call_result(prepared_request) or prepared_promotion_outcome
        if outcome is None:
            return None
        return apply_tool_execution_facts(
            outcome,
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )

    one_shot_keys = _one_shot_tool_call_keys(runtime_request.payload)
    execution = runtime_request.agent.tools.execute_tool(
        runtime_request.call,
        write_boundary=write_boundary_with_runtime_ledger(runtime_request.agent, runtime_request.request.params),
        runtime_snapshot=runtime_request.request.params.tool_runtime_snapshot,
        trusted_run_context={
            "process_completion_target": process_completion_target(runtime_request.agent, runtime_request.request.params),
            "task_attributes": dict(
                runtime_request.request.params.task_attributes
                if isinstance(runtime_request.request.params.task_attributes, dict)
                else {}
            ),
            "run_scope": runtime_run_scope(
                runtime_request.agent,
                runtime_request.request.params,
            ).to_dict(),
        },
        cancellation_token=getattr(runtime_request.request.params, "cancellation_token", None),
        required_action=_required_action_for_call(runtime_request),
        pre_handler_gate=pre_handler_gate,
        output_archiver=lambda call, outcome: archive_tool_output_projection(
            runtime_request.agent,
            runtime_request.request.params,
            call,
            outcome,
            model_call=runtime_request.model_call,
        ),
    )
    result = execution.result
    executable_payload = {
        "tool": execution.call.tool_name,
        "call_id": execution.call.call_id,
        **execution.call.arguments,
    }
    result = _record_passive_verification(runtime_request.agent, execution.call, result)
    execution = replace(execution, result=result)
    audit_privileged_tool_call(runtime_request.agent, executable_payload, result)  # 特权动作落审计(审计 #13)
    if one_shot_keys and _one_shot_result_consumes_key(result):
        runtime_request.request.params.one_shot_tool_calls.update(one_shot_keys)
    trace_runner_tool_call_finished(_finished_trace_request(runtime_request, result))
    return execution


# LLM: 工具参数是原始请求事实；任务晋升只登记运行身份，不重写路径、内容或 cwd。
# 函数用途: 在首个工作工具前建立可恢复的运行记录；停止、权限与 handler 仍走唯一执行入口。
def _prepare_traced_tool_runtime_request(
    runtime_request: ToolCallRuntimeRequest,
) -> tuple[ToolCallRuntimeRequest, ToolHandlerOutcome | None]:
    token = getattr(runtime_request.request.params, "cancellation_token", None)
    cancelled = bool(token and getattr(token, "cancelled", False))
    promotion_outcome = (
        None
        if cancelled
        else _promote_conversation_task_for_work_tool(runtime_request)
    )
    return runtime_request, promotion_outcome


def _required_action_for_call(runtime_request: ToolCallRuntimeRequest) -> object | None:
    action_id = runtime_request.call.required_action_id
    snapshot = getattr(runtime_request.request.params, "effective_contract_snapshot", None)
    for action in tuple(getattr(snapshot, "required_actions", ()) or ()):
        if str(getattr(action, "action_id", "") or "") == action_id:
            return action
    return None


def _record_passive_verification(
    agent: object,
    call: ToolCall,
    result: ToolResult,
) -> ToolResult:
    """Record advisory verification facts at the one shared tool seam.

    The local import keeps the generic tool runtime independent from the
    owner-scoped persistence package during module initialization.
    """

    from ..verification.runtime import record_tool_verification

    return record_tool_verification(agent, call, result)


# LLM: Promotion is one active-turn mutation. Gateway callers must hold their exact turn
# transition around the whole bind; local callers without that host capability execute directly.
# 函数用途: 首个工作工具把普通对话晋升为持久任务；若用户已经停止则不产生任何续接任务。
def _promote_conversation_task_for_work_tool(
    runtime_request: ToolCallRuntimeRequest,
) -> ToolHandlerOutcome | None:
    """在工具网关唯一执行缝隙按 ToolRuntimePolicy 晋升任务。"""
    tool_name = str(runtime_request.payload.get("tool") or "").strip()
    snapshot = runtime_request.request.params.tool_runtime_snapshot
    runtime = snapshot.runtime(tool_name) if snapshot is not None else None
    if runtime is None or runtime.runtime_policy.promotes_task is not True:
        return None
    from ..tooling._persona_write_guard import _persona_runtime_redirect_error

    handler_root = getattr(runtime.handler, "workspace_root", None)
    persona_error = _persona_runtime_redirect_error(
        runtime_request.agent,
        runtime_request.payload,
        workspace_root=(
            Path(handler_root).expanduser().resolve(strict=False)
            if handler_root
            else None
        ),
    )
    if persona_error:
        return ToolHandlerOutcome(
            tool_name,
            False,
            persona_error,
            error_code="PERSONA_WRITE_REQUIRES_TOOL",
        )
    current = getattr(runtime_request.agent, "_current_run_params", None)
    attrs = getattr(current, "task_attributes", None) if current is not None else None
    conversation_thread_id = (
        str(attrs.get("conversation_thread_id") or "").strip()
        if isinstance(attrs, dict)
        else ""
    )
    from ..conversation.task_promotion import promote_current_conversation_task

    # Gateway 的 active-turn T 锁必须覆盖「检查仍在运行 -> 建立/续接 task link ->
    # 把绑定写回热请求」整个晋升事务。否则 /stop 可在早期 cancellation 检查之后
    # 把原任务置 interrupted，而本线程又紧接着建立一个 -continue- active link，
    # 让已停止的主代理重新显示 Working。普通本地运行没有该 callback，保持原路径。
    transition = getattr(
        runtime_request.request.params,
        "active_turn_transition_callback",
        None,
    )
    try:
        promoted = (
            transition(
                "task_promotion",
                lambda: promote_current_conversation_task(runtime_request.agent),
            )
            if callable(transition)
            else promote_current_conversation_task(runtime_request.agent)
        )
    except InterruptedError:
        return ToolHandlerOutcome(
            tool_name or "conversation_task_binding",
            False,
            "CANCELLED: 当前回合已停止，本次工作步骤没有启动。",
            error_code="CANCELLED",
            effect_outcome="not_started",
        )
    if promoted is not None:
        _synchronize_tool_task_attributes_after_promotion(
            runtime_request.agent,
            runtime_request.request.params,
        )
        return None
    if not conversation_thread_id:
        return None
    return ToolHandlerOutcome(
        tool_name or "conversation_task_binding",
        False,
        "CONVERSATION_TASK_BINDING_FAILED: 当前执行请求无法可靠绑定到持久任务，已阻止本次工作步骤。",
        error_code="CONVERSATION_TASK_BINDING_FAILED",
    )


# LLM: The mutable outer RunParams owns conversation promotion, while ToolLoopExecuteParams is an
# immutable run snapshot that may carry a distinct task_attributes projection.  After promotion,
# replace only that projection's contents from the outer authority before cwd/boundary resolution;
# never infer a task path from model arguments or copy in the opposite direction.
# 函数用途: 同步刚登记的运行身份和归档引用，不改变工具请求的文件地址或执行目录。
def _synchronize_tool_task_attributes_after_promotion(
    agent: object,
    tool_params: object,
) -> None:
    current = getattr(agent, "_current_run_params", None)
    authoritative = getattr(current, "task_attributes", None)
    projection = getattr(tool_params, "task_attributes", None)
    if (
        not isinstance(authoritative, dict)
        or not isinstance(projection, dict)
        or projection is authoritative
    ):
        return
    projection.clear()
    projection.update(authoritative)




def _duplicate_one_shot_result(payload: dict[str, object]) -> ToolHandlerOutcome:
    tool_name = str(payload.get("tool") or "unknown")
    return ToolHandlerOutcome(
        tool_name,
        False,
        "本轮已经执行过相同的一次性编排工具调用，系统已阻止重复执行。"
        "请基于前面的工具结果直接给最终回答，不要再次调用同一个工具。",
        error_code="TOOL_ONE_SHOT_ALREADY_EXECUTED",
    )


def _finished_trace_request(runtime_request: ToolCallRuntimeRequest, result: ToolResult):
    return _finished_trace_request_from_trace(runtime_request.trace_request, result)


def _finished_trace_request_from_trace(trace_request: RunnerToolStageTraceRequest, result: ToolResult):
    return RunnerToolStageTraceRequest(
        agent=trace_request.agent,
        params=trace_request.params,
        tool_rounds=trace_request.tool_rounds,
        idx=trace_request.idx,
        call=trace_request.call,
        result=result,
    )


def _one_shot_result_consumes_key(result: ToolResult) -> bool:
    if not result.ok:
        return False
    return not _orchestration_result_is_blocked(result.output)


def _orchestration_result_is_blocked(output: object) -> bool:
    try:
        payload = json.loads(str(output or ""))
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("blocked"))
