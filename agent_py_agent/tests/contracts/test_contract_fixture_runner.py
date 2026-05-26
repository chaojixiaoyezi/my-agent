from __future__ import annotations

import json
from pathlib import Path


def test_contract_fixture_rejects_missing_artifact(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import (
        FixtureRunFacts,
        verify_contract_fixture,
    )

    contract = _fixture("missing_artifact_should_fail.json")

    result = verify_contract_fixture(
        tmp_path,
        contract,
        FixtureRunFacts(tool_trace=({"tool": "write_file"},), final_status="SUCCEEDED"),
    )

    assert result.ok is False
    assert "ARTIFACT_MISSING" in result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.error_codes
    assert result.recommended_action == "repair_then_reverify"
    assert any(finding["code"] == "ARTIFACT_MISSING" for finding in result.findings)


def test_contract_fixture_rejects_empty_artifact(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import (
        FixtureRunFacts,
        verify_contract_fixture,
    )

    contract = _fixture("empty_artifact_should_fail.json")
    (tmp_path / "output.md").write_text("", encoding="utf-8")

    result = verify_contract_fixture(
        tmp_path,
        contract,
        FixtureRunFacts(tool_trace=({"tool": "write_file"},), final_status="SUCCEEDED"),
    )

    assert result.ok is False
    assert "ARTIFACT_TOO_SMALL" in result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.error_codes


def test_contract_fixture_rejects_missing_tool_trace(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import (
        FixtureRunFacts,
        verify_contract_fixture,
    )

    contract = _fixture("simple_file_summary.json")
    (tmp_path / "output.md").write_text("Summary\nEnough content\nChecked Files\ninput.txt\n", encoding="utf-8")

    result = verify_contract_fixture(tmp_path, contract, FixtureRunFacts(tool_trace=(), final_status="SUCCEEDED"))

    assert result.ok is False
    assert "TOOL_TRACE_EMPTY" in result.error_codes
    assert "REQUIRED_TOOL_CALL_MISSING" in result.error_codes


def test_contract_fixture_rejects_empty_structured_checkpoint(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import (
        FixtureRunFacts,
        verify_contract_fixture,
    )

    contract = _fixture("staged_json_no_rows_cannot_complete.json")
    (tmp_path / "source_data.json").write_text(
        '{"sheets":[{"name":"Week01","rows":[]}]}',
        encoding="utf-8",
    )

    result = verify_contract_fixture(
        tmp_path,
        contract,
        FixtureRunFacts(
            tool_trace=({"tool": "write_file", "result": {"ok": True}},),
            final_status="SUCCEEDED",
        ),
    )

    assert result.ok is False
    assert "REQUIRED_JSON_COLLECTION_EMPTY" in result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.error_codes


def test_contract_fixture_rejects_builder_not_called(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import (
        FixtureRunFacts,
        verify_contract_fixture,
    )

    contract = _fixture("builder_not_called_cannot_complete.json")
    (tmp_path / "source_data.json").write_text(
        '{"sheets":[{"name":"Week01","rows":[{"repo":"demo","stars":123}]}]}',
        encoding="utf-8",
    )

    result = verify_contract_fixture(
        tmp_path,
        contract,
        FixtureRunFacts(
            tool_trace=({"tool": "write_file", "result": {"ok": True}},),
            final_status="SUCCEEDED",
        ),
    )

    assert result.ok is False
    assert "REQUIRED_SUCCESSFUL_TOOL_CALL_MISSING" in result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.error_codes


def test_contract_fixture_rejects_failed_required_tool(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import (
        FixtureRunFacts,
        verify_contract_fixture,
    )

    contract = _fixture("tool_failed_cannot_complete.json")

    result = verify_contract_fixture(
        tmp_path,
        contract,
        FixtureRunFacts(
            tool_trace=(
                {"tool": "write_file", "result": {"ok": False, "error_code": "PATH_PERMISSION_DENIED"}},
            ),
            final_status="SUCCEEDED",
        ),
    )

    assert result.ok is False
    assert "REQUIRED_SUCCESSFUL_TOOL_CALL_MISSING" in result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.error_codes


def test_contract_fixture_rejects_success_when_no_progress_issue_blocks_completion(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import (
        FixtureRunFacts,
        verify_contract_fixture,
    )

    contract = _fixture("repeated_exploration_should_redirect_or_block.json")

    result = verify_contract_fixture(
        tmp_path,
        contract,
        FixtureRunFacts(
            tool_trace=(),
            final_status="SUCCEEDED",
            runtime_issues=({"code": "NO_PROGRESS", "severity": "hard"},),
        ),
    )

    assert result.ok is False
    assert "RUNTIME_ISSUE_SUCCESS_CONFLICT" in result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.error_codes


def test_contract_fixture_rejects_missing_required_runtime_issue(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import (
        FixtureRunFacts,
        verify_contract_fixture,
    )

    contract = _fixture("repeated_exploration_should_redirect_or_block.json")

    result = verify_contract_fixture(
        tmp_path,
        contract,
        FixtureRunFacts(tool_trace=(), final_status="BLOCKED", runtime_issues=()),
    )

    assert result.ok is False
    assert "REQUIRED_RUNTIME_ISSUE_MISSING" in result.error_codes


def test_contract_fixture_rejects_artifact_path_outside_run_dir(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import (
        FixtureRunFacts,
        verify_contract_fixture,
    )

    outside = tmp_path.parent / "outside.md"
    outside.write_text("Summary\nEvidence\nChecked Files\n", encoding="utf-8")
    contract = {
        "artifacts": {"required": [{"path": "../outside.md"}]},
        "tools": {"required_calls": ["write_file"]},
        "final_status": {"allow_succeeded_only_if": ["artifact_exists"]},
    }

    result = verify_contract_fixture(
        tmp_path,
        contract,
        FixtureRunFacts(tool_trace=({"tool": "write_file", "result": {"ok": True}},), final_status="SUCCEEDED"),
    )

    assert result.ok is False
    assert "ARTIFACT_PATH_OUTSIDE_RUN_DIR" in result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.error_codes


def test_contract_fixture_rejects_structured_evidence_without_source(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import (
        FixtureRunFacts,
        verify_contract_fixture,
    )

    report = tmp_path / "report.json"
    report.write_text('{"evidence":[{"summary":"saw suspicious event"}]}', encoding="utf-8")
    contract = {
        "artifacts": {
            "required": [
                {
                    "path": "report.json",
                    "json_requirements": {"required_non_empty_paths": ["evidence"]},
                    "evidence": {"json_path": "evidence", "require_source": True},
                }
            ]
        },
        "tools": {"required_calls": ["query_logs"]},
        "final_status": {
            "allow_succeeded_only_if": [
                "artifact_exists",
                "json_requirements_present",
                "evidence_source_present",
            ]
        },
    }

    result = verify_contract_fixture(
        tmp_path,
        contract,
        FixtureRunFacts(tool_trace=({"tool": "query_logs", "result": {"ok": True}},), final_status="SUCCEEDED"),
    )

    assert result.ok is False
    assert "EVIDENCE_SOURCE_MISSING" in result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.error_codes


def test_contract_fixture_rejects_dry_run_tool_as_real_execution(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import (
        FixtureRunFacts,
        verify_contract_fixture,
    )

    (tmp_path / "report.md").write_text("Summary\nEvidence\nDecision\n", encoding="utf-8")
    contract = {
        "artifacts": {"required": [{"path": "report.md", "min_size": 10}]},
        "tools": {"required_real_successful_calls": ["block_ip"]},
        "final_status": {
            "allow_succeeded_only_if": [
                "artifact_exists",
                "artifact_non_empty",
                "required_real_tool_calls_present",
            ]
        },
    }

    result = verify_contract_fixture(
        tmp_path,
        contract,
        FixtureRunFacts(
            tool_trace=(
                {"tool": "block_ip", "result": {"ok": True, "mode": "dry_run"}},
            ),
            final_status="SUCCEEDED",
        ),
    )

    assert result.ok is False
    assert "REQUIRED_REAL_TOOL_CALL_MISSING" in result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.error_codes


def test_contract_fixture_rejects_unreplayable_tool_trace(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import (
        FixtureRunFacts,
        verify_contract_fixture,
    )

    contract = {
        "tools": {
            "trace_requirements": {
                "require_operation_id": True,
                "require_duration_ms": True,
                "require_error_code_on_failure": True,
            }
        }
    }

    result = verify_contract_fixture(
        tmp_path,
        contract,
        FixtureRunFacts(tool_trace=({"tool": "query_logs", "result": {"ok": False}},), final_status="BLOCKED"),
    )

    assert result.ok is False
    assert "TOOL_TRACE_OPERATION_ID_MISSING" in result.error_codes
    assert "TOOL_TRACE_DURATION_MISSING" in result.error_codes
    assert "TOOL_TRACE_ERROR_CODE_MISSING" in result.error_codes


def test_contract_fixture_rejects_sensitive_values_in_tool_trace(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import (
        FixtureRunFacts,
        verify_contract_fixture,
    )

    contract = {"tools": {"trace_requirements": {"forbid_sensitive_values": True}}}

    result = verify_contract_fixture(
        tmp_path,
        contract,
        FixtureRunFacts(
            tool_trace=(
                {
                    "tool": "send_message",
                    "params": {"token": "plain-token"},
                    "result": {"ok": True, "password": "plain-password"},
                },
            ),
            final_status="BLOCKED",
        ),
    )

    assert result.ok is False
    assert "TOOL_TRACE_SECRET_LEAK" in result.error_codes


def _fixture(name: str) -> dict[str, object]:
    path = Path(__file__).parent / name
    return json.loads(path.read_text(encoding="utf-8"))
