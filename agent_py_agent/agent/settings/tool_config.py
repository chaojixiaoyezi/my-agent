"""tool registry and execution settings."""

# LLM: 这些上限保护 prompt 预算和外部调用，放宽前确认调用面。
# 模块用途: 工具读取、检索、HTTP 和目录展示上限的配置模型。

from __future__ import annotations

from dataclasses import dataclass, field

from ..path_access_policy import DEFAULT_DANGEROUS_PATH_ROOTS, DEFAULT_PATH_ACCESS_MODE

__all__ = ["DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS", "ToolConfig"]

DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS = 12_000
DEFAULT_COMMAND_ACCESS_MODE = "workspace-write"


# LLM: ToolConfig 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: ToolConfig 配置模型，保存 配置系统 的默认值和可调参数。
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
    tool_catalog_include_examples: bool = True
    tool_catalog_entry_max_chars: int = 1200
    tool_catalog_show_truncated_notice: bool = True
    tool_detail_max_chars: int = 4000
    tool_retrieval_limit: int = 3
    tool_vector_search_enabled: bool = True
