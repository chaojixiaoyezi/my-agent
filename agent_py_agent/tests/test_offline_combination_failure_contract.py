from __future__ import annotations


def test_combination_rejects_tool_failure_artifact_without_tool_and_approval_tamper() -> None:
    from agent_py_agent.agent.contracts.offline_combination_failure_contract import (
        validate_combination_failures,
    )

    result = validate_combination_failures(
        {
            "tool_results": [{"tool": "query_logs", "ok": False}],
            "final_claim": {"claimed_success": True},
            "artifacts": [{"path": "report.md", "exists": True}],
            "required_tools": ["query_logs"],
            "approval": {"approved_args_hash": "a", "executed_args_hash": "b"},
        }
    )

    assert result.error_codes == (
        "TOOL_FAILED_MODEL_CLAIMED_SUCCESS",
        "ARTIFACT_WITHOUT_REQUIRED_TOOL",
        "APPROVAL_ARGS_CHANGED",
    )


def test_combination_rejects_compact_repeat_and_parent_missing_artifact() -> None:
    from agent_py_agent.agent.contracts.offline_combination_failure_contract import (
        validate_combination_failures,
    )

    result = validate_combination_failures(
        {
            "compact": {"after_compact_repeated_action": True},
            "child_tasks": [{"status": "SUCCEEDED"}],
            "parent_artifact": {"required": True, "exists": False},
        }
    )

    assert result.error_codes == ("COMPACT_REPEAT_AFTER_COMPACT", "CHILD_OK_PARENT_ARTIFACT_MISSING")
