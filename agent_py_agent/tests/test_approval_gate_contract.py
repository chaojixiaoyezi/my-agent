from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def test_approval_gate_requires_approval_for_dangerous_action():
    from agent_py_agent.agent.contracts.approval_gate import (
        ApprovalGatePolicy,
        ApprovalRequest,
        evaluate_approval_gate,
    )

    decision = evaluate_approval_gate(
        ApprovalRequest(action="block_ip", args_hash="h1", run_id="run-1"),
        approvals=(),
        policy=ApprovalGatePolicy(dangerous_actions=("block_ip",)),
    )

    assert decision.allowed is False
    assert decision.status == "WAITING_HUMAN"
    assert decision.error_code == "APPROVAL_REQUIRED"
    payload = decision.to_dict()
    assert payload["recovery"]["status"] == "needs_user_input"
    assert payload["recovery"]["actions"][0]["message_zh"]


def test_approval_gate_allows_exact_approved_action_binding():
    from agent_py_agent.agent.contracts.approval_gate import (
        ApprovalGatePolicy,
        ApprovalRecord,
        ApprovalRequest,
        evaluate_approval_gate,
    )

    decision = evaluate_approval_gate(
        ApprovalRequest(action="block_ip", args_hash="h1", run_id="run-1", approval_id="ap-1"),
        approvals=(
            ApprovalRecord(
                approval_id="ap-1",
                action="block_ip",
                args_hash="h1",
                run_id="run-1",
                approver="admin",
                status="APPROVED",
            ),
        ),
        policy=ApprovalGatePolicy(dangerous_actions=("block_ip",), allowed_approvers=("admin",)),
    )

    assert decision.allowed is True
    assert decision.status == "APPROVED"
    assert decision.error_code == ""


def test_approval_gate_rejects_action_if_bound_args_change():
    from agent_py_agent.agent.contracts.approval_gate import (
        ApprovalGatePolicy,
        ApprovalRecord,
        ApprovalRequest,
        evaluate_approval_gate,
    )

    decision = evaluate_approval_gate(
        ApprovalRequest(action="block_ip", args_hash="changed", run_id="run-1", approval_id="ap-1"),
        approvals=(
            ApprovalRecord(
                approval_id="ap-1",
                action="block_ip",
                args_hash="original",
                run_id="run-1",
                approver="admin",
                status="APPROVED",
            ),
        ),
        policy=ApprovalGatePolicy(dangerous_actions=("block_ip",), allowed_approvers=("admin",)),
    )

    assert decision.allowed is False
    assert decision.status == "BLOCKED"
    assert decision.error_code == "APPROVAL_BINDING_MISMATCH"
    payload = decision.to_dict()
    assert payload["recovery"]["status"] == "needs_user_input"
    assert payload["recovery"]["actions"][0]["recommended_action"] == "request_user_input_or_approval"


def test_approval_gate_rejects_denied_expired_unauthorized_and_replayed_records():
    from agent_py_agent.agent.contracts.approval_gate import (
        ApprovalGatePolicy,
        ApprovalRecord,
        ApprovalRequest,
        evaluate_approval_gate,
    )

    now = datetime(2026, 5, 21, tzinfo=UTC)
    base = ApprovalRequest(action="block_ip", args_hash="h1", run_id="run-1", approval_id="ap-1")
    policy = ApprovalGatePolicy(dangerous_actions=("block_ip",), allowed_approvers=("admin",), now=now)

    denied = ApprovalRecord("ap-1", "block_ip", "h1", "run-1", "admin", "REJECTED")
    expired = ApprovalRecord(
        "ap-1",
        "block_ip",
        "h1",
        "run-1",
        "admin",
        "APPROVED",
        expires_at=now - timedelta(seconds=1),
    )
    unauthorized = ApprovalRecord("ap-1", "block_ip", "h1", "run-1", "guest", "APPROVED")
    replayed = ApprovalRecord("ap-1", "block_ip", "h1", "run-1", "admin", "APPROVED", used=True)

    assert evaluate_approval_gate(base, approvals=(denied,), policy=policy).error_code == "APPROVAL_REJECTED"
    assert evaluate_approval_gate(base, approvals=(expired,), policy=policy).error_code == "APPROVAL_EXPIRED"
    assert (
        evaluate_approval_gate(base, approvals=(unauthorized,), policy=policy).error_code
        == "APPROVER_NOT_AUTHORIZED"
    )
    assert evaluate_approval_gate(base, approvals=(replayed,), policy=policy).error_code == "APPROVAL_ALREADY_USED"
