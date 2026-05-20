from __future__ import annotations

import json
from pathlib import Path


def test_task_acceptance_ignores_open_session_for_accepted_target(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_task_acceptance import (
        TaskRunAcceptanceRequest,
        validate_task_artifacts,
    )
    from agent_py_agent.agent.tooling.spreadsheet_builder import DataWorkbookTool

    workspace = tmp_path / "task"
    DataWorkbookTool(workspace).execute(
        {
            "path": "outputs/github_star_growth/github_star_growth.xlsx",
            "sheets": [
                {"name": "summary", "rows": [{"项目名": "demo", "地址": "https://example.com"}]},
                {"name": "details", "rows": [{"项目名": "demo", "地址": "https://example.com"}]},
            ],
        }
    )
    workbook = workspace / "outputs/github_star_growth/github_star_growth.xlsx"
    _write_open_session_target_manifest(
        workspace,
        session_id="session-workbook",
        relative_target="outputs/github_star_growth/github_star_growth.xlsx",
        resolved_target=workbook,
    )
    expected = tmp_path / "expected_artifacts.json"
    expected.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "artifact_id": "github_star_growth_workbook",
                        "kind": "xlsx",
                        "preferred_path": "outputs/github_star_growth/github_star_growth.xlsx",
                        "required": True,
                        "validation_contract": {
                            "validator": "spreadsheet_acceptance",
                            "required_columns": ["项目名", "地址"],
                            "required_sheets_min": 2,
                        },
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
    assert report.runtime_findings == []
    assert report.artifacts[0].ok is True


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
