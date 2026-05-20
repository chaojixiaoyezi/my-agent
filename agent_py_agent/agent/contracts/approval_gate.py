# LLM: Approval gate contracts bind side-effect actions to exact structured approvals.
# 模块用途: 校验高危动作是否具备可用审批记录，避免模型用自然语言声称“已审批”后直接执行。

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


# LLM: ApprovalRequest carries the exact structured action binding that needs approval.
# 类用途: 保存待审批动作、参数哈希、run_id 和审批 id，供审批门只按机器字段判断。
@dataclass(frozen=True)
class ApprovalRequest:
    action: str
    args_hash: str
    run_id: str
    approval_id: str = ""
    risk_level: str = ""

# LLM: ApprovalRecord is the structured approval fact consumed by side-effect gates.
# 类用途: 保存审批记录的动作绑定、审批人、状态、过期时间和是否已使用。
@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    action: str
    args_hash: str
    run_id: str
    approver: str
    status: str
    expires_at: datetime | None = None
    used: bool = False

# LLM: ApprovalGatePolicy keeps dangerous action and approver policy out of prompt text.
# 类用途: 配置哪些动作需要审批、哪些审批人有效，以及本次判断使用的当前时间。
@dataclass(frozen=True)
class ApprovalGatePolicy:
    dangerous_actions: tuple[str, ...] = ()
    allowed_approvers: tuple[str, ...] = ()
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

# LLM: ApprovalGateDecision is the machine-readable result checked before tool execution.
# 类用途: 返回是否允许执行、当前状态、错误码、审批 id 和下一步建议。
@dataclass(frozen=True)
class ApprovalGateDecision:
    allowed: bool
    status: str
    error_code: str = ""
    approval_id: str = ""
    recommended_action: str = ""


# LLM: evaluate_approval_gate validates dangerous actions against exact approval records.
# 函数用途: 根据 action、args_hash、run_id 和 approval_id 判断高危动作是否允许执行。
def evaluate_approval_gate(
    request: ApprovalRequest,
    *,
    approvals: tuple[ApprovalRecord, ...],
    policy: ApprovalGatePolicy,
) -> ApprovalGateDecision:
    if _action_key(request.action) not in {_action_key(item) for item in policy.dangerous_actions}:
        return ApprovalGateDecision(True, "NOT_REQUIRED", recommended_action="execute")
    if not request.approval_id:
        return ApprovalGateDecision(
            False,
            "WAITING_HUMAN",
            "APPROVAL_REQUIRED",
            recommended_action="request_approval",
        )
    record = _matching_record(request.approval_id, approvals)
    if record is None:
        return ApprovalGateDecision(False, "WAITING_HUMAN", "APPROVAL_NOT_FOUND", request.approval_id)
    return _evaluate_record(request, record, policy)


# LLM: _evaluate_record applies replay, expiry, approver, status, and binding checks.
# 函数用途: 对单条审批记录做完整结构化校验，生成允许或阻断决策。
def _evaluate_record(
    request: ApprovalRequest,
    record: ApprovalRecord,
    policy: ApprovalGatePolicy,
) -> ApprovalGateDecision:
    if _status(record.status) == "REJECTED":
        return _blocked("APPROVAL_REJECTED", record)
    if _status(record.status) != "APPROVED":
        return ApprovalGateDecision(False, "WAITING_HUMAN", "APPROVAL_PENDING", record.approval_id)
    if record.used:
        return _blocked("APPROVAL_ALREADY_USED", record)
    if record.expires_at is not None and record.expires_at <= policy.now:
        return _blocked("APPROVAL_EXPIRED", record)
    if policy.allowed_approvers and record.approver not in set(policy.allowed_approvers):
        return _blocked("APPROVER_NOT_AUTHORIZED", record)
    if not _binding_matches(request, record):
        return _blocked("APPROVAL_BINDING_MISMATCH", record)
    return ApprovalGateDecision(True, "APPROVED", approval_id=record.approval_id, recommended_action="execute")


# LLM: _matching_record selects approval facts by stable approval id only.
# 函数用途: 从审批记录集合里按 approval_id 找到唯一候选记录。
def _matching_record(approval_id: str, approvals: tuple[ApprovalRecord, ...]) -> ApprovalRecord | None:
    for record in approvals:
        if record.approval_id == approval_id:
            return record
    return None


# LLM: _binding_matches prevents approval reuse for a different action, args, or run.
# 函数用途: 校验审批记录和当前请求的动作、参数哈希、run_id 是否完全一致。
def _binding_matches(request: ApprovalRequest, record: ApprovalRecord) -> bool:
    return (
        _action_key(request.action) == _action_key(record.action)
        and request.args_hash == record.args_hash
        and request.run_id == record.run_id
    )


# LLM: _blocked converts denied approval facts into structured task decisions.
# 函数用途: 用统一错误码返回阻断结果，避免调用方靠文本猜原因。
def _blocked(error_code: str, record: ApprovalRecord) -> ApprovalGateDecision:
    return ApprovalGateDecision(False, "BLOCKED", error_code, record.approval_id, "stop_or_request_new_approval")


# LLM: _action_key normalizes action identifiers for exact structured comparison.
# 函数用途: 规范化动作名，避免大小写和首尾空白导致结构化匹配失效。
def _action_key(value: object) -> str:
    return str(value or "").strip().lower()


# LLM: _status normalizes approval status values before policy checks.
# 函数用途: 规范化审批状态，供审批门按枚举式字符串判断。
def _status(value: object) -> str:
    return str(value or "").strip().upper()


__all__ = [
    "ApprovalGateDecision",
    "ApprovalGatePolicy",
    "ApprovalRecord",
    "ApprovalRequest",
    "evaluate_approval_gate",
]
