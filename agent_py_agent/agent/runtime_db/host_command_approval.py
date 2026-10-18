# LLM: 原审批纯退休未结只同对象传播和核收；普通同码不升级，物理未知优先，不以清理完成签发终态或释放资源。
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
# LLM: 实际运输或纯退休未结必须保同对象，不形成普通拒绝回执；原handler权限与取消/截止不变。
# 函数用途: 消费原确认并将未结交原执行Future，明确拒绝才收口原工具。
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
    except Exception as error:  # noqa: BLE001 普通运输失败仍原未启动；实际未结交原Future而非覆盖为拒绝
        from ..conversation.agent_tool_approval import capture_approval_retirement_call
        from ..gateway_parts.private_approval_control import raise_private_approval_transport_error

        capture_approval_retirement_call(error, request, execution)
        raise_private_approval_transport_error(error)
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
