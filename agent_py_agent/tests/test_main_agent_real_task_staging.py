"""Tests for staged contracts in long main-agent real tasks."""

from __future__ import annotations

import json
from pathlib import Path


def test_real_task_acceptance_reports_candidate_artifact_paths(tmp_path):
    from openpyxl import Workbook

    from agent_py_agent.agent.contracts.main_agent_task_acceptance import (
        TaskRunAcceptanceRequest,
        validate_task_artifacts,
    )

    workspace = tmp_path / "task"
    wrong_dir = workspace / "data" / "outputs" / "table_report"
    wrong_dir.mkdir(parents=True)
    workbook_path = wrong_dir / "table_report.xlsx"
    wb = Workbook()
    wb.active["A1"] = "记录名"
    wb.save(workbook_path)
    expected = tmp_path / "expected_artifacts.json"
    expected.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "artifact_id": "table_report_workbook",
                        "kind": "xlsx",
                        "preferred_path": "outputs/table_report/table_report.xlsx",
                        "required": True,
                        "validation_contract": {"validator": "spreadsheet_acceptance"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = validate_task_artifacts(
        TaskRunAcceptanceRequest(
            expected_artifacts_path=expected,
            task_workspace=workspace,
            report_path=tmp_path / "acceptance_report.json",
        )
    )
    findings = report.artifacts[0].report["findings"]

    assert any(item["code"] == "ARTIFACT_MISSING" for item in findings)
    candidates = [item for item in findings if item["code"] == "ARTIFACT_CANDIDATE_PATH"]
    assert candidates
    assert str(Path(candidates[0]["value"]).relative_to(workspace)) == "data/outputs/table_report/table_report.xlsx"


def test_real_task_acceptance_rejects_invalid_candidate_artifact(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_task_acceptance import (
        TaskRunAcceptanceRequest,
        validate_task_artifacts,
    )
    from agent_py_agent.tests.support.xlsx_fixtures import write_xlsx_fixture

    workspace = tmp_path / "task"
    write_xlsx_fixture(workspace, 
        {
            "path": "data/outputs/table_report/table_report.xlsx",
            "sheets": [{"name": "summary", "rows": [{"记录名": "demo"}]}],
        }
    )
    expected = tmp_path / "expected_artifacts.json"
    _write_candidate_expected_artifacts(expected)

    report = validate_task_artifacts(
        TaskRunAcceptanceRequest(
            expected_artifacts_path=expected,
            task_workspace=workspace,
            report_path=tmp_path / "acceptance_report.json",
        )
    )
    findings = report.artifacts[0].report["findings"]
    codes = {item["code"] for item in findings}

    assert "ARTIFACT_CANDIDATE_REJECTED" in codes
    assert "ARTIFACT_CANDIDATE_PATH" not in codes


def test_real_task_acceptance_ignores_open_session_for_accepted_target(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_task_acceptance import (
        TaskRunAcceptanceRequest,
        validate_task_artifacts,
    )
    from agent_py_agent.tests.support.xlsx_fixtures import write_xlsx_fixture

    workspace = tmp_path / "task"
    write_xlsx_fixture(workspace, 
        {
            "path": "outputs/table_report/table_report.xlsx",
            "sheets": [
                {"name": "summary", "rows": [{"记录名": "demo", "地址": "https://example.com"}]},
                {"name": "details", "rows": [{"记录名": "demo", "地址": "https://example.com"}]},
            ],
        }
    )
    workbook = workspace / "outputs/table_report/table_report.xlsx"
    _write_open_session_target_manifest(
        workspace,
        session_id="session-workbook",
        relative_target="outputs/table_report/table_report.xlsx",
        resolved_target=workbook,
    )
    expected = tmp_path / "expected_artifacts.json"
    _write_candidate_expected_artifacts(expected)

    report = validate_task_artifacts(
        TaskRunAcceptanceRequest(
            expected_artifacts_path=expected,
            task_workspace=workspace,
            report_path=tmp_path / "acceptance_report.json",
        )
    )

    assert report.ok is True
    assert report.runtime_findings == []
    assert report.artifacts[0].ok is True


def _write_candidate_expected_artifacts(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "artifact_id": "table_report_workbook",
                        "kind": "xlsx",
                        "preferred_path": "outputs/table_report/table_report.xlsx",
                        "required": True,
                        "validation_contract": {
                            "validator": "spreadsheet_acceptance",
                            "required_columns": ["记录名", "地址"],
                            "required_sheets_min": 2,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_real_task_acceptance_validates_staged_checkpoints(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_task_acceptance import (
        TaskRunAcceptanceRequest,
        validate_task_artifacts,
    )

    workspace = tmp_path / "task"
    stage_dir = workspace / "outputs" / "table_report"
    stage_dir.mkdir(parents=True)
    (stage_dir / "source_data.json").write_text(json.dumps({"top10": []}), encoding="utf-8")
    expected = _write_staged_expected_artifacts(tmp_path)

    report = validate_task_artifacts(
        TaskRunAcceptanceRequest(
            expected_artifacts_path=expected,
            task_workspace=workspace,
            report_path=tmp_path / "acceptance_report.json",
        )
    )
    codes = {item["code"] for item in report.runtime_findings}

    assert "STAGED_JSON_NO_ROWS" in codes


def test_real_task_acceptance_treats_skeleton_only_staged_json_as_no_rows(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_task_acceptance import (
        TaskRunAcceptanceRequest,
        validate_task_artifacts,
    )

    workspace = tmp_path / "task"
    stage_dir = workspace / "outputs" / "table_report"
    stage_dir.mkdir(parents=True)
    (stage_dir / "source_data.json").write_text(
        json.dumps(
            {
                "generated_date": "2026-05-20",
                "sheets": [{"week": "2026-W01", "projects": []}],
                "metadata": {"total_weeks": 20},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    expected = _write_staged_expected_artifacts(tmp_path)

    report = validate_task_artifacts(
        TaskRunAcceptanceRequest(
            expected_artifacts_path=expected,
            task_workspace=workspace,
            report_path=tmp_path / "acceptance_report.json",
        )
    )
    codes = {item["code"] for item in report.runtime_findings}

    assert "STAGED_JSON_NO_ROWS" in codes


def test_real_task_acceptance_reports_invalid_staged_json(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_task_acceptance import (
        TaskRunAcceptanceRequest,
        validate_task_artifacts,
    )

    workspace = tmp_path / "task"
    stage_dir = workspace / "outputs" / "table_report"
    stage_dir.mkdir(parents=True)
    (stage_dir / "source_data.json").write_text('[{"记录名":"demo","语言":"Py', encoding="utf-8")
    expected = _write_staged_expected_artifacts(tmp_path)

    report = validate_task_artifacts(
        TaskRunAcceptanceRequest(
            expected_artifacts_path=expected,
            task_workspace=workspace,
            report_path=tmp_path / "acceptance_report.json",
        )
    )
    finding = next(item for item in report.runtime_findings if item["code"] == "STAGED_JSON_INVALID")

    assert "parse_error" in finding
    assert finding["stage_ref"] == "outputs/table_report/source_data.json"


def _write_staged_expected_artifacts(tmp_path):
    expected = tmp_path / "expected_artifacts.json"
    expected.write_text(json.dumps({"artifacts": [_staged_workbook_artifact()]}), encoding="utf-8")
    return expected


def _staged_workbook_artifact():
    return {
        "artifact_id": "table_report_workbook",
        "kind": "xlsx",
        "preferred_path": "outputs/table_report/table_report.xlsx",
        "required": True,
        "validation_contract": {
            "validator": "spreadsheet_acceptance",
            "staging_contract": {
                "strategy": "data_then_tool_builder_then_workbook",
                "builder_tool": "write_file",
                "source_json_ref": "outputs/table_report/source_data.json",
                "workbook_ref": "outputs/table_report/table_report.xlsx",
                "checkpoint_refs": [
                    "outputs/table_report/source_data.json",
                    "outputs/table_report/table_report.xlsx",
                ]
            },
        },
    }


def _write_open_session_target_manifest(
    task_workspace: Path,
    session_id: str,
    relative_target: str,
    resolved_target: Path,
) -> Path:
    path = task_workspace / ".agent_write_files" / session_id / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "status": "open",
                "target_path": {
                    "raw": relative_target,
                    "resolved": str(resolved_target.resolve()),
                    "display": relative_target,
                },
                "chunks": {"0": {"index": 0}},
            }
        ),
        encoding="utf-8",
    )
    return path
