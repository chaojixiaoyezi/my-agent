from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.contracts.artifact_acceptance import validate_artifact
from agent_py_agent.agent.contracts.artifact_acceptance_models import ArtifactAcceptanceRequest
from agent_py_agent.tests.support.xlsx_fixtures import write_xlsx_fixture


# LLM: Markdown artifacts should satisfy declared section and size contracts, not just exist.
# 函数用途: 验证 md 产物缺少 validation_contract.required_sections 时会返回结构化 finding。
def test_markdown_artifact_requires_declared_sections_and_size(tmp_path: Path) -> None:
    report_path = tmp_path / "report.md"
    report_path.write_text("# Summary\nshort\n", encoding="utf-8")

    result = validate_artifact(
        ArtifactAcceptanceRequest(
            path=report_path,
            workspace_root=tmp_path,
            validation_contract={"required_sections": ["Summary", "Evidence"], "min_size": 80},
        )
    )

    assert result.ok is True
    assert [item.code for item in result.findings] == [
        "ARTIFACT_TOO_SMALL",
        "MARKDOWN_REQUIRED_SECTION_MISSING",
    ]


# LLM: JSON reports need declared required fields before other agents can trust them.
# 函数用途: 验证 JSON 顶层对象缺少 validation_contract.required_fields 时会失败。
def test_json_artifact_requires_declared_fields(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text('{"risk_level":"low"}', encoding="utf-8")

    result = validate_artifact(
        ArtifactAcceptanceRequest(
            path=report_path,
            workspace_root=tmp_path,
            validation_contract={"required_fields": ["risk_level", "evidence"]},
        )
    )

    assert result.ok is False
    assert [item.code for item in result.findings] == ["JSON_REQUIRED_FIELDS_MISSING"]
    assert result.findings[0].value == "evidence"


# LLM: CSV artifacts need declared columns when a task contract says table shape matters.
# 函数用途: 验证 CSV 表头缺少 validation_contract.required_columns 时会失败。
def test_csv_artifact_requires_declared_columns(tmp_path: Path) -> None:
    csv_path = tmp_path / "report.csv"
    csv_path.write_text("记录名,地址\nA,https://example.test\n", encoding="utf-8")

    result = validate_artifact(
        ArtifactAcceptanceRequest(
            path=csv_path,
            workspace_root=tmp_path,
            validation_contract={"required_columns": ["记录名", "地址", "说明依据"]},
        )
    )

    assert result.ok is False
    assert [item.code for item in result.findings] == ["CSV_REQUIRED_COLUMNS_MISSING"]
    assert result.findings[0].value == "说明依据"


# LLM: XLSX contracts use the same required_columns shape as CSV and staged tabular JSON.
# 函数用途: 验证 xlsx 缺少声明列时沿用已有 workbook XML 验收器。
def test_xlsx_artifact_requires_declared_columns(tmp_path: Path) -> None:
    xlsx_path = tmp_path / "report.xlsx"
    write_xlsx_fixture(
        tmp_path,
        {
            "path": "report.xlsx",
            "sheets": [{"name": "summary", "rows": [{"记录名": "A", "地址": "https://example.test"}]}],
        },
    )

    result = validate_artifact(
        ArtifactAcceptanceRequest(
            path=xlsx_path,
            workspace_root=tmp_path,
            validation_contract={"required_columns": ["记录名", "地址", "说明依据"]},
        )
    )

    assert result.ok is True
    assert [item.code for item in result.findings] == ["XLSX_MISSING_REQUIRED_COLUMNS"]
    assert result.findings[0].value == "说明依据"
