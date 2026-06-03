
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .recovery_actions import RecoveryAction
from .recovery_envelope import RecoveryEnvelopeRequest, recovery_envelope_from_gate_payload

UTC = timezone.utc


@dataclass(frozen=True)
class ApprovalRequest:
    action: str
    args_hash: str
    run_id: str
    approval_id: str = ""
    risk_level: str = ""

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

@dataclass(frozen=True)
class ApprovalGatePolicy:
    dangerous_actions: tuple[str, ...] = ()
    allowed_approvers: tuple[str, ...] = ()
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

@dataclass(frozen=True)
class ApprovalGateDecision:
    allowed: bool
    status: str
    error_code: str = ""
    approval_id: str = ""
    recommended_action: str = ""

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "allowed": self.allowed,
            "status": self.status,
            "error_code": self.error_code,
            "approval_id": self.approval_id,
            "recommended_action": self.recommended_action,
        }
        recovery = recovery_envelope_from_gate_payload(
            RecoveryEnvelopeRequest(
                gate="approval_gate",
                status=self.status,
                allowed=self.allowed,
                findings=({"code": self.error_code},) if self.error_code else (),
                recommended_action=self.recommended_action,
                evidence={"approval_id": self.approval_id} if self.approval_id else {},
            )
        )
        if recovery is not None:
            payload["recovery"] = recovery.to_dict()
        return payload


def evaluate_approval_gate(
    request: ApprovalRequest,
    *,
    approvals: tuple[ApprovalRecord, ...],
    policy: ApprovalGatePolicy,
) -> ApprovalGateDecision:
    if _action_key(request.action) not in {_action_key(item) for item in policy.dangerous_actions}:
        return ApprovalGateDecision(True, "NOT_REQUIRED", recommended_action=RecoveryAction.EXECUTE.value)
    if not request.approval_id:
        return ApprovalGateDecision(
            False,
            "WAITING_HUMAN",
            "APPROVAL_REQUIRED",
            recommended_action=RecoveryAction.REQUEST_APPROVAL.value,
        )
    record = _matching_record(request.approval_id, approvals)
    if record is None:
        return ApprovalGateDecision(False, "WAITING_HUMAN", "APPROVAL_NOT_FOUND", request.approval_id)
    return _evaluate_record(request, record, policy)


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
    return ApprovalGateDecision(True, "APPROVED", approval_id=record.approval_id, recommended_action=RecoveryAction.EXECUTE.value)


def _matching_record(approval_id: str, approvals: tuple[ApprovalRecord, ...]) -> ApprovalRecord | None:
    for record in approvals:
        if record.approval_id == approval_id:
            return record
    return None


def _binding_matches(request: ApprovalRequest, record: ApprovalRecord) -> bool:
    return (
        _action_key(request.action) == _action_key(record.action)
        and request.args_hash == record.args_hash
        and request.run_id == record.run_id
    )


def _blocked(error_code: str, record: ApprovalRecord) -> ApprovalGateDecision:
    return ApprovalGateDecision(False, "BLOCKED", error_code, record.approval_id, RecoveryAction.REPORT_BLOCKER.value)


def _action_key(value: object) -> str:
    return str(value or "").strip().lower()


def _status(value: object) -> str:
    return str(value or "").strip().upper()


__all__ = [
    "ApprovalGateDecision",
    "ApprovalGatePolicy",
    "ApprovalRecord",
    "ApprovalRequest",
    "evaluate_approval_gate",
]
