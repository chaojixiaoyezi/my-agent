# LLM: 工具注册的装配点，按注册参数构造内置工具；可信来源合同和语法反馈开关由 core 注入，构造不读私有资源，
#   只注册不执行，权限仍由原动作策略逐调用裁决。
# 模块用途: 把文件、归档读取、命令、网页等内置工具注册进工具表，让来源与可选诊断沿同一权限链连接。
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
from .controlled_exec import ControlledExecTool
from .models import (
    HybridToolRetriever,
    KeywordToolSearchProvider,
    VectorToolSearchProvider,
)
from .process_sessions import ProcessSessionTool
from .pty_sessions import TerminalSessionTool
from .shell import ShellTool, ShellToolOptions
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


# LLM: 基础工具只在这里成批装配。
# 函数用途: 注册所有基础工具。
def register_base_tools(registry: Any, params: Any) -> None:
    _register_filesystem_tools(registry, params)
    _register_network_tools(registry, params)




# LLM: 文件类工具与归档读取工具在这里按注册参数构造；source resolver/schema 和语法开关只由宿主注入，三写入口共用配置；
#   artifact 读取额度只传字符数（窗口固定在工具内），只注册不执行，逐调用仍由 Registry 裁剪目标权限。
# 函数用途: 把列目录、找文件、读写文件、搜索和归档读取等工具注册进工具表，装配路径、配额、同源输入及可选诊断。
def _register_filesystem_tools(registry: Any, params: Any) -> None:
    workspace_roots = registry.workspace_roots
    access_options = filesystem_access_options(
        path_access_mode=params.path_access_mode,
        path_dangerous_roots=params.path_dangerous_roots,
        owner_scope_root=params.owner_scope_root,
        protected_persona_root=params.protected_persona_root,
        owner_quota_max_bytes=getattr(params, "owner_quota_max_bytes", 0),
        owner_quota_policy_available=getattr(params, "owner_quota_policy_available", True),
        enable_file_syntax_diagnostics=params.enable_file_syntax_diagnostics,
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
                source_resolver=getattr(params, "file_source_resolver", None),
                source_ref_schema=getattr(params, "file_source_ref_schema", None),
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
            artifact_backup_root=params.artifact_backup_root,
            access_mode=params.access_mode,
            default_timeout=params.shell_tool_timeout,
            max_output_chars=params.shell_tool_output_max_chars,
            listen_scope_enforce=params.background_process_listen_scope_enforce,
            host_private_roots=params.host_private_roots,
            sandbox_boundary_facts=params.shell_sandbox_boundary_facts,
        ),
    )
    registry.register(shell_tool)
    registry.register(
        ProcessSessionTool(
            params.owner_scope_root,
            registry.workspace_root,
        )
    )
    registry.register(TerminalSessionTool(shell_tool))
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
