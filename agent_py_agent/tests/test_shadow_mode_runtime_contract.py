from __future__ import annotations


def test_shadow_runtime_accepts_run_backed_by_phase5_tool_probes() -> None:
    from agent_py_agent.agent.contracts.shadow_mode_runtime_contract import (
        validate_shadow_mode_runtime,
    )

    result = validate_shadow_mode_runtime(_valid_shadow_runtime())

    assert result.ok is True
    assert result.error_codes == ()


def test_shadow_runtime_rejects_missing_phase5_probe_and_real_side_effect() -> None:
    from agent_py_agent.agent.contracts.shadow_mode_runtime_contract import (
        validate_shadow_mode_runtime,
    )

    facts = _valid_shadow_runtime()
    facts["real_tool_probe_refs"] = []
    facts["executed_actions"] = [{"action_id": "ACT-1", "mode": "real_run"}]
    facts["shadow_facts"]["executed_actions"] = [{"action_id": "ACT-1", "mode": "real_run"}]
    facts["comparison_artifacts"] = []

    result = validate_shadow_mode_runtime(facts)

    assert result.error_codes == (
        "SHADOW_RUNTIME_PHASE5_PROBE_MISSING",
        "SHADOW_RUNTIME_COMPARISON_ARTIFACT_MISSING",
        "SHADOW_REAL_ACTION_EXECUTED",
        "SHADOW_RUNTIME_SIDE_EFFECT_EXECUTED",
    )


def _valid_shadow_runtime() -> dict[str, object]:
    return {
        "run_id": "shadow-run-1",
        "stage": "shadow_mode",
        "mode": "shadow",
        "real_tool_probe_refs": [
            {
                "probe_id": "read-file-success",
                "contract_ref": "artifact://phase5/read-file-probe.json",
                "validation_ok": True,
                "effect": "read_only",
                "mode": "read_only",
                "tool_executor_ref": "tool_registry.execute_call",
            },
            {
                "probe_id": "controlled-exec-dry-run",
                "contract_ref": "artifact://phase5/controlled-exec-probe.json",
                "validation_ok": True,
                "effect": "dangerous",
                "mode": "dry_run",
                "tool_executor_ref": "tool_registry.execute_call",
            },
        ],
        "shadow_facts": _valid_shadow_facts(),
        "comparison_artifacts": [
            {
                "artifact_ref": "artifact://shadow/human-review.json",
                "kind": "human_review",
                "exists": True,
            }
        ],
        "executed_actions": [],
    }


def _valid_shadow_facts() -> dict[str, object]:
    return {
        "mode": "shadow",
        "risk": {"score": 72},
        "evidence_refs": [
            {"evidence_id": "EV-1", "source_type": "tool_result", "source_ref": "tool://read-file-success"}
        ],
        "recommended_actions": [
            {
                "action_id": "ACT-1",
                "effect": "dangerous",
                "mode": "dry_run",
                "operator_review_ref": "artifact://shadow/review-card.json",
                "approval_draft_ref": "artifact://shadow/approval-card.json",
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
        },
    }
