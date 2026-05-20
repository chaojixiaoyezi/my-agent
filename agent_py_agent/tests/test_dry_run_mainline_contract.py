from __future__ import annotations


def test_dry_run_mainline_accepts_structured_alert_analysis_facts() -> None:
    from agent_py_agent.agent.contracts.dry_run_mainline_contract import validate_dry_run_mainline

    result = validate_dry_run_mainline(
        {
            "execution_mode": "dry_run",
            "required_inputs": ["alert_id", "subject_ref"],
            "inputs": {"alert_id": "ALERT-1", "subject_ref": "artifact://inputs/alert.json"},
            "required_tools": ["query_logs", "query_assets"],
            "tool_results": [
                {"tool": "query_logs", "ok": True, "mode": "read_only", "operation_id": "tool-log"},
                {"tool": "query_assets", "ok": True, "mode": "read_only", "operation_id": "tool-asset"},
            ],
            "artifact_refs": [{"kind": "report", "ref": "artifact://runs/1/report.md", "bytes": 512}],
            "evidence_refs": [
                {"evidence_id": "EV-1", "source_type": "tool_result", "source_ref": "tool-log"}
            ],
            "action_intents": [{"intent": "block_ip", "mode": "dry_run", "args_hash": "ip-a"}],
            "executed_actions": [],
        }
    )

    assert result.ok is True
    assert result.error_codes == ()


def test_dry_run_mainline_rejects_missing_evidence_failed_tools_and_real_actions() -> None:
    from agent_py_agent.agent.contracts.dry_run_mainline_contract import validate_dry_run_mainline

    result = validate_dry_run_mainline(
        {
            "execution_mode": "dry_run",
            "required_inputs": ["alert_id", "subject_ref"],
            "inputs": {"alert_id": ""},
            "required_tools": ["query_logs"],
            "tool_results": [{"tool": "query_logs", "ok": False, "mode": "read_only", "operation_id": "tool-log"}],
            "artifact_refs": [{"kind": "report", "ref": "artifact://runs/1/report.md", "bytes": 0}],
            "evidence_refs": [{"evidence_id": "EV-1", "source_type": "tool_result", "source_ref": ""}],
            "action_intents": [{"intent": "block_ip", "mode": "real_run", "args_hash": "ip-a"}],
            "executed_actions": [{"intent": "block_ip", "mode": "real_run", "args_hash": "ip-a"}],
        }
    )

    assert result.ok is False
    assert result.error_codes == (
        "DRY_RUN_REQUIRED_INPUT_MISSING",
        "DRY_RUN_REQUIRED_TOOL_FAILED",
        "DRY_RUN_ARTIFACT_EMPTY",
        "DRY_RUN_EVIDENCE_SOURCE_MISSING",
        "DRY_RUN_INTENT_NOT_DRY_RUN",
        "DRY_RUN_REAL_ACTION_EXECUTED",
    )
