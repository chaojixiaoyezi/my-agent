
# LLM: 保持旧模块路径可用，真实实现放在拆分后的读写模块。
# 模块用途: 文件工具的兼容导入门面。

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
)
from ._filesystem_search import SearchTextTool

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
