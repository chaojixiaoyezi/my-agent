# LLM: 宿主命令只复用原 ToolApproval 合同与 ToolExecutor；等待期间不更换 call、handler、快照或执行归属。
# 模块用途: 将明确命令的审批决定接回原调用，拒绝、取消和无消费者都不执行业务工具。

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace

from ..contracts.tool_approval import ToolApprovalDecision, build_tool_approval_request
from ..tooling.action_policy import ActionDecision
from ..tooling.executor import ToolExecution, ToolExecutor, ToolExecutorRequest
from ..tooling.runtime_contracts import ToolFailureFacts, ToolResult


# LLM: consumer 由已鉴权运输注入，只接收公开请求；无会话缓存，唯一批准 binding 只作用于同一执行请求。
# 函数用途: 等待一次明确批准，然后让原执行器重新检查全部权限、取消及固定 handler 的准入。
def resolve_host_command_approval(prepared: ToolExecutorRequest, execution: ToolExecution, *,
                                  request_id: str, consumer: Callable | None) -> ToolExecution:
    if execution.decision.status != "ask" or consumer is None:
        return execution
    request = build_tool_approval_request(execution.call, request_id=request_id, round_number=0,
                                         call_index=1, description=execution.call.tool_name)
    request = replace(request, options=tuple(option for option in request.options
                                             if option["decision"] != "approved_session"))
    try:
        raw = consumer(request.to_dict(), cancellation_token=prepared.cancellation_token)
        decision = (raw if isinstance(raw, ToolApprovalDecision) else ToolApprovalDecision.from_mapping(raw)
                    if isinstance(raw, Mapping) else ToolApprovalDecision(request.permission_id, "unavailable"))
    except Exception:  # noqa: BLE001 审批运输失败发生在 handler 前，只保留未启动，不能猜批准或执行
        decision = ToolApprovalDecision(request.permission_id, "unavailable")
    if prepared.cancellation_token is not None and prepared.cancellation_token.cancelled:
        decision = ToolApprovalDecision(request.permission_id, "cancelled")
    if decision.permission_id != request.permission_id or decision.decision not in {"approved", "denied", "cancelled"}:
        return execution
    if decision.approved:
        boundary = dict(prepared.write_boundary or {})
        boundary["approved_actions"] = [request.approved_binding(decision)]
        return ToolExecutor().execute(replace(prepared, write_boundary=boundary))
    return _rejected_execution(execution, decision)


# LLM: 拒绝来自精确 permission_id 的结构化决定；失败结果明确 handler_executed=false，不写伪造工具操作。
# 函数用途: 让原 HostCommand 收口未启动的拒绝或取消，后续同请求只重放该结论。
def _rejected_execution(execution: ToolExecution, decision: ToolApprovalDecision) -> ToolExecution:
    cancelled = decision.decision == "cancelled"
    code = "CANCELLED" if cancelled else "APPROVAL_REJECTED"
    policy = ActionDecision("deny", (code,), {"failure_stage": "authorization",
                            "approval_id": decision.permission_id, "approval_decision": decision.decision},
                            resolved_effect=execution.decision.resolved_effect)
    result = ToolResult.failed(execution.call, "本次调用已取消。" if cancelled else "本次调用已拒绝。",
                               error_code=code, failure_stage="authorization",
                               facts=ToolFailureFacts(status="cancelled" if cancelled else "failed",
                                   metadata={"action_decision": policy.to_dict(), "approval_decision": decision.to_dict()}))
    return replace(execution, decision=policy, result=result)
