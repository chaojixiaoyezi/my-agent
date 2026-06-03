from __future__ import annotations


def test_defense_contract_rejects_unsafe_blocking_decisions() -> None:
    from agent_py_agent.agent.contracts.offline_defense_audit_observability_contract import (
        validate_defense_audit_observability,
    )

    result = validate_defense_audit_observability(
        {
            "defense_actions": [
                {"target": "10.0.0.1", "target_in_whitelist": True},
                {"cidr_prefix": 16, "min_cidr_prefix": 24},
                {"risk_score": 59, "decision": "auto_block"},
                {"evidence_count": 1, "min_evidence_count": 2},
                {"action": "block_ip", "expire_at": ""},
            ]
        }
    )

    assert result.error_codes == (
        "DEFENSE_WHITELIST_BLOCK_FORBIDDEN",
        "DEFENSE_CIDR_TOO_BROAD",
        "DEFENSE_RISK_DECISION_INVALID",
        "DEFENSE_EVIDENCE_INSUFFICIENT",
        "DEFENSE_BLOCK_EXPIRY_MISSING",
    )


def test_audit_contract_requires_fields_redaction_and_links() -> None:
    from agent_py_agent.agent.contracts.offline_defense_audit_observability_contract import (
        validate_defense_audit_observability,
    )

    result = validate_defense_audit_observability(
        {
            "audit_records": [
                {
                    "who": "agent",
                    "when": "",
                    "what": "block_ip",
                    "why": "risk",
                    "approval_id": "ap-1",
                    "tool_result_ref": "",
                    "evidence_refs": [],
                    "secret": "raw",
                }
            ]
        }
    )

    assert result.error_codes == ("AUDIT_FIELD_MISSING", "AUDIT_SECRET_LEAK", "AUDIT_LINK_MISSING")


def test_observability_contract_requires_metrics_and_stuck_alerts() -> None:
    from agent_py_agent.agent.contracts.offline_defense_audit_observability_contract import (
        validate_defense_audit_observability,
    )

    result = validate_defense_audit_observability(
        {
            "metrics": {"task_duration": 120},
            "stuck_tasks": [{"task_id": "t1", "age_ms": 9000, "stuck_threshold_ms": 1000, "alert_emitted": False}],
        }
    )

    assert result.error_codes == ("METRIC_FIELD_MISSING", "STUCK_TASK_ALERT_REQUIRED")
