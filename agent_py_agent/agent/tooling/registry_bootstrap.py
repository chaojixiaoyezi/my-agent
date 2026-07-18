from __future__ import annotations

from typing import Any

from ._filesystem_edit import EditFileTool
from ._filesystem_find import FindFilesTool
from ._filesystem_list import ListFilesTool
from ._filesystem_patch import ApplyPatchTool
from ._filesystem_read import ReadFileTool, filesystem_access_options
from ._filesystem_search import SearchTextTool
from ._filesystem_write import WriteFileTool, WriteFileToolOptions
from .artifact import ReadArtifactTool
from .browser_tools import browser_tools
from .capabilities_tool import ListCapabilitiesTool
from .controlled_exec import ControlledExecTool
from .lsp_client import LspTool
from .models import (
    HybridToolRetriever,
    KeywordToolSearchProvider,
    VectorToolSearchProvider,
)
from .process_tools import KillProcessTool, ListProcessesTool, ProcessStatusTool
from .pty_sessions import TerminalSessionTool
from .shell import ShellTool, ShellToolOptions
from .vision_tools import AnalyzeImageTool, VisionModelConfig
from .web import WebFetchTool
from .web_search import WebSearchTool

# LLM: 本模块集中注册基础工具；能力清单必须惰性读取同一个 registry，不能维护平行静态工具列表。
# 模块用途: 把文件、网络、视觉、终端和自我能力工具装入同一 ToolRegistry。


# LLM: retriever 的关键词与向量通道顺序是工具发现合同；embedder 缺失时向量通道应明确降级而非虚报。
# 函数用途: 创建基础工具检索器，让模型按关键词和可选向量检索当前注册工具。
def build_tool_retriever(params: Any) -> HybridToolRetriever:
    return HybridToolRetriever(
        [
            KeywordToolSearchProvider(),
            VectorToolSearchProvider(
                enabled=params.vector_search_enabled,
                embedder=getattr(params, "tool_embedder", None),
            ),
        ]
    )


# LLM: 基础工具只在这里成批装配；ListCapabilitiesTool 通过 provider 读取注册完成后的同一 registry 状态。
# 函数用途: 注册所有基础工具，并让能力自我描述看到当前 Agent 的真实配置和工具集合。
def register_base_tools(registry: Any, params: Any) -> None:
    _register_filesystem_tools(registry, params)
    _register_network_tools(registry, params)
    _register_vision_tools(registry, params)
    registry.register(
        ListCapabilitiesTool(
            config=getattr(params, "capability_config", None),
            tool_names_provider=lambda: set(registry.tools),
            channel_registry=getattr(params, "channel_registry", None),
            channel_binding_provider=getattr(params, "channel_binding_provider", None),
            skill_snapshot_provider=getattr(params, "skill_snapshot_provider", None),
            memory_snapshot_provider=getattr(params, "memory_snapshot_provider", None),
            persona_snapshot_provider=getattr(params, "persona_snapshot_provider", None),
            scheduler_snapshot_provider=getattr(params, "scheduler_snapshot_provider", None),
        )
    )


def _register_vision_tools(registry: Any, params: Any) -> None:
    """注册看图工具 analyze_image(短板6 视觉理解)。

    视觉是可选加法:vision_config 为 None 时给一个空 VisionModelConfig,工具照常注册,
    但调用时返回 TOOL_UNAVAILABLE 带配置指引——没配视觉模型对现有流程零影响、不崩。
    """
    vision_config = getattr(params, "vision_config", None) or VisionModelConfig()
    registry.register(AnalyzeImageTool(vision_config))


def _register_filesystem_tools(registry: Any, params: Any) -> None:
    workspace_roots = registry.workspace_roots
    access_options = filesystem_access_options(
        path_access_mode=params.path_access_mode,
        path_dangerous_roots=params.path_dangerous_roots,
        owner_scope_root=params.owner_scope_root,
        protected_persona_root=params.protected_persona_root,
        owner_quota_max_bytes=getattr(params, "owner_quota_max_bytes", 0),
        owner_quota_policy_available=getattr(params, "owner_quota_policy_available", True),
    )
    registry.register(
        ListFilesTool(registry.workspace_root, params.max_entries, workspace_roots, access_options)
    )
    registry.register(
        FindFilesTool(registry.workspace_root, params.max_matches, workspace_roots, access_options)
    )
    registry.register(
        ReadFileTool(registry.workspace_root, params.max_chars, workspace_roots, access_options)
    )
    registry.register(
        SearchTextTool(registry.workspace_root, params.max_matches, workspace_roots, access_options)
    )
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
    registry.register(EditFileTool(registry.workspace_root, workspace_roots, access_options))


def _register_network_tools(registry: Any, params: Any) -> None:
    registry.register(WebSearchTool(max_results=params.max_matches, timeout=params.http_timeout))
    registry.register(WebFetchTool(max_chars=params.web_max_chars, timeout=params.http_timeout))
    shell_tool = ShellTool(
        registry.workspace_root,
        options=ShellToolOptions(
            workspace_roots=registry.workspace_roots,
            path_access_mode=params.path_access_mode,
            path_dangerous_roots=params.path_dangerous_roots,
            owner_scope_root=params.owner_scope_root,
            protected_persona_root=params.protected_persona_root,
            access_mode=params.access_mode,
            default_timeout=params.shell_tool_timeout,
            max_output_chars=params.shell_tool_output_max_chars,
        ),
    )
    registry.register(shell_tool)
    registry.register(TerminalSessionTool(shell_tool))
    lsp_tool = LspTool(
        registry.workspace_root,
        registry.workspace_roots,
        params.owner_scope_root,
        getattr(params, "lsp_servers", None),
    )
    registry.register(lsp_tool)
    registry._lsp_manager = lsp_tool.manager
    # 后台进程管理:管住 run_command(run_in_background=true) 起的后台进程
    # (注册表 + 列表/查状态/杀进程组),让模型不再只剩日志文件管不了进程。
    registry.register(ListProcessesTool())
    registry.register(ProcessStatusTool())
    registry.register(KillProcessTool())
    # 浏览器自动化:补 web_fetch 抓不到的 JS 渲染/SPA/需点击填表的动态页面
    # (惰性启动 headless Chromium,导航走 SSRF 防护,a11y 快照给无视觉模型用)。
    for browser_tool in browser_tools():
        registry.register(browser_tool)
    # controlled_exec is an internal tool used by capability grants.
    # flows, but ToolRegistry hides it from the default model-facing catalog.
    registry.register(ControlledExecTool())


def _runtime_fact_roots(registry: Any, params: Any) -> list[Any]:
    roots = [
        *(getattr(params, "runtime_fact_roots", None) or []),
        getattr(params, "artifact_root", None),
        registry.workspace_root,
    ]
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
