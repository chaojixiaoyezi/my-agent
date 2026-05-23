from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.contracts.artifact_acceptance import validate_artifact
from agent_py_agent.agent.contracts.artifact_acceptance_models import ArtifactAcceptanceRequest
from agent_py_agent.agent.tooling.document_pdf_builder import MarkdownPdfTool
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams


# LLM: MarkdownPdfTool is the generic document builder; tests assert machine refs, not task prose.
# 函数用途: 验证 Markdown 文档可以通过通用工具生成可验收 PDF。
def test_markdown_pdf_tool_builds_pdf_from_markdown(tmp_path: Path) -> None:
    source = tmp_path / "outputs" / "documents" / "report.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 标题\n\n这是中文正文。\n\n- 来源: https://example.com/doc\n", encoding="utf-8")
    tool = MarkdownPdfTool(tmp_path)

    result = tool.execute(
        {
            "source_markdown_path": "outputs/documents/report.md",
            "path": "outputs/documents/report.pdf",
            "title": "文档报告",
        }
    )

    assert result.ok, result.output
    pdf = tmp_path / "outputs" / "documents" / "report.pdf"
    assert pdf.exists()
    assert validate_artifact(ArtifactAcceptanceRequest(path=pdf, workspace_root=tmp_path)).ok
    assert result.result_envelope["source_ref"] == "outputs/documents/report.md"
    assert result.result_envelope["artifact_ref"] == "outputs/documents/report.pdf"


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
