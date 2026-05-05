"""LLM: implements workspace-scoped filesystem tools with path containment checks.

给人看的解释：
这个文件只负责"读写本地工作区文件"。
模型想看目录、读文件、搜索文字、写文件、追加内容、局部替换内容，都会走这里。
最重要的安全规则也在这里：路径必须待在 workspace 里面，不能偷偷跑到项目外。
"""

from __future__ import annotations

from typing import Any

# Re-export helpers for backwards compatibility
from ._filesystem_helpers import (
    _MAX_PATH_CHARS,
    _MAX_SEARCH_LINE_CHARS,
    _MAX_SEARCH_QUERY_CHARS,
    _MAX_WRITE_TEXT_CHARS,
    _bool_param,
    _has_control_chars,
    _int_param,
    _optional_path,
    _parse_count_param,
    _read_text_safe,
    _required_path,
    _text_param,
)

# Re-export base class and read tools
from ._filesystem_read import (
    FileSystemTool,
    ListFilesTool,
    ReadFileTool,
    SearchTextTool,
)

# Re-export write tools
from ._filesystem_write import (
    AppendFileTool,
    ReplaceInFileTool,
    WriteFileTool,
)
from .models import BaseTool, ToolExecutionResult, ToolSpec

__all__ = [
    # Base class
    "BaseTool",
    "ToolExecutionResult",
    "ToolSpec",
    "FileSystemTool",
    # Helpers
    "_bool_param",
    "_has_control_chars",
    "_int_param",
    "_optional_path",
    "_parse_count_param",
    "_read_text_safe",
    "_required_path",
    "_text_param",
    "_MAX_PATH_CHARS",
    "_MAX_SEARCH_LINE_CHARS",
    "_MAX_SEARCH_QUERY_CHARS",
    "_MAX_WRITE_TEXT_CHARS",
    # Read tools
    "ListFilesTool",
    "ReadFileTool",
    "SearchTextTool",
    # Write tools
    "AppendFileTool",
    "ReplaceInFileTool",
    "WriteFileTool",
]
