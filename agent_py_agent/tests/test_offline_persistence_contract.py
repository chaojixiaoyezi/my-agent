from __future__ import annotations


# LLM: State changes must have matching runlog entries.
# 函数用途: 验证状态已经变化但 runlog 缺失时会进入一致性失败。
def test_persistence_requires_runlog_for_state_change() -> None:
    from agent_py_agent.agent.contracts.offline_persistence_contract import (
        validate_persistence_consistency,
    )

    result = validate_persistence_consistency(
        {
            "state_updates": [{"event_id": "e1", "to": "VERIFYING"}],
            "runlog_entries": [],
        }
    )

    assert result.ok is False
    assert result.error_codes == ("PERSISTENCE_LOG_MISSING",)


# LLM: Executed tools must have ToolTrace before the task can continue.
# 函数用途: 验证工具已执行但 ToolTrace 缺失时必须进入恢复，不可假成功。
def test_persistence_requires_tooltrace_for_executed_tool() -> None:
    from agent_py_agent.agent.contracts.offline_persistence_contract import (
        validate_persistence_consistency,
    )

    result = validate_persistence_consistency(
        {
            "tool_executions": [{"operation_id": "op-1", "executed": True}],
            "tool_trace": [],
        }
    )

    assert result.error_codes == ("TOOL_EXECUTED_TRACE_MISSING",)


# LLM: Partial artifact writes and finalizer crashes must be revalidated.
# 函数用途: 验证半截产物和 finalizer 崩溃不会直接进入成功。
def test_persistence_detects_partial_artifact_and_finalizer_revalidation() -> None:
    from agent_py_agent.agent.contracts.offline_persistence_contract import (
        validate_persistence_consistency,
    )

    result = validate_persistence_consistency(
        {
            "artifact_writes": [{"path": "report.md", "atomic_complete": False}],
            "finalizer": {"crashed_after_verify": True, "revalidated_after_recovery": False},
        }
    )

    assert result.error_codes == ("ARTIFACT_PARTIAL_WRITE", "FINALIZER_REVALIDATION_REQUIRED")
