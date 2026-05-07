# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Tool registration for log analysis extension.

This module provides the register_tools function that registers all log analysis
tools with the agent's tool registry.
"""

from pathlib import Path

from .tools_handlers import SecurityHuntIpTool, SecurityQueryTool, SecurityTraceCaseTool


# LLM: 工具层把查询、追踪和 handler 结果暴露给 agent 调用；修改 register_tools 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 register tools 在当前模块中的核心转换或协调步骤，衔接 工具层把查询、追踪和 handler 结果暴露给 agent 调用。
def register_tools(registry, store_root: Path | None = None) -> None:
    """Register all log analysis tools with the given registry.

    Args:
        registry: Tool registry that supports .register() method.
        store_root: Optional root path for the log analysis store."""
    root = store_root or Path.cwd()
    registry.register(SecurityQueryTool(root))
    registry.register(SecurityHuntIpTool(root))
    registry.register(SecurityTraceCaseTool(root))