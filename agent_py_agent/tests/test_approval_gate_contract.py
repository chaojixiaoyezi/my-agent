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
    assert payload["recovery"]["status"] == "blocked"
    assert payload["recovery"]["terminal"] is True
    assert payload["recovery"]["actions"][0]["recommended_action"] == "report_blocker"


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

    for record, expected_code in (
        (denied, "APPROVAL_REJECTED"),
        (expired, "APPROVAL_EXPIRED"),
        (unauthorized, "APPROVER_NOT_AUTHORIZED"),
        (replayed, "APPROVAL_ALREADY_USED"),
    ):
        decision = evaluate_approval_gate(base, approvals=(record,), policy=policy)
        assert decision.error_code == expected_code
        payload = decision.to_dict()
        assert payload["recovery"]["status"] == "blocked"
        assert payload["recovery"]["terminal"] is True


def test_approved_session_decision_is_treated_as_approved(runtime_snapshot_factory=None):
    """R2-7 回归: approved_session(会话级批准)必须走批准分支, 不能抛 ValueError。

    实测现场: 选"Yes always for this session"后 decision.approved 返回 False,
    走 rejected_binding 抛 ValueError → 整个请求失败(TUI 显示"任务处理失败")。
    """
    import pytest

    from agent_py_agent.agent.contracts.tool_approval import (
        ToolApprovalDecision,
        ToolApprovalRequest,
    )

    request = ToolApprovalRequest(
        permission_id="approval:test-r2-7",
        request_id="req-r2-7",
        tool_name="run_command",
        round=1,
        call_index=0,
        title="Tool use",
        description="test",
        binding={
            "tool_name": "run_command",
            "run_id": "run-r2-7",
            "operation_id": "op-r2-7",
            "idempotency_key": "ik-r2-7",
            "args_hash": "ah-r2-7",
        },
        options=(
            {
                "id": "allow_once",
                "label": "Yes",
                "decision": "approved",
                "feedback_type": "accept",
                "feedback_placeholder": "",
            },
            {
                "id": "allow_session",
                "label": "Yes, always for this session",
                "decision": "approved_session",
                "feedback_type": "accept",
                "feedback_placeholder": "",
            },
            {
                "id": "deny",
                "label": "No",
                "decision": "denied",
                "feedback_type": "reject",
                "feedback_placeholder": "",
            },
        ),
    )
    session_decision = ToolApprovalDecision("approval:test-r2-7", "approved_session")

    # approved_session 必须被 approved_binding 接受（修复前抛 ValueError）
    binding = request.approved_binding(session_decision)
    assert binding["status"] == "APPROVED"
    assert binding["approval_id"] == "approval:test-r2-7"

    # approved_session 不能被 rejected_binding 接受（它确实批准了）
    with pytest.raises(ValueError):
        request.rejected_binding(session_decision)
