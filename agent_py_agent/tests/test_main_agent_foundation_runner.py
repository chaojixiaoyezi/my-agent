"""Focused tests for the main-agent foundation test runner."""

from __future__ import annotations


def test_main_agent_foundation_runner_reports_core_categories(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_foundation_runner import (
        MainAgentFoundationRequest,
        run_main_agent_foundation,
    )

    report = run_main_agent_foundation(MainAgentFoundationRequest(workspace=tmp_path))

    assert report.ok is True
    assert report.summary["total"] == 12
    assert report.summary["failed"] == 0
    by_id = {item.case_id: item for item in report.results}
    assert by_id["tool_failure_contracts"].status == "PASSED"
    assert by_id["research_evidence_contracts"].status == "PASSED"
    assert by_id["web_artifact_validator"].status == "PASSED"
    assert by_id["activity_timeout_recovery"].status == "PASSED"
    assert by_id["model_call_ledger_timeout"].status == "PASSED"
    assert by_id["tool_protocol_v2_envelope"].status == "PASSED"
    assert by_id["general_write_contract"].status == "PASSED"
    assert by_id["large_output_artifact_refs"].status == "PASSED"
    assert by_id["deterministic_e2e_matrix"].status == "PASSED"
    assert by_id["single_agent_real_tasks"].status == "SKIPPED"
    assert by_id["compact_resume_real_cycle"].status == "SKIPPED"
    assert by_id["tool_error_recovery_real"].status == "SKIPPED"


def test_main_agent_foundation_report_is_refs_first(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_foundation_runner import (
        MainAgentFoundationRequest,
        run_main_agent_foundation,
    )

    payload = run_main_agent_foundation(MainAgentFoundationRequest(workspace=tmp_path)).to_dict()

    assert payload["summary"]["total"] == 12
    assert "large output row" not in str(payload)
    assert "hello world" not in str(payload)
    large_case = next(
        item for item in payload["results"] if item["case_id"] == "large_output_artifact_refs"
    )
    assert large_case["evidence_refs"]


def test_main_agent_foundation_tool_failure_contracts_are_specific(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_foundation_runner import (
        MainAgentFoundationRequest,
        run_main_agent_foundation,
    )

    report = run_main_agent_foundation(MainAgentFoundationRequest(workspace=tmp_path))
    item = next(result for result in report.results if result.case_id == "tool_failure_contracts")

    assert item.status == "PASSED"
    assert not item.issues
    evidence = item.evidence_refs[0]
    text = (
        tmp_path.joinpath(evidence).read_text(encoding="utf-8")
        if not evidence.startswith("/")
        else __import__("pathlib").Path(evidence).read_text(encoding="utf-8")
    )
    assert "PATH_INVALID" in text
    assert "TOOL_TIMEOUT" in text
    assert "MODEL_UPSTREAM_FAILED" in text


def test_main_agent_foundation_requested_real_model_placeholders_fail_closed(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_foundation_runner import (
        MainAgentFoundationRequest,
        run_main_agent_foundation,
    )

    report = run_main_agent_foundation(
        MainAgentFoundationRequest(workspace=tmp_path, include_real_model=True)
    )

    assert report.ok is False
    by_id = {item.case_id: item for item in report.results}
    assert by_id["single_agent_real_tasks"].status == "FAILED"
    assert by_id["single_agent_real_tasks"].issues == ["REAL_MODEL_RUNNER_NOT_IMPLEMENTED"]
