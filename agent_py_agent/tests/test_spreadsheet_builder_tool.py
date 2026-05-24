from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

from agent_py_agent.agent.contracts.artifact_acceptance import validate_artifact
from agent_py_agent.agent.contracts.artifact_acceptance_models import ArtifactAcceptanceRequest
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.tooling.spreadsheet_builder import DataWorkbookTool


# LLM: test_data_workbook_tool_builds_xlsx_from_json_rows protects the generic data-to-xlsx contract.
# 函数用途: 验证模型只要写结构化 JSON 数据，就能交给工具稳定生成 xlsx，不必现场写大脚本。
def test_data_workbook_tool_builds_xlsx_from_json_rows(tmp_path: Path) -> None:
    source = tmp_path / "outputs" / "research" / "source_data.json"
    source.parent.mkdir(parents=True)
    source.write_text(
        json.dumps(
            {
                "top_projects": [
                    {"记录名": "demo-a", "地址": "https://example.com/a", "指标值": 120},
                    {"记录名": "demo-b", "地址": "https://example.com/b", "指标值": 90},
                ],
                "summary": {"ignored_scalar": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tool = DataWorkbookTool(tmp_path)

    result = tool.execute(
        {
            "source_json_path": "outputs/research/source_data.json",
            "path": "outputs/research/report.xlsx",
        }
    )

    assert result.ok, result.output
    workbook = tmp_path / "outputs" / "research" / "report.xlsx"
    assert workbook.exists()
    assert validate_artifact(
        ArtifactAcceptanceRequest(path=workbook, workspace_root=tmp_path)
    ).ok
    with ZipFile(workbook) as archive:
        assert "xl/workbook.xml" in archive.namelist()
        assert "xl/worksheets/sheet1.xml" in archive.namelist()
        sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "demo-a" in sheet_xml
    assert "指标值" in sheet_xml


# LLM: test_data_workbook_tool_rejects_empty_structured_data keeps empty checkpoints from passing.
# 函数用途: 验证源 JSON 没有非空表格数据时，工具返回稳定错误码供恢复流程识别。
def test_data_workbook_tool_rejects_empty_structured_data(tmp_path: Path) -> None:
    source = tmp_path / "empty.json"
    source.write_text(json.dumps({"top_projects": []}), encoding="utf-8")
    tool = DataWorkbookTool(tmp_path)

    result = tool.execute({"source_json_path": "empty.json", "path": "out.xlsx"})

    assert not result.ok
    assert result.error_code == "SPREADSHEET_SOURCE_NO_ROWS"
    assert "SPREADSHEET_SOURCE_NO_ROWS" in result.output


# LLM: test_tool_registry_registers_data_workbook_tool keeps the tool visible to main-agent tasks.
# 函数用途: 验证主代理工具注册表默认带 data_to_workbook，复杂表格任务能被工具检索到。
def test_tool_registry_registers_data_workbook_tool(tmp_path: Path) -> None:
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=1000,
            max_entries=50,
            max_matches=20,
            web_max_chars=1000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=10,
            vector_search_enabled=False,
        )
    )

    assert "data_to_workbook" in registry.tools
    hits = registry.find_relevant_specs("整理数据生成 xlsx 表格")
    assert any(spec.name == "data_to_workbook" for spec in hits)
