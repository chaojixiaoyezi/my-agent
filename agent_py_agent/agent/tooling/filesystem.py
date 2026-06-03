

from __future__ import annotations

from typing import Any

from ._filesystem_find import FindFilesTool

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
from ._filesystem_list import ListFilesTool

# Re-export write tools
from ._filesystem_patch import ApplyPatchTool

# Re-export base class and read tools
from ._filesystem_read import (
    FileSystemAccessOptions,
    FileSystemTool,
    ReadFileTool,
    filesystem_access_options,
)
from ._filesystem_search import SearchTextTool
from ._filesystem_write import WriteFileTool, WriteFileToolOptions
from .models import BaseTool, ToolExecutionResult, ToolSpec

__all__ = [
    # Base class
    "BaseTool",
    "ToolExecutionResult",
    "ToolSpec",
    "FileSystemAccessOptions",
    "FileSystemTool",
    "filesystem_access_options",
    "FindFilesTool",
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
    "ApplyPatchTool",
    "WriteFileTool",
    "WriteFileToolOptions",
]
