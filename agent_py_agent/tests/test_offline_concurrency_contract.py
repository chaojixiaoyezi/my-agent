from __future__ import annotations


def test_only_one_worker_can_claim_same_run_lease() -> None:
    from agent_py_agent.agent.contracts.offline_concurrency_contract import (
        validate_concurrency_events,
    )

    result = validate_concurrency_events(
        (
            {"type": "lease_claimed", "run_id": "run-1", "worker_id": "worker-a", "lease_id": "lease-a"},
            {"type": "lease_claimed", "run_id": "run-1", "worker_id": "worker-b", "lease_id": "lease-b"},
        )
    )

    assert result.ok is False
    assert result.error_codes == ("LEASE_ALREADY_HELD",)
    assert result.findings[0]["run_id"] == "run-1"


def test_two_runs_cannot_write_same_artifact_without_same_idempotency_key() -> None:
    from agent_py_agent.agent.contracts.offline_concurrency_contract import (
        validate_concurrency_events,
    )

    conflict = validate_concurrency_events(
        (
            {
                "type": "artifact_write",
                "run_id": "run-a",
                "artifact_ref": "runs/shared/output.md",
                "idempotency_key": "idem-a",
            },
            {
                "type": "artifact_write",
                "run_id": "run-b",
                "artifact_ref": "runs/shared/output.md",
                "idempotency_key": "idem-b",
            },
        )
    )
    replay = validate_concurrency_events(
        (
            {
                "type": "artifact_write",
                "run_id": "run-a",
                "artifact_ref": "runs/shared/output.md",
                "idempotency_key": "idem-same",
            },
            {
                "type": "artifact_write",
                "run_id": "run-a",
                "artifact_ref": "runs/shared/output.md",
                "idempotency_key": "idem-same",
            },
        )
    )

    assert conflict.ok is False
    assert conflict.error_codes == ("ARTIFACT_WRITE_CONFLICT",)
    assert replay.ok is True


def test_same_approval_decision_accepts_first_terminal_status_only() -> None:
    from agent_py_agent.agent.contracts.offline_concurrency_contract import (
        validate_concurrency_events,
    )

    result = validate_concurrency_events(
        (
            {"type": "approval_decision", "approval_id": "approval-1", "status": "APPROVED", "actor": "lead"},
            {"type": "approval_decision", "approval_id": "approval-1", "status": "REJECTED", "actor": "other"},
        )
    )

    assert result.ok is False
    assert result.error_codes == ("APPROVAL_DECISION_ALREADY_FINAL",)
    assert result.findings[0]["approval_id"] == "approval-1"
