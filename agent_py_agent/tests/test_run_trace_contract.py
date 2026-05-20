from __future__ import annotations


# LLM: Run trace validation must prove state and tool ledgers are replayable without prompt text.
# 函数用途: 验证合法状态迁移和完整 ToolTrace 字段可以通过合同校验。
def test_run_trace_contract_accepts_replayable_state_and_tool_events() -> None:
    from agent_py_agent.agent.contracts.run_trace_contract import validate_run_trace_events

    result = validate_run_trace_events(
        (
            {"type": "state_transition", "run_id": "run-1", "from": "PLANNING", "event": "PLAN_DONE", "to": "RUNNING"},
            {
                "type": "tool_result",
                "run_id": "run-1",
                "tool": "read_file",
                "operation_id": "op-read",
                "ok": True,
                "duration_ms": 12,
            },
        )
    )

    assert result.ok is True
    assert result.error_codes == ()


# LLM: State ledgers must reject fake completion paths before replay trusts final status.
# 函数用途: 验证 RUNNING 直接到 DONE/SUCCEEDED 这类绕过 VERIFYING 的状态账本会失败。
def test_run_trace_contract_rejects_invalid_state_transition_sequence() -> None:
    from agent_py_agent.agent.contracts.run_trace_contract import validate_run_trace_events

    result = validate_run_trace_events(
        (
            {"type": "state_transition", "run_id": "run-1", "from": "PLANNING", "event": "PLAN_DONE", "to": "RUNNING"},
            {"type": "state_transition", "run_id": "run-1", "from": "RUNNING", "event": "DONE", "to": "DONE"},
        )
    )

    assert result.ok is False
    assert "STATE_TRANSITION_INVALID" in result.error_codes


# LLM: ToolTrace rows need operation id, duration, and structured failure code for recovery.
# 函数用途: 验证工具结果缺少可回放字段时，账本合同会返回机器错误码。
def test_run_trace_contract_rejects_unreplayable_tool_result_rows() -> None:
    from agent_py_agent.agent.contracts.run_trace_contract import validate_run_trace_events

    result = validate_run_trace_events(
        (
            {"type": "tool_result", "run_id": "run-1", "tool": "query_logs", "ok": False},
        )
    )

    assert result.ok is False
    assert result.error_codes == (
        "TOOL_TRACE_OPERATION_ID_MISSING",
        "TOOL_TRACE_DURATION_MISSING",
        "TOOL_TRACE_ERROR_CODE_MISSING",
    )
