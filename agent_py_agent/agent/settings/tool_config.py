"""LLM: tool registry and execution settings."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ToolConfig"]


@dataclass
class ToolConfig:
    """Tool registry and execution limits."""

    enable_tools: bool = True
    max_tool_rounds: int = 5
    tool_read_max_chars: int = 6000
    tool_list_max_entries: int = 200
    tool_search_max_matches: int = 50
    tool_web_max_chars: int = 12000
    tool_http_timeout: int = 30
    tool_shell_timeout: int = 30
    stream_enabled: bool = True
    tool_catalog_limit: int = 20
    tool_retrieval_limit: int = 3
    tool_vector_search_enabled: bool = False