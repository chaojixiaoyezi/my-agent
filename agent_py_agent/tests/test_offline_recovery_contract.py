from __future__ import annotations


def test_retryable_tool_failure_allows_bounded_retry() -> None:
    from agent_py_agent.agent.contracts.offline_recovery_contract import validate_recovery_events

    result = validate_recovery_events(
        (
            {
                "type": "tool_result",
                "operation_id": "op-fetch",
                "tool": "web_fetch",
                "ok": False,
                "error_code": "TOOL_TIMEOUT",
                "retryable": True,
                "attempt": 1,
                "retry_limit": 3,
            },
        )
    )

    assert result.ok is True
    assert result.actions[0]["code"] == "RETRY_ALLOWED"
    assert result.actions[0]["operation_id"] == "op-fetch"


def test_retryable_tool_failure_zero_retry_limit_is_unlimited() -> None:
    from agent_py_agent.agent.contracts.offline_recovery_contract import validate_recovery_events

    result = validate_recovery_events(
        (
            {
                "type": "tool_result",
                "operation_id": "op-fetch",
                "tool": "web_fetch",
                "ok": False,
                "error_code": "TOOL_TIMEOUT",
                "retryable": True,
                "attempt": 12,
                "retry_limit": 0,
            },
        )
    )

    assert result.ok is True
    assert result.actions[0]["code"] == "RETRY_ALLOWED"


def test_non_retryable_permission_failure_stops_retry() -> None:
    from agent_py_agent.agent.contracts.offline_recovery_contract import validate_recovery_events

    result = validate_recovery_events(
        (
            {
                "type": "tool_result",
                "operation_id": "op-write",
                "tool": "write_file",
                "ok": False,
                "error_code": "PATH_PERMISSION_DENIED",
                "retryable": False,
                "attempt": 1,
                "retry_limit": 3,
            },
        )
    )

    assert result.ok is False
    assert result.error_codes == ("NON_RETRYABLE_FAILURE",)


def test_corrupt_state_file_blocks_with_recovery_diagnostic() -> None:
    from agent_py_agent.agent.contracts.offline_recovery_contract import validate_recovery_events

    result = validate_recovery_events(
        (
            {
                "type": "state_load_result",
                "run_id": "run-1",
                "ok": False,
                "error_code": "JSON_DECODE_ERROR",
                "state_ref": "runs/run-1/state.json",
            },
        )
    )

    assert result.ok is False
    assert result.error_codes == ("STATE_CORRUPT",)
    assert result.actions[0]["next_status"] == "BLOCKED"
    assert result.recovery is not None
    assert result.recovery["status"] == "recovering"
    assert "checkpoint" in result.recovery["message_zh"]


def test_artifact_written_before_finalizer_crash_revalidates_without_reexecuting_side_effects() -> None:
    from agent_py_agent.agent.contracts.offline_recovery_contract import validate_recovery_events

    result = validate_recovery_events(
        (
            {
                "type": "finalizer_crash",
                "run_id": "run-1",
                "artifact_refs": ["runs/run-1/artifacts/report.md"],
                "executed_side_effect_operation_ids": ["op-send-message"],
            },
        )
    )

    assert result.ok is False
    assert result.error_codes == ("REVALIDATE_ARTIFACT_BEFORE_RERUN",)
    assert result.actions[0]["forbid_reexecute_operation_ids"] == "op-send-message"
    assert result.recovery is not None
    assert result.recovery["next_status"] == "RECOVERING"
