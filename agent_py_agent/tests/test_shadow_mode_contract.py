from __future__ import annotations


def test_shadow_mode_accepts_reviewable_recommendation_with_human_comparison() -> None:
    from agent_py_agent.agent.contracts.shadow_mode_contract import validate_shadow_mode_run

    result = validate_shadow_mode_run(
        {
            "mode": "shadow",
            "risk": {"score": 72, "band": "medium"},
            "evidence_refs": [
                {"evidence_id": "EV-1", "source_type": "tool_result", "source_ref": "tool-log"}
            ],
            "recommended_actions": [
                {
                    "action_id": "ACT-1",
                    "intent": "block_subject",
                    "effect": "dangerous",
                    "mode": "dry_run",
                    "operator_review_ref": "artifact://shadow/review-card.json",
                    "approval_draft_ref": "artifact://shadow/approval-card.json",
                    "ticket_draft_ref": "artifact://shadow/ticket-draft.json",
                }
            ],
            "dry_run_results": [
                {"action_id": "ACT-1", "mode": "dry_run", "ok": True, "result_ref": "artifact://shadow/dry-run.json"}
            ],
            "executed_actions": [],
            "human_review": {
                "review_id": "HR-1",
                "review_ref": "artifact://shadow/human-review.json",
                "decision": "agree",
                "agreement": True,
                "disposition_bias": "none",
            },
        }
    )

    assert result.ok is True
    assert result.error_codes == ()


def test_shadow_mode_rejects_real_execution_missing_evidence_and_missing_review() -> None:
    from agent_py_agent.agent.contracts.shadow_mode_contract import validate_shadow_mode_run

    result = validate_shadow_mode_run(
        {
            "mode": "shadow",
            "risk": {"band": "high"},
            "evidence_refs": [{"evidence_id": "EV-1", "source_type": "tool_result", "source_ref": ""}],
            "recommended_actions": [
                {
                    "action_id": "ACT-1",
                    "intent": "block_subject",
                    "effect": "dangerous",
                    "mode": "real_run",
                    "operator_review_ref": "",
                    "approval_draft_ref": "",
                }
            ],
            "dry_run_results": [],
            "executed_actions": [{"action_id": "ACT-1", "mode": "real_run"}],
        }
    )

    assert result.ok is False
    assert result.error_codes == (
        "SHADOW_RISK_SCORE_MISSING",
        "SHADOW_EVIDENCE_SOURCE_MISSING",
        "SHADOW_ACTION_NOT_REVIEW_ONLY",
        "SHADOW_OPERATOR_REVIEW_REF_MISSING",
        "SHADOW_DANGEROUS_DRY_RUN_MISSING",
        "SHADOW_APPROVAL_DRAFT_MISSING",
        "SHADOW_REAL_ACTION_EXECUTED",
        "SHADOW_HUMAN_REVIEW_MISSING",
    )


def test_shadow_mode_requires_mismatch_reason_when_human_disagrees() -> None:
    from agent_py_agent.agent.contracts.shadow_mode_contract import validate_shadow_mode_run

    result = validate_shadow_mode_run(
        {
            "mode": "shadow",
            "risk": {"score": 20},
            "evidence_refs": [
                {"evidence_id": "EV-1", "source_type": "artifact", "source_ref": "artifact://ev/1"}
            ],
            "recommended_actions": [],
            "executed_actions": [],
            "human_review": {
                "review_id": "HR-1",
                "review_ref": "artifact://shadow/human-review.json",
                "decision": "disagree",
                "agreement": False,
                "mismatch_reason_codes": [],
                "missing_evidence_codes": [],
            },
        }
    )

    assert result.error_codes == ("SHADOW_REVIEW_DISAGREEMENT_REASON_MISSING",)
