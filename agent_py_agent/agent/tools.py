# LLM: Agent package module; keep public imports and cross-module compatibility stable.
# 模块用途: 提供 agent 核心功能的一部分，对外暴露稳定入口或兼容转发。

from __future__ import annotations

"""compatibility facade for the split `agent.tooling` package.

给人看的解释：
真实工具代码现在已经拆到 `agent_py_agent.agent.tooling` 目录里。
这个文件暂时保留老入口，避免以前写的 `from agent_py_agent.agent.tools import ToolRegistry` 立刻失效。
新代码建议直接按职责导入 `agent.tooling` 里的模块。
"""

from .tooling import (
    WRITE_TOOL_NAMES,
    ApplyPatchTool,
    BaseTool,
    BaseToolSearchProvider,
    FetchUrlTool,
    FileSystemTool,
    FindFilesTool,
    HttpRequestTool,
    HybridToolRetriever,
    KeywordToolSearchProvider,
    ListFilesTool,
    ReadArtifactTool,
    ReadFileTool,
    SearchTextTool,
    ToolExecutionResult,
    ToolRegistry,
    ToolRegistryParams,
    ToolSearchHit,
    ToolSpec,
    VectorToolSearchProvider,
    WriteFileTool,
    validate_write_boundary,
)
from .tooling.models import _tokenize
from .tooling.parser import (
    _decode_xmlish_parameter_value,
    _normalize_xmlish_parameter_name,
    _normalize_xmlish_tool_name,
    _parse_xmlish_tool_call_body,
)
from .tooling.parser import (
    parse_xmlish_tool_calls as _parse_xmlish_tool_calls,
)
from .tooling.registry import _allowed_tool_set
from .tooling.write_boundary import (
    _boundary_paths,
    _display_path,
    _is_relative_to,
    _resolve_boundary_path,
)
from .tooling.write_boundary import (
    validate_write_boundary as _validate_write_boundary,
)

__all__ = [
    "ApplyPatchTool",
    "BaseTool",
    "BaseToolSearchProvider",
    "FetchUrlTool",
    "FileSystemTool",
    "FindFilesTool",
    "HttpRequestTool",
    "HybridToolRetriever",
    "KeywordToolSearchProvider",
    "ListFilesTool",
    "ReadArtifactTool",
    "ReadFileTool",
    "SearchTextTool",
    "ToolExecutionResult",
    "ToolRegistry",
    "ToolSearchHit",
    "ToolSpec",
    "VectorToolSearchProvider",
    "WRITE_TOOL_NAMES",
    "WriteFileTool",
]
