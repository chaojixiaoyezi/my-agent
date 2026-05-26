# LLM: Registry bootstrap isolates concrete tool wiring from the registry protocol.
# 模块用途: 集中注册内置工具，避免 ToolRegistry 主入口继续膨胀。

from __future__ import annotations

from typing import Any

from .api_json_collection import ApiJsonCollectionTool
from .artifact import ReadArtifactTool
from .controlled_exec import ControlledExecTool
from .delivery_acceptance import SubmitForAcceptanceTool
from .document_pdf_builder import MarkdownPdfTool
from .file_write_session import FileWriteSessionTool
from .filesystem import (
    AppendFileTool,
    ListFilesTool,
    ReadFileTool,
    ReplaceInFileTool,
    SearchTextTool,
    WriteFileTool,
)
from .models import HybridToolRetriever, KeywordToolSearchProvider, VectorToolSearchProvider
from .shell import ShellTool
from .spreadsheet_builder import DataWorkbookTool
from .structured_json_writer import StructuredJsonTool
from .web import FetchUrlTool, HttpRequestTool
from .web_search import WebSearchTool


# LLM: build_tool_retriever centralizes catalog retrieval setup.
# 函数用途: 根据配置创建关键词/向量混合工具检索器，供 ToolRegistry 查询推荐工具。
def build_tool_retriever(params: Any) -> HybridToolRetriever:
    return HybridToolRetriever(
        [
            KeywordToolSearchProvider(),
            VectorToolSearchProvider(enabled=params.vector_search_enabled),
        ]
    )


# LLM: register_base_tools keeps ToolRegistry focused on protocol behavior.
# 函数用途: 按稳定顺序注册文件、网络、shell、表格和安全工具。
def register_base_tools(registry: Any, params: Any) -> None:
    _register_filesystem_tools(registry, params)
    _register_network_tools(registry, params)
    registry.register(SubmitForAcceptanceTool())
    _register_security_tools(registry)


# LLM: _register_filesystem_tools keeps file-tool config in one place.
# 函数用途: 注册文件系统工具，并把用户配置的读取/写入上限传给对应工具。
def _register_filesystem_tools(registry: Any, params: Any) -> None:
    workspace_roots = registry.workspace_roots
    registry.register(ListFilesTool(registry.workspace_root, params.max_entries, workspace_roots))
    registry.register(ReadFileTool(registry.workspace_root, params.max_chars, workspace_roots))
    registry.register(SearchTextTool(registry.workspace_root, params.max_matches, workspace_roots))
    registry.register(
        ReadArtifactTool(
            registry.workspace_root,
            artifact_read_budget_window_seconds=params.artifact_read_budget_window_seconds,
            artifact_read_budget_max_chars=params.artifact_read_budget_max_chars,
            default_read_chars=params.artifact_default_read_chars,
        )
    )
    registry.register(
        WriteFileTool(
            registry.workspace_root,
            workspace_roots,
            max_inline_content_chars=params.tool_write_inline_max_chars,
        )
    )
    registry.register(
        AppendFileTool(
            registry.workspace_root,
            workspace_roots,
            max_inline_content_chars=params.tool_write_inline_max_chars,
        )
    )
    registry.register(FileWriteSessionTool(registry.workspace_root, workspace_roots))
    registry.register(ReplaceInFileTool(registry.workspace_root, workspace_roots))
    registry.register(StructuredJsonTool(registry.workspace_root, workspace_roots))
    registry.register(ApiJsonCollectionTool(registry.workspace_root, workspace_roots, timeout=params.http_timeout))
    registry.register(DataWorkbookTool(registry.workspace_root, workspace_roots))
    registry.register(MarkdownPdfTool(registry.workspace_root, workspace_roots))


# LLM: _register_network_tools isolates non-filesystem tool setup from constructor policy.
# 函数用途: 注册网页、HTTP、shell 和受控执行工具，保持工具初始化顺序稳定。
def _register_network_tools(registry: Any, params: Any) -> None:
    registry.register(WebSearchTool(max_results=params.max_matches, timeout=params.http_timeout))
    registry.register(FetchUrlTool(max_chars=params.web_max_chars, timeout=params.http_timeout))
    registry.register(HttpRequestTool(max_chars=params.web_max_chars, timeout=params.http_timeout))
    registry.register(
        ShellTool(
            registry.workspace_root,
            default_timeout=params.shell_tool_timeout,
            max_output_chars=params.shell_tool_output_max_chars,
        )
    )
    registry.register(ControlledExecTool())


# LLM: _register_security_tools keeps optional security tool registration easy to audit.
# 函数用途: 延迟导入并注册安全分析工具，避免主注册流程继续增长。
def _register_security_tools(registry: Any) -> None:
    from ..log_analysis.tools import (
        SecurityHuntIpTool,
        SecurityQueryTool,
        SecurityTraceCaseTool,
    )

    registry.register(SecurityQueryTool(registry.workspace_root))
    registry.register(SecurityHuntIpTool(registry.workspace_root))
    registry.register(SecurityTraceCaseTool(registry.workspace_root))


__all__ = ["build_tool_retriever", "register_base_tools"]
