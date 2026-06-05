from __future__ import annotations

import json
from pathlib import Path


def test_task_acceptance_ignores_open_session_for_accepted_target(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_task_acceptance import (
        TaskRunAcceptanceRequest,
        validate_task_artifacts,
    )
    workspace = tmp_path / "task"
    workbook = _write_valid_workbook(workspace)
    _write_open_session_target_manifest(
        workspace,
        session_id="session-workbook",
        relative_target="outputs/table_report/table_report.xlsx",
        resolved_target=workbook,
    )
    expected = _write_expected_artifacts(tmp_path)

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


def test_task_acceptance_allows_user_requested_absolute_output_root(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_task_acceptance import (
        TaskRunAcceptanceRequest,
        validate_task_artifacts,
    )

    workspace = tmp_path / "task"
    requested = tmp_path.parent / f"{tmp_path.name}-requested-output"
    requested.mkdir()
    report_path = requested / "report.md"
    report_path.write_text("finished report with enough detail", encoding="utf-8")
    expected = tmp_path / "expected_artifacts.json"
    expected.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "artifact_id": "user_requested_report",
                        "kind": "md",
                        "preferred_path": str(report_path),
                        "allowed_output_roots": [str(requested)],
                        "required": True,
                        "validation_contract": {"min_size": 10},
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

    assert report.ok is True
    assert report.artifacts[0].ok is True
    findings = report.artifacts[0].report["findings"]
    assert {item["code"] for item in findings} == set()


def _write_valid_workbook(workspace: Path) -> Path:
    from agent_py_agent.tests.support.xlsx_fixtures import write_xlsx_fixture
    write_xlsx_fixture(workspace, 
        {
            "path": "outputs/table_report/table_report.xlsx",
            "sheets": [
                {"name": "summary", "rows": [{"记录名": "demo", "地址": "https://example.com"}]},
                {"name": "details", "rows": [{"记录名": "demo", "地址": "https://example.com"}]},
            ],
        }
    )
    return workspace / "outputs/table_report/table_report.xlsx"


def _write_expected_artifacts(tmp_path: Path) -> Path:
    expected = tmp_path / "expected_artifacts.json"
    expected.write_text(json.dumps({"artifacts": [_workbook_artifact_contract()]}), encoding="utf-8")
    return expected


def _workbook_artifact_contract() -> dict[str, object]:
    return {
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


def _write_open_session_target_manifest(
    task_workspace: Path,
    session_id: str,
    relative_target: str,
    resolved_target: Path,
) -> Path:
    path = task_workspace / ".agent_file_write_sessions" / session_id / "manifest.json"
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
