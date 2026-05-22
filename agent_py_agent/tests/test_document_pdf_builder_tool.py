from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.contracts.artifact_acceptance import validate_artifact
from agent_py_agent.agent.contracts.artifact_acceptance_models import ArtifactAcceptanceRequest
from agent_py_agent.agent.tooling.document_pdf_builder import MarkdownPdfTool
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams


# LLM: MarkdownPdfTool is the generic document builder; tests assert machine refs, not task prose.
# 函数用途: 验证 Markdown 文档可以通过通用工具生成可验收 PDF。
def test_markdown_pdf_tool_builds_pdf_from_markdown(tmp_path: Path) -> None:
    source = tmp_path / "outputs" / "research" / "report.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 标题\n\n这是中文正文。\n\n- 来源: https://example.com/paper\n", encoding="utf-8")
    tool = MarkdownPdfTool(tmp_path)

    result = tool.execute(
        {
            "source_markdown_path": "outputs/research/report.md",
            "path": "outputs/research/report.pdf",
            "title": "研究报告",
        }
    )

    assert result.ok, result.output
    pdf = tmp_path / "outputs" / "research" / "report.pdf"
    assert pdf.exists()
    assert validate_artifact(ArtifactAcceptanceRequest(path=pdf, workspace_root=tmp_path)).ok
    assert result.result_envelope["source_ref"] == "outputs/research/report.md"
    assert result.result_envelope["artifact_ref"] == "outputs/research/report.pdf"


# LLM: Builder tools must fail structurally when the staged markdown source is absent.
# 函数用途: 验证 source_markdown_path 缺失时返回稳定错误码，供恢复链路识别。
def test_markdown_pdf_tool_rejects_missing_source(tmp_path: Path) -> None:
    tool = MarkdownPdfTool(tmp_path)

    result = tool.execute({"source_markdown_path": "missing.md", "path": "out/report.pdf"})

    assert not result.ok
    assert result.error_code == "MARKDOWN_SOURCE_MISSING"


# LLM: Tool registry visibility is the bridge from recovery action to executable builder.
# 函数用途: 验证主代理默认能检索到 markdown_to_pdf，不需要临场写脚本。
def test_tool_registry_registers_markdown_pdf_tool(tmp_path: Path) -> None:
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

    assert "markdown_to_pdf" in registry.tools
    hits = registry.find_relevant_specs("把 markdown 文档生成 pdf")
    assert any(spec.name == "markdown_to_pdf" for spec in hits)


# LLM: Document startup contracts must carry markdown source/output refs for the builder tool.
# 函数用途: 验证 PDF 阶段构建动作不会再只支持 workbook 字段，避免 source_ref/output_ref 为空。
def test_research_pdf_startup_action_uses_markdown_source_and_pdf_output(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.main_agent_real_task_suite import (
        MainAgentRealTaskSuiteRequest,
        plan_main_agent_real_task_suite,
    )
    from agent_py_agent.agent.contracts.main_agent_task_execution_files import (
        case_paths,
        command_for_case,
    )
    from agent_py_agent.agent.contracts.main_agent_task_execution_models import (
        MainAgentTaskExecutionRequest,
    )
    suite = plan_main_agent_real_task_suite(MainAgentRealTaskSuiteRequest(workspace=tmp_path, max_workers=1))
    case = next(item for item in suite.cases if item.case_id == "research_documents_translation_pdf")
    paths = case_paths(tmp_path, case.case_id)
    command_for_case(
        case,
        MainAgentTaskExecutionRequest(workspace=tmp_path),
        config_path=tmp_path / "config.yaml",
        workspace=tmp_path,
        delivery_contract_path=paths["delivery_contract"],
    )
    payload = json.loads(paths["delivery_contract"].read_text(encoding="utf-8"))
    builder_actions = [
        item for item in payload["bootstrap_contract"]["startup_actions"] if item.get("action") == "invoke_builder_tool"
    ]

    assert builder_actions == [
        {
            "action": "invoke_builder_tool",
            "priority": 2,
            "builder_tool": "markdown_to_pdf",
            "source_ref": "outputs/research_documents/research_documents_zh.md",
            "output_ref": "outputs/research_documents/research_documents_zh.pdf",
        }
    ]


# LLM: research translation tasks need collection completeness and mapping contracts.
# 函数用途: 验证 PDF 任务不再只要求一个 PDF，而要求 source index、完整性证据和正文映射。
def test_research_pdf_case_has_collection_completeness_contract(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.main_agent_real_task_suite import (
        MainAgentRealTaskSuiteRequest,
        plan_main_agent_real_task_suite,
    )

    suite = plan_main_agent_real_task_suite(MainAgentRealTaskSuiteRequest(workspace=tmp_path, max_workers=1))
    case = next(item for item in suite.cases if item.case_id == "research_documents_translation_pdf")
    artifacts = json.loads((tmp_path / case.expected_artifacts_ref).read_text(encoding="utf-8"))
    contract = artifacts["artifacts"][0]["validation_contract"]["collection_contract"]

    assert contract["source_json_ref"] == "outputs/research_documents/source_index.json"
    assert contract["items_path"] == "rows"
    assert contract["min_items_total"] >= 3
    assert contract["required_item_values"] == {"translated": True}
    assert contract["require_completion_evidence"] is True
    assert contract["require_item_evidence"] is True
    assert contract["required_item_evidence_fields"] == ["title", "url", "date"]
    assert contract["mapping"]["artifact_ref"] == "outputs/research_documents/research_documents_zh.md"
    assert contract["mapping"]["key_fields"] == ["title"]
