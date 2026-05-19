from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.contracts.artifact_acceptance import validate_artifact
from agent_py_agent.agent.contracts.artifact_acceptance_models import ArtifactAcceptanceRequest
from agent_py_agent.agent.tooling.spreadsheet_builder import DataWorkbookTool


# LLM: xlsx acceptance must validate structured workbook schema, not only zip integrity.
# 函数用途: 验证 required_columns 和 required_sheets_min 会真实参与 xlsx 机器验收。
def test_xlsx_acceptance_checks_required_columns_and_sheet_count(tmp_path: Path) -> None:
    tool = DataWorkbookTool(tmp_path)
    result = tool.execute(
        {
            "path": "report.xlsx",
            "sheets": [
                {"name": "summary", "rows": [{"项目名": "demo", "地址": "https://example.com"}]},
                {"name": "detail", "rows": [{"项目名": "demo", "上升 star 数": 42}]},
            ],
        }
    )
    assert result.ok

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=tmp_path / "report.xlsx",
            workspace_root=tmp_path,
            validation_contract={
                "required_sheets_min": 2,
                "required_columns": ["项目名", "地址", "上升 star 数"],
            },
        )
    )

    assert report.ok, report.to_dict()


# LLM: xlsx acceptance should reject workbook packages that lack declared schema fields.
# 函数用途: 验证缺少必需列时返回稳定 finding，避免错表通过真实 E2E。
def test_xlsx_acceptance_rejects_missing_required_columns(tmp_path: Path) -> None:
    tool = DataWorkbookTool(tmp_path)
    result = tool.execute(
        {
            "path": "report.xlsx",
            "sheets": [{"name": "summary", "rows": [{"项目名": "demo"}]}],
        }
    )
    assert result.ok

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=tmp_path / "report.xlsx",
            workspace_root=tmp_path,
            validation_contract={
                "required_sheets_min": 2,
                "required_columns": ["项目名", "地址"],
            },
        )
    )
    codes = {finding.code for finding in report.findings}

    assert not report.ok
    assert "XLSX_TOO_FEW_SHEETS" in codes
    assert "XLSX_MISSING_REQUIRED_COLUMNS" in codes
