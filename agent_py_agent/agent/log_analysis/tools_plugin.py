# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Log analysis plugin facade implementing ExtensionPlugin interface.

This module provides LogAnalysisPlugin which wraps the log analysis tools
and registers them via the ExtensionPlugin protocol.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from ..extensions.plugin import ExtensionPlugin

if TYPE_CHECKING:
    from ..extensions.plugin import ExtensionRegistry


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 LogAnalysisPlugin 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 LogAnalysisPlugin 的状态和协作方法，作为当前模块对外复用的领域对象。
class LogAnalysisPlugin:
    """ExtensionPlugin facade for log analysis tools.

    This class wraps the log analysis tools and registers them via the
    ExtensionPlugin protocol for integration with the agent's extension system."""

    name: str = "log_analysis"
    version: str = "1.0.0"

    # LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 __init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 初始化实例依赖、路径或缓存状态，为同一对象的后续方法提供共享上下文。
    def __init__(self, store_root: Path | None = None) -> None:
        """Initialize plugin with optional store root."""
        self.store_root = store_root or Path.cwd()

    # LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 register_tools 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 register tools 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
    def register_tools(self, registry) -> None:
        """Register log analysis tools with the tool registry."""
        from .tools_register import register_tools
        register_tools(registry, self.store_root)

    # LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 register_commands 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 register commands 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
    def register_commands(self, registry) -> None:
        """Register CLI/API commands (not implemented for this version)."""
        pass

    # LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 register_workflows 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 register workflows 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
    def register_workflows(self, registry) -> None:
        """Register workflow templates (not implemented for this version)."""
        pass

    # LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 register_memory_sources 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 register memory sources 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
    def register_memory_sources(self, registry) -> None:
        """Register memory sources (not implemented for this version)."""
        pass


# For convenience, expose the plugin instance
_plugin_instance: LogAnalysisPlugin | None = None


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 get_plugin 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 读取 get plugin 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
def get_plugin(store_root: Path | None = None) -> LogAnalysisPlugin:
    """Get or create the LogAnalysisPlugin singleton."""
    global _plugin_instance
    if _plugin_instance is None:
        _plugin_instance = LogAnalysisPlugin(store_root)
    return _plugin_instance
