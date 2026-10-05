
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
    tool_promotes_task,
)
from ..tooling.runtime_contracts import ToolCall, ToolResult
from .audit_dispatch import audit_privileged_tool_call
from .parameters import (
    _one_shot_tool_call_is_duplicate,
    _one_shot_tool_call_key,
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


# LLM: 运行时门在注册表 handler 之前拒绝调用，依次是审计来源工作者范围、一次性编排去重、携带记录没读全时的一次性编排
#   fail-closed、过期子代理 attempt 和工具预算；每个拒绝都标 RUNTIME_GATE 且 handler 未执行。只读结构化参数与宿主事实。
# 函数用途: 在真正执行工具前做运行时拦截，返回拦截结果；全部放行时交给预算门。
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
    if _one_shot_blocked_by_incomplete_carry(request.params, payload):
        return apply_tool_execution_facts(
            _incomplete_carry_one_shot_result(payload),
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )
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


# LLM: 最终 Tool Gateway 携带同一 run 快照、宿主审批消费者存在事实和事件身份；interactive/actor 不从模型参数读，不能扩大工具宇宙。
# 首个工作工具在权限快照前登记运行身份，供归档与停止查询使用；晋升前后 cwd 和 owner 文件范围不变。
# 函数用途: 执行并审计一个已追踪工具调用，同时维护任务晋升、幂等记录和被动验收事实。
def execute_traced_tool_call(runtime_request: ToolCallRuntimeRequest):
    from ..conversation.input_media import model_accepts_images
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
            # 能力包 v2 块 3：本 run 第一次改工作区之前记基线（开关关着时零开销）。
            _pack_verification_hooks().capture_baseline_before_tool(
                runtime_request.agent, runtime_request.request.params, call.tool_name)
            return None
        return apply_tool_execution_facts(
            outcome,
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )

    one_shot_keys = _one_shot_tool_call_keys(runtime_request.payload)
    run_scope = runtime_run_scope(runtime_request.agent, runtime_request.request.params)
    execution = runtime_request.agent.tools.execute_tool(
        runtime_request.call,
        write_boundary=write_boundary_with_runtime_ledger(runtime_request.agent, runtime_request.request.params),
        runtime_snapshot=runtime_request.request.params.tool_runtime_snapshot,
        trusted_run_context={
            "interactive": _interactive_approvals(runtime_request),
            "process_completion_target": process_completion_target(runtime_request.agent, runtime_request.request.params),
            "task_attributes": dict(
                runtime_request.request.params.task_attributes
                if isinstance(runtime_request.request.params.task_attributes, dict)
                else {}
            ),
            "run_scope": run_scope.to_dict(),
            "plugin_event_context": _tool_event_context(runtime_request, run_scope),
            # 观察截图意愿（J16 第 9 节 vision2）：宿主按当前模型档案的结构化模态决定，经内部参数传给观察工具，
            # 由绑定层转成 _meta 协商字段；只读 model_input_modalities，不按模型名猜。
            "observation_screenshot": model_accepts_images(runtime_request.agent),
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
    result = _pack_verification_hooks().attach_post_write_verification(
        runtime_request.agent, runtime_request.request.params, result)
    execution = replace(execution, result=result)
    audit_privileged_tool_call(runtime_request.agent, executable_payload, result)  # 特权动作落审计(审计 #13)
    if one_shot_keys and _one_shot_result_consumes_key(result):
        runtime_request.request.params.one_shot_tool_calls.update(one_shot_keys)
    trace_runner_tool_call_finished(_finished_trace_request(runtime_request, result))
    return execution


# LLM: 9b 必须修 2：interactive 是"这次调用有没有人能当场审批"的宿主事实，不能拿"写入器碰巧有 request_permission 方法"
#   当判据——Gateway 的流写入器总有这个方法，非管理员 IM 与没声明 tool_approval 的客户端 interactive_approvals=False，
#   后台 sink 也总有这个方法。拿方法当判据会让插件收到 interactive=true、自己选 ask，到审批时才变成无法审批；
#   设计 8.5 承诺的是插件能看到"不可交互"、可以自己选 deny。所以先读 on_chunk 上 bool 型的 interactive_approvals；
#   没有这个属性（后台 sink 要在等待期才知道有没有消费者）才沿用原 callable 判断，由结算时投影纠正。
# 函数用途: 从消费者结构化能力判断这次调用到底能不能当场收集审批意见。
def _interactive_approvals(runtime_request: ToolCallRuntimeRequest) -> bool:
    on_chunk = getattr(runtime_request.request.params, "effective_on_chunk", None)
    declared = getattr(on_chunk, "interactive_approvals", None)
    if isinstance(declared, bool):
        return declared
    return callable(getattr(on_chunk, "request_permission", None))


# LLM: 可选观察装配独立隔离；关闭零路由读取/导入/发布。启用后先备诊断再导入 Gateway，导入/装配故障均仅固定原因。
# 只读宿主不可变 actor 与 RunScope，沿 Gateway 唯一发布口装配；同步检查 Registry 接缝与 test_plugin_event_runtime。
# 函数用途: 安全传递工具观察身份，启用却装配失败时提示一次；观察不可用不改变原工具权限和执行。
def _tool_event_context(runtime_request: ToolCallRuntimeRequest, scope):
    warn = None
    try:
        agent = runtime_request.agent
        if getattr(getattr(agent, "config", None), "plugin_events_enabled", False) is not True:
            return None
        from ..plugin_events.points import warn_event_assembly_failure

        warn = warn_event_assembly_failure
        from ..gateway_parts.event_points import gateway_event_context

        request = runtime_request.request
        attrs = request.params.task_attributes or {}
        actor = "subagent" if scope.agent_kind in {"subagent", "child_agent", "grandchild_agent"} else "main"
        if request.actor == "decision":
            actor = "decision"
        return gateway_event_context(agent, {"actor": actor, "thread_id": scope.session_id,
            "channel": attrs.get("plugin_event_channel") or "local"})
    except Exception:  # noqa: BLE001 只隔离可选观察，权限、核验与 handler 异常不在此范围
        if warn is not None:
            warn('PLUGIN_EVENT_TOOL_CONTEXT_FAILED')
        return None


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


# LLM: 局部导入，和 _record_passive_verification 一样让通用工具运行时不在模块初始化时依赖能力包子系统。
# 函数用途: 返回能力包宿主核验的工具钩子模块（执行前记基线、写后核验）。
def _pack_verification_hooks():
    from ..capability import pack_verification_hooks

    return pack_verification_hooks


# LLM: 晋升条件仍只读原 ToolRuntimePolicy；实际晋升与参数同步复用 conversation.task_promotion 的活动回合事务，和宿主准备共用原身份。
# 函数用途: 首个实际工作动作沿同一受保护事务绑定持久任务；检索不晋升，停止后不重建任务或反向覆盖权威参数。
def _promote_conversation_task_for_work_tool(
    runtime_request: ToolCallRuntimeRequest,
) -> ToolHandlerOutcome | None:
    """在工具网关唯一执行缝隙按 ToolRuntimePolicy 晋升任务。"""
    tool_name = str(runtime_request.payload.get("tool") or "").strip()
    snapshot = runtime_request.request.params.tool_runtime_snapshot
    runtime = snapshot.runtime(tool_name) if snapshot is not None else None
    if runtime is None or not tool_promotes_task(runtime.runtime_policy, runtime_request.call.arguments):
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
    from ..conversation.task_promotion import promote_conversation_task_for_run

    try:
        promoted = promote_conversation_task_for_run(runtime_request.agent, runtime_request.request.params)
    except InterruptedError:
        return ToolHandlerOutcome(
            tool_name or "conversation_task_binding",
            False,
            "CANCELLED: 当前回合已停止，本次工作步骤没有启动。",
            error_code="CANCELLED",
            effect_outcome="not_started",
        )
    if promoted is not None:
        return None
    if not conversation_thread_id:
        return None
    return ToolHandlerOutcome(
        tool_name or "conversation_task_binding",
        False,
        "CONVERSATION_TASK_BINDING_FAILED: 当前执行请求无法可靠绑定到持久任务，已阻止本次工作步骤。",
        error_code="CONVERSATION_TASK_BINDING_FAILED",
    )



# LLM: 生命周期续跑的携带记录没读全（task_attributes 里的结构化不完整事实）时，一次性编排去重 fail-closed：只要有一个
#   待建子代理没有有效的 replacement_for_run_ids，这次 create_subagents 就拦下，不把读不到的前台副作用当成没发生；
#   “有效”直接复用创建边界的口径（create_payload.effective_replacement_ids_per_child：item 覆盖顶层、去空白后非空），
#   不另写一套判断。全部写明接替关系的照常走原去重，非一次性工具不受影响。只读结构化字段，不看正文。
# 函数用途: 判断本次一次性编排调用是否因为本轮工具历史没读全而必须拦下。
def _one_shot_blocked_by_incomplete_carry(params: object, payload: dict[str, object]) -> bool:
    from ..conversation.authority import CONVERSATION_ACTIVE_TURN_CARRY_INCOMPLETE_ATTR
    from .orchestration.create_payload import effective_replacement_ids_per_child

    attributes = getattr(params, "task_attributes", None)
    if not isinstance(attributes, dict) or not attributes.get(CONVERSATION_ACTIVE_TURN_CARRY_INCOMPLETE_ATTR):
        return False
    if not _one_shot_tool_call_key(payload):
        return False
    return any(not ids for ids in effective_replacement_ids_per_child(payload))


# LLM: 本轮工具历史没读全时的一次性编排拦截结果；拦截只由结构化事实决定，这里的文字只是软引导。
# 函数用途: 生成因为历史读不全而拦下派工的失败结果，并说明唯一的结构化出口 replacement_for_run_ids。
def _incomplete_carry_one_shot_result(payload: dict[str, object]) -> ToolHandlerOutcome:
    tool_name = str(payload.get("tool") or "unknown")
    return ToolHandlerOutcome(
        tool_name,
        False,
        "本轮（含子代理唤醒后的续跑）的工具历史没有读全，无法证明同内容的派工没有做过，系统已拦下这次一次性编排调用。"
        "不要换个说法重复派工；确需另派子代理接替已有 run 时，在 replacement_for_run_ids 里精确写出被接替的 run_id。",
        error_code="TOOL_ONE_SHOT_HISTORY_INCOMPLETE",
    )


# LLM: 一次性编排去重的拦截结果。去重范围是整个活动回合（前台轮加子代理唤醒后的续跑片），拦截与放行只由
#   结构化去重键决定，这里的文字只是软引导；改文案时同步 test_background_active_turn_carry。
# 函数用途: 生成重复派工被拦时返回给模型的失败结果，并说明确需接替旧子代理时要在 replacement_for_run_ids 里写明。
def _duplicate_one_shot_result(payload: dict[str, object]) -> ToolHandlerOutcome:
    tool_name = str(payload.get("tool") or "unknown")
    return ToolHandlerOutcome(
        tool_name,
        False,
        "本轮（含子代理唤醒后的续跑）已经执行过相同的一次性编排工具调用，系统已阻止重复执行。"
        "请基于前面的工具结果直接给最终回答，不要再次调用同一个工具；"
        "确需另派子代理接替已有 run 时，在 replacement_for_run_ids 里精确写出被接替的 run_id。",
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
