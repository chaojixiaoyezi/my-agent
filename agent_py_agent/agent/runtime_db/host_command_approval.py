# LLM: 宿主命令只复用原 ToolApproval 合同与 ToolExecutor；等待期间不更换 call、handler、快照或执行归属。
# 模块用途: 将明确命令的审批决定接回原调用，拒绝、取消和无消费者都不执行业务工具。

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import replace

from ..contracts.tool_approval import (
    ToolApprovalDecision,
    approval_deadline,
    build_tool_approval_request,
)
from ..tooling.action_policy import ActionDecision
from ..tooling.executor import ToolExecution, ToolExecutor, ToolExecutorRequest
from ..tooling.runtime_contracts import ToolFailureFacts, ToolResult


# LLM: consumer 与可调期限由已鉴权宿主注入，有限正整数冻结原请求 expires_at；无人接收/非法答复写未启动终态，取消优先。
# 函数用途: 消费一次有界确认，明确拒绝后不再保留可重询 APPROVAL_REQUIRED，批准后重核原执行器全部权限。
def resolve_host_command_approval(prepared: ToolExecutorRequest, execution: ToolExecution, *,
                                  request_id: str, consumer: Callable | None, timeout_seconds: int | None = None) -> ToolExecution:
    if execution.decision.status != "ask":
        return execution
    request = build_tool_approval_request(execution.call, request_id=request_id, round_number=0,
                                         call_index=1, description=execution.call.tool_name)
    if timeout_seconds is not None:
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            return _rejected_execution(execution, ToolApprovalDecision(request.permission_id, "unavailable",
                reason_code="APPROVAL_UNAVAILABLE"))
        request = replace(request, expires_at=request.requested_at + timeout_seconds)
    request = replace(request, options=tuple(option for option in request.options
                                             if option["decision"] not in {"approved_session", "approved_owner"}))
    try:
        raw = (consumer(request.to_dict(), cancellation_token=prepared.cancellation_token)
               if callable(consumer) else ToolApprovalDecision(request.permission_id, "unavailable",
                   reason_code="APPROVAL_UNAVAILABLE"))
        decision = (raw if isinstance(raw, ToolApprovalDecision) else ToolApprovalDecision.from_mapping(raw)
                    if isinstance(raw, Mapping) else ToolApprovalDecision(request.permission_id, "unavailable"))
    except Exception:  # noqa: BLE001 审批运输失败发生在 handler 前，只保留未启动，不能猜批准或执行
        decision = ToolApprovalDecision(request.permission_id, "unavailable")
    if prepared.cancellation_token is not None and prepared.cancellation_token.cancelled:
        decision = ToolApprovalDecision(request.permission_id, "cancelled")
    elif time.time() >= approval_deadline(request):
        decision = ToolApprovalDecision(request.permission_id, "denied", reason_code="APPROVAL_TIMEOUT")
    if decision.permission_id != request.permission_id or decision.decision in {"approved_session", "approved_owner"}:
        decision = ToolApprovalDecision(request.permission_id, "unavailable", reason_code="APPROVAL_UNAVAILABLE")
    if decision.decision == "unavailable" and not decision.reason_code:
        decision = replace(decision, reason_code="APPROVAL_UNAVAILABLE")
    if decision.approved:
        boundary = dict(prepared.write_boundary or {})
        boundary["approved_actions"] = [request.approved_binding(decision)]
        return ToolExecutor().execute(replace(prepared, write_boundary=boundary))
    return _rejected_execution(execution, decision)


# LLM: 原决定保留 reason_code 与精确 permission_id；失败明确 handler_executed=false，拒绝与无人审批均不写伪造工具操作。
# 函数用途: 让原 HostCommand 收口未启动的拒绝或取消，后续同请求只重放该结论。
def _rejected_execution(execution: ToolExecution, decision: ToolApprovalDecision) -> ToolExecution:
    cancelled = decision.decision == "cancelled"
    code = decision.reason_code or ("CANCELLED" if cancelled else "APPROVAL_REJECTED")
    policy = ActionDecision("deny", (code,), {"failure_stage": "authorization",
                            "approval_id": decision.permission_id, "approval_decision": decision.decision},
                            resolved_effect=execution.decision.resolved_effect)
    result = ToolResult.failed(execution.call, "本次调用已取消。" if cancelled else "本次调用已拒绝。",
                               error_code=code, failure_stage="authorization",
                               facts=ToolFailureFacts(status="cancelled" if cancelled else "failed",
                                   metadata={"action_decision": policy.to_dict(), "approval_decision": decision.to_dict()}))
    return replace(execution, decision=policy, result=result)
