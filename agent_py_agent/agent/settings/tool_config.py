"""tool registry and execution settings."""

# LLM: 这些上限保护 prompt 预算和外部调用，放宽前确认调用面。
# 模块用途: 工具读取、检索、HTTP 和目录展示上限的配置模型。

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ToolConfig"]


# LLM: ToolConfig 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: ToolConfig 配置模型，保存 配置系统 的默认值和可调参数。
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