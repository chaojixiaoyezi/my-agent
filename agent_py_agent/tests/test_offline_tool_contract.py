from __future__ import annotations


def test_tool_result_rejects_none_and_malformed_payloads() -> None:
    from agent_py_agent.agent.contracts.offline_tool_contract import validate_tool_events

    result = validate_tool_events(
        (
            {"type": "tool_result", "operation_id": "op-none", "tool": "query_logs", "result": None},
            {"type": "tool_result", "operation_id": "op-text", "tool": "query_logs", "result": "success"},
        )
    )

    assert result.ok is False
    assert result.error_codes == ("TOOL_RESULT_NONE", "TOOL_RESULT_SHAPE_INVALID")


def test_large_tool_output_requires_artifact_ref_or_truncation_marker() -> None:
    from agent_py_agent.agent.contracts.offline_tool_contract import validate_tool_events

    result = validate_tool_events(
        (
            {
                "type": "tool_result",
                "operation_id": "op-large",
                "tool": "query_logs",
                "inline_budget_bytes": 10,
                "result": {"ok": True, "content": "x" * 50, "artifact_refs": [], "truncated": False},
            },
        )
    )

    assert result.ok is False
    assert result.error_codes == ("TOOL_RESULT_TOO_LARGE_NOT_EXTERNALIZED",)


def test_tool_result_rejects_unredacted_secret_fields() -> None:
    from agent_py_agent.agent.contracts.offline_tool_contract import validate_tool_events

    result = validate_tool_events(
        (
            {
                "type": "tool_result",
                "operation_id": "op-secret",
                "tool": "fetch_config",
                "result": {"ok": True, "data": {"token": "plain-token"}},
            },
        )
    )

    assert result.ok is False
    assert result.error_codes == ("TOOL_RESULT_SECRET_LEAK",)
    assert result.findings[0]["field_path"] == "result.data.token"


def test_repeated_identical_tool_calls_block_after_threshold() -> None:
    from agent_py_agent.agent.contracts.offline_tool_contract import validate_tool_events

    call = {"tool": "query_logs", "args_hash": "args-1", "result_hash": "result-1", "read_only": True}
    result = validate_tool_events(
        (
            {"type": "tool_result", "operation_id": "op-1", "result": {"ok": True}, **call},
            {"type": "tool_result", "operation_id": "op-2", "result": {"ok": True}, **call},
            {"type": "tool_result", "operation_id": "op-3", "result": {"ok": True}, **call},
        ),
        repeated_threshold=3,
    )

    assert result.ok is False
    assert result.error_codes == ("TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED",)


def test_repeated_threshold_zero_is_unlimited() -> None:
    from agent_py_agent.agent.contracts.offline_tool_contract import validate_tool_events

    call = {"tool": "query_logs", "args_hash": "args-1", "result_hash": "result-1", "read_only": True}
    result = validate_tool_events(
        tuple(
            {"type": "tool_result", "operation_id": f"op-{idx}", "result": {"ok": True}, **call}
            for idx in range(10)
        ),
        repeated_threshold=0,
    )

    assert result.ok is True


def test_same_tool_with_different_args_is_allowed() -> None:
    from agent_py_agent.agent.contracts.offline_tool_contract import validate_tool_events

    result = validate_tool_events(
        (
            {
                "type": "tool_result",
                "operation_id": "op-1",
                "tool": "query_logs",
                "args_hash": "args-a",
                "result_hash": "result-a",
                "read_only": True,
                "result": {"ok": True, "rows": [1]},
            },
            {
                "type": "tool_result",
                "operation_id": "op-2",
                "tool": "query_logs",
                "args_hash": "args-b",
                "result_hash": "result-b",
                "read_only": True,
                "result": {"ok": True, "rows": [2]},
            },
        )
    )

    assert result.ok is True
    assert result.error_codes == ()
