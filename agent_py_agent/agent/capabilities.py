# LLM: Agent package module; keep public imports and cross-module compatibility stable.
# 模块用途: 提供 agent 核心功能的一部分，对外暴露稳定入口或兼容转发。

from __future__ import annotations

"""compatibility facade for capability routing moved to `agent.capability`.

给人看的解释：
真实能力路由代码已经放进 `agent_py_agent.agent.capability`。
这个文件只保留旧入口，让测试和历史调用不用立刻改 import。
"""

from .capability import (  # noqa: F401
    CapabilityCard,
    CapabilityGrantScope,
    CapabilityRouter,
    CapabilitySearchHit,
    CapabilityUsageRecord,
    CapabilityUsageStats,
    CapabilityUsageStore,
    InMemoryMcpExecutor,
    JsonlCapabilityUsageStore,
    McpExecutionRequest,
    McpRegistryConfig,
    McpStdioServerSpec,
    McpTool,
    McpToolDescriptor,
    StdioMcpExecutor,
    classify_tool_risk,
    default_capability_cards,
    from_mcp_tool,
    from_skill_card,
    from_tool_spec,
    score_card,
    tokenize,
)

__all__ = [
    "CapabilityCard",
    "CapabilityRouter",
    "CapabilitySearchHit",
    "CapabilityGrantScope",
    "CapabilityUsageRecord",
    "CapabilityUsageStats",
    "CapabilityUsageStore",
    "InMemoryMcpExecutor",
    "JsonlCapabilityUsageStore",
    "McpExecutionRequest",
    "McpRegistryConfig",
    "McpStdioServerSpec",
    "McpTool",
    "McpToolDescriptor",
    "StdioMcpExecutor",
    "classify_tool_risk",
    "default_capability_cards",
    "from_mcp_tool",
    "from_skill_card",
    "from_tool_spec",
    "score_card",
    "tokenize",
]
