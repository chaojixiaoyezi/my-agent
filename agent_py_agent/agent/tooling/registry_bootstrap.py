
from __future__ import annotations

from typing import Any

from .artifact import ReadArtifactTool
from .controlled_exec import ControlledExecTool
from .delivery_acceptance import SubmitForAcceptanceTool
from .filesystem import (
    ApplyPatchTool,
    FindFilesTool,
    ListFilesTool,
    ReadFileTool,
    SearchTextTool,
    WriteFileTool,
    WriteFileToolOptions,
    filesystem_access_options,
)
from .models import HybridToolRetriever, KeywordToolSearchProvider, VectorToolSearchProvider
from .shell import ShellTool, ShellToolOptions
from .web import WebFetchTool
from .web_search import WebSearchTool


def build_tool_retriever(params: Any) -> HybridToolRetriever:
    return HybridToolRetriever(
        [
            KeywordToolSearchProvider(),
            VectorToolSearchProvider(enabled=params.vector_search_enabled),
        ]
    )


def register_base_tools(registry: Any, params: Any) -> None:
    _register_filesystem_tools(registry, params)
    _register_network_tools(registry, params)
    registry.register(SubmitForAcceptanceTool())
    _register_security_tools(registry)


def _register_filesystem_tools(registry: Any, params: Any) -> None:
    workspace_roots = registry.workspace_roots
    access_options = filesystem_access_options(
        path_access_mode=params.path_access_mode,
        path_dangerous_roots=params.path_dangerous_roots,
    )
    registry.register(ListFilesTool(registry.workspace_root, params.max_entries, workspace_roots, access_options))
    registry.register(FindFilesTool(registry.workspace_root, params.max_matches, workspace_roots, access_options))
    registry.register(ReadFileTool(registry.workspace_root, params.max_chars, workspace_roots, access_options))
    registry.register(SearchTextTool(registry.workspace_root, params.max_matches, workspace_roots, access_options))
    registry.register(
        ReadArtifactTool(
            getattr(params, "artifact_root", None) or registry.workspace_root,
            artifact_read_budget_window_seconds=params.artifact_read_budget_window_seconds,
            artifact_read_budget_max_chars=params.artifact_read_budget_max_chars,
            default_read_chars=params.artifact_default_read_chars,
        )
    )
    registry.register(
        WriteFileTool(
            registry.workspace_root,
            workspace_roots,
            WriteFileToolOptions(
                max_inline_content_chars=params.tool_write_inline_max_chars,
                access_options=access_options,
                runtime_fact_roots=_runtime_fact_roots(registry, params),
            ),
        )
    )
    registry.register(ApplyPatchTool(registry.workspace_root, workspace_roots, access_options))


def _register_network_tools(registry: Any, params: Any) -> None:
    registry.register(WebSearchTool(max_results=params.max_matches, timeout=params.http_timeout))
    registry.register(WebFetchTool(max_chars=params.web_max_chars, timeout=params.http_timeout))
    registry.register(
        ShellTool(
            registry.workspace_root,
            options=ShellToolOptions(
                workspace_roots=registry.workspace_roots,
                path_access_mode=params.path_access_mode,
                path_dangerous_roots=params.path_dangerous_roots,
                access_mode=params.access_mode,
                default_timeout=params.shell_tool_timeout,
                max_output_chars=params.shell_tool_output_max_chars,
            ),
        )
    )
    # controlled_exec is kept as a legacy/internal tool for existing capability
    # flows, but ToolRegistry hides it from the default model-facing catalog.
    registry.register(ControlledExecTool())


def _register_security_tools(registry: Any) -> None:
    from ..log_analysis.tools import (
        SecurityHuntIpTool,
        SecurityQueryTool,
        SecurityTraceCaseTool,
    )

    registry.register(SecurityQueryTool(registry.workspace_root))
    registry.register(SecurityHuntIpTool(registry.workspace_root))
    registry.register(SecurityTraceCaseTool(registry.workspace_root))


def _runtime_fact_roots(registry: Any, params: Any) -> list[Any]:
    roots = [*(getattr(params, "runtime_fact_roots", None) or []), getattr(params, "artifact_root", None), registry.workspace_root]
    result: list[Any] = []
    seen: set[str] = set()
    for root in roots:
        if not root:
            continue
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        result.append(root)
    return result


__all__ = ["build_tool_retriever", "register_base_tools"]
