from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.contracts.artifact_acceptance import validate_artifact
from agent_py_agent.agent.contracts.artifact_acceptance_models import ArtifactAcceptanceRequest
from agent_py_agent.agent.contracts.staged_checkpoint_acceptance import staged_checkpoint_findings
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


# LLM: Workbook acceptance must reject fact tables whose source JSON has no machine evidence refs.
# 函数用途: 验证 xlsx 即使生成成功，只要 staging source 缺结构化来源证据，也不能通过机器验收。
def test_xlsx_acceptance_rejects_unsourced_staged_source_json(tmp_path: Path) -> None:
    source = tmp_path / "source_data.json"
    source.write_text(
        """
{
  "sheets": [
    {
      "name": "weekly",
      "columns": ["项目名", "地址", "上升 star 数"],
      "rows": [{"项目名": "demo", "地址": "https://example.com/demo", "上升 star 数": 42}]
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )
    tool = DataWorkbookTool(tmp_path)
    result = tool.execute({"path": "report.xlsx", "source_json_path": "source_data.json"})
    assert result.ok

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=tmp_path / "report.xlsx",
            workspace_root=tmp_path,
            validation_contract={
                "required_columns": ["项目名", "地址", "上升 star 数"],
                "staging_contract": {
                    "source_json_ref": "source_data.json",
                },
                "evidence_contract": {
                    "required_fields": ["上升 star 数"],
                    "require_verified": True,
                },
            },
        )
    )

    codes = {finding.code for finding in report.findings}
    assert not report.ok
    assert "EVIDENCE_REQUIRED_FIELD_MISSING" in codes


# LLM: staged checkpoint acceptance must use the same sheet/evidence contract as final artifact checks.
# 函数用途: 验证真实任务 acceptance 的 runtime_findings 不会丢掉 staged source 的结构和证据问题。
def test_staged_checkpoint_findings_use_validation_contract_shape_and_evidence(tmp_path: Path) -> None:
    source = tmp_path / "outputs/github_star_growth/source_data.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        '{"sheets":[{"name":"汇总","rows":[{"项目名":"demo","地址":"https://example.com","上升 star 数":"估算"}]}]}',
        encoding="utf-8",
    )

    findings = staged_checkpoint_findings(
        [
            {
                "preferred_path": "outputs/github_star_growth/github_star_growth.xlsx",
                "validation_contract": {
                    "required_sheets_min": 2,
                    "required_columns": ["项目名", "地址", "上升 star 数"],
                    "staging_contract": {
                        "checkpoint_refs": [
                            "outputs/github_star_growth/source_data.json",
                            "outputs/github_star_growth/github_star_growth.xlsx",
                        ]
                    },
                    "evidence_contract": {"required_fields": ["项目名", "地址", "上升 star 数"], "require_verified": True},
                },
            }
        ],
        tmp_path,
    )

    codes = {finding["code"] for finding in findings}
    assert "STAGED_JSON_TOO_FEW_SHEETS" in codes
    assert "EVIDENCE_REQUIRED_FIELD_MISSING" in codes
