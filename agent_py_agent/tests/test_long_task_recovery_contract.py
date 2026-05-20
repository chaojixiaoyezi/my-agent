from __future__ import annotations


def test_long_task_recovery_accepts_compact_resume_and_checkpoint_refs() -> None:
    from agent_py_agent.agent.contracts.long_task_recovery_contract import (
        validate_long_task_recovery,
    )

    result = validate_long_task_recovery(_valid_recovery())

    assert result.ok is True
    assert result.error_codes == ()


def test_long_task_recovery_rejects_missing_resume_and_replayed_side_effect() -> None:
    from agent_py_agent.agent.contracts.long_task_recovery_contract import (
        validate_long_task_recovery,
    )

    recovery = _valid_recovery()
    recovery["compact_cycles"][0]["resume_ref"] = ""
    recovery["latest_resume_packet"]["restored_state_refs"] = []
    recovery["side_effect_ledger"]["replayed_action_ids"] = ["send-message-1"]

    result = validate_long_task_recovery(recovery)

    assert result.error_codes == (
        "LONG_TASK_COMPACT_RESUME_REF_MISSING",
        "LONG_TASK_RESTORED_STATE_REF_MISSING",
        "LONG_TASK_SIDE_EFFECT_REPLAYED",
    )


def _valid_recovery() -> dict[str, object]:
    return {
        "task_id": "task-long-1",
        "run_id": "run-long-1",
        "run_scope_ref": "scope://run-long-1",
        "checkpoints": [
            {
                "checkpoint_ref": "artifact://run-long-1/checkpoints/001.json",
                "sequence": 1,
                "state_ref": "artifact://run-long-1/state-001.json",
                "artifact_refs": ["artifact://run-long-1/output.part.json"],
                "status": "RUNNING",
            }
        ],
        "compact_cycles": [
            {
                "cycle_id": "compact-1",
                "bundle_ref": "artifact://run-long-1/context-bundle.json",
                "apply_ref": "artifact://run-long-1/compact-apply.json",
                "resume_ref": "artifact://run-long-1/resume-packet.json",
            }
        ],
        "latest_resume_packet": {
            "packet_ref": "artifact://run-long-1/resume-packet.json",
            "restored_state_refs": ["artifact://run-long-1/state-001.json"],
            "next_action_refs": ["action://continue-writing"],
        },
        "side_effect_ledger": {
            "executed_action_ids": ["send-message-1"],
            "replayed_action_ids": [],
            "idempotency_state_ref": "artifact://run-long-1/idempotency.json",
        },
    }
