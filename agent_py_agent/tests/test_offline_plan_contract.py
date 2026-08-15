from __future__ import annotations


def test_plan_requires_output_paths_and_required_tools() -> None:
    from agent_py_agent.agent.contracts.offline_plan_contract import validate_plan_contract

    result = validate_plan_contract(
        plan={"steps": [{"tool": "write_file"}], "output_paths": []},
        contract={"required_tools": ["query_logs"], "artifacts": {"required": [{"path": "report.md"}]}},
    )

    assert result.ok is False
    assert result.error_codes == ("PLAN_OUTPUT_PATH_MISSING", "PLAN_REQUIRED_TOOL_MISSING")


def test_plan_rejects_forbidden_tools_and_artifact_drift() -> None:
    from agent_py_agent.agent.contracts.offline_plan_contract import validate_plan_contract

    result = validate_plan_contract(
        plan={"steps": [{"tool": "block_ip"}], "output_paths": ["artifacts/run-1/report.md"]},
        contract={"forbidden_tools": ["block_ip"]},
        execution_events=(
            {"type": "artifact_write", "path": "/tmp/report.md"},
        ),
    )

    assert result.ok is False
    assert result.error_codes == ("PLAN_FORBIDDEN_TOOL", "PLAN_ARTIFACT_DRIFT")


def test_replan_rejects_repeated_failed_params_but_allows_changed_strategy() -> None:
    from agent_py_agent.agent.contracts.offline_plan_contract import validate_plan_contract

    repeated = validate_plan_contract(
        plan={"steps": [{"tool": "query_logs", "args_hash": "index-a"}], "output_paths": ["report.md"]},
        contract={},
        execution_events=({"type": "tool_result", "tool": "query_logs", "args_hash": "index-a", "ok": False},),
    )
    changed = validate_plan_contract(
        plan={"steps": [{"tool": "query_logs", "args_hash": "index-b"}], "output_paths": ["report.md"]},
        contract={},
        execution_events=({"type": "tool_result", "tool": "query_logs", "args_hash": "index-a", "ok": False},),
    )

    assert repeated.error_codes == ("REPLAN_REPEATS_FAILED_PARAMS",)
    assert changed.ok is True
