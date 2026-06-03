"""tool registry and execution settings."""


from __future__ import annotations

from dataclasses import dataclass, field

from ..path_access_policy import DEFAULT_DANGEROUS_PATH_ROOTS, DEFAULT_PATH_ACCESS_MODE

__all__ = ["DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS", "ToolConfig"]

DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS = 12_000
DEFAULT_COMMAND_ACCESS_MODE = "workspace-write"


@dataclass
class ToolConfig:
    """Tool registry and execution limits."""

    enable_tools: bool = True
    max_tool_rounds: int | None = None
    tool_agent_budget_window_seconds: int | None = None
    tool_agent_budget_max_calls: int | None = None
    tool_artifact_read_budget_window_seconds: int = 600
    tool_artifact_read_budget_max_chars: int = 240_000
    tool_output_externalize_min_chars: int = 1200
    tool_output_preview_chars: int = 500
    tool_payload_max_fields: int = 64
    tool_payload_max_field_name_chars: int = 128
    tool_payload_max_name_chars: int = 128
    tool_payload_parse_error_raw_chars: int = 1000
    tool_read_max_chars: int = 50_000
    tool_write_inline_max_chars: int = DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS
    tool_list_max_entries: int = 200
    tool_search_max_matches: int = 50
    tool_web_max_chars: int = 100_000
    tool_http_timeout: int = 30
    path_access_mode: str = DEFAULT_PATH_ACCESS_MODE
    path_dangerous_roots: list[str] = field(default_factory=lambda: list(DEFAULT_DANGEROUS_PATH_ROOTS))
    access_mode: str = DEFAULT_COMMAND_ACCESS_MODE
    tool_shell_timeout: int = 240
    tool_shell_output_max_chars: int = 12_000
    stream_enabled: bool = True
    tool_catalog_limit: int = 20
    tool_catalog_mode: str = "compact"
    tool_catalog_offset: int = 0
    tool_catalog_categories: list[str] = field(default_factory=list)
    # Default prompt catalog stays compact: examples and long parameter notes
    # remain available through list_tools or the recommended-tool details.
    tool_catalog_include_examples: bool = False
    tool_catalog_entry_max_chars: int = 700
    tool_catalog_show_truncated_notice: bool = True
    tool_detail_max_chars: int = 4000
    tool_retrieval_limit: int = 3
    tool_vector_search_enabled: bool = True
