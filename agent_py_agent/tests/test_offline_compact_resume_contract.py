from __future__ import annotations


def test_compact_resume_preserves_waiting_for_human_and_tool_refs() -> None:
    from agent_py_agent.agent.contracts.offline_compact_resume_contract import (
        validate_compact_resume_bundle,
    )

    result = validate_compact_resume_bundle(
        {
            "compact": {
                "current_status": "WAITING_FOR_USER",
                "waiting_reason": "approval",
                "tool_result_refs": ["memory://tool-results/op-query"],
                "failed_operation_ids": [],
                "executed_side_effect_operation_ids": [],
                "summary": _complete_summary(),
            },
            "resume_events": [],
        }
    )

    assert result.ok is True
    assert result.error_codes == ()


def test_compact_resume_rejects_missing_waiting_state_or_tool_refs() -> None:
    from agent_py_agent.agent.contracts.offline_compact_resume_contract import (
        validate_compact_resume_bundle,
    )

    result = validate_compact_resume_bundle(
        {
            "pre_compact": {
                "current_status": "WAITING_FOR_TOOL",
                "waiting_reason": "tool_result",
                "tool_result_refs": ["memory://tool-results/op-query"],
            },
            "compact": {
                "current_status": "",
                "waiting_reason": "",
                "tool_result_refs": [],
                "failed_operation_ids": [],
                "executed_side_effect_operation_ids": [],
                "summary": _complete_summary(),
            },
            "resume_events": [],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("COMPACT_STATE_MISSING", "COMPACT_TOOL_REF_MISSING")


def test_compact_resume_cannot_forget_failed_tool_result() -> None:
    from agent_py_agent.agent.contracts.offline_compact_resume_contract import (
        validate_compact_resume_bundle,
    )

    result = validate_compact_resume_bundle(
        {
            "pre_compact_events": [
                {"type": "tool_result", "operation_id": "op-failed", "ok": False, "error_code": "TOOL_TIMEOUT"}
            ],
            "compact": {
                "current_status": "RUNNING",
                "tool_result_refs": ["memory://tool-results/op-failed"],
                "failed_operation_ids": [],
                "executed_side_effect_operation_ids": [],
                "summary": _complete_summary(),
            },
            "resume_events": [],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("COMPACT_FAILURE_FORGOTTEN",)


def test_compact_resume_cannot_repeat_dangerous_operation() -> None:
    from agent_py_agent.agent.contracts.offline_compact_resume_contract import (
        validate_compact_resume_bundle,
    )

    result = validate_compact_resume_bundle(
        {
            "compact": {
                "current_status": "RUNNING",
                "tool_result_refs": [],
                "failed_operation_ids": [],
                "executed_side_effect_operation_ids": ["op-block-ip"],
                "summary": _complete_summary(),
            },
            "resume_events": [
                {"type": "tool_call", "operation_id": "op-block-ip", "tool": "block_ip"},
            ],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("DANGEROUS_OPERATION_REPLAYED",)


def test_compact_resume_cannot_repeat_no_progress_loop() -> None:
    from agent_py_agent.agent.contracts.offline_compact_resume_contract import (
        validate_compact_resume_bundle,
    )

    result = validate_compact_resume_bundle(
        {
            "compact": {
                "current_status": "RUNNING",
                "tool_result_refs": [],
                "failed_operation_ids": [],
                "executed_side_effect_operation_ids": [],
                "no_progress_fingerprints": ["read:a:unchanged"],
                "summary": _complete_summary(),
            },
            "resume_events": [
                {"type": "tool_call", "tool": "read_file", "progress_fingerprint": "read:a:unchanged"},
            ],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("COMPACT_NO_PROGRESS_LOOP_REPEATED",)


def test_compact_summary_quality_requires_state_done_next_and_refs() -> None:
    from agent_py_agent.agent.contracts.offline_compact_resume_contract import (
        validate_compact_resume_bundle,
    )

    result = validate_compact_resume_bundle(
        {
            "compact": {
                "current_status": "RUNNING",
                "tool_result_refs": [],
                "failed_operation_ids": [],
                "executed_side_effect_operation_ids": [],
                "summary": {"current_status": "RUNNING", "completed_actions": ["read file"]},
            },
            "resume_events": [],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("COMPACT_SUMMARY_INCOMPLETE",)
    assert "pending_actions" in result.findings[0]["missing_fields"]


def _complete_summary() -> dict[str, object]:
    return {
        "current_status": "RUNNING",
        "completed_actions": ["read source"],
        "pending_actions": ["write report"],
        "evidence_refs": ["memory://evidence/source"],
        "next_constraints": ["do not replay side effects"],
    }
