# LLM: Capability module; keep skill/tool routing contracts stable for planner and dispatch callers.
# 模块用途: 描述和路由 agent 能力、技能、工具和执行条件。

from __future__ import annotations

"""public API for capability routing, capability config, and skill cards.

给人看的解释：
这里是'能力治理'目录。skill 和 tool 都会先变成能力卡，再由父代理判断该给谁、给多少、
什么时候上抛缺口。以后 resource、MCP、remote agent 也应该接到这里。
"""

from .config import CapabilityConfig, load_capability_config
from .grants import CapabilityGrantScope, filter_cards_by_grant_scope
from .mcp_config import McpRegistryConfig, mcp_registry_config_from_agent_config
from .mcp_runtime import (
    InMemoryMcpExecutor,
    McpExecutionRequest,
    McpStdioServerSpec,
    McpTool,
    StdioMcpExecutor,
)
from .router import (
    CapabilityCard,
    CapabilityRouter,
    CapabilitySearchHit,
    McpToolDescriptor,
    classify_tool_risk,
    default_capability_cards,
    from_mcp_tool,
    from_skill_card,
    from_tool_spec,
    score_card,
    tokenize,
)
from .runtime_config import (
    CapabilityConfigPatch,
    CapabilityConfigPatchRequest,
    CapabilityConfigPatchResult,
    CapabilityConfigReloadResult,
    CapabilityConfigSnapshot,
    apply_capability_config_patch,
    capability_config_version,
    default_capability_config_path,
    load_capability_config_snapshot,
    reload_capability_config_if_changed,
)
from .skills import (
    SkillCard,
    SkillDraftRequest,
    SkillLifecycleEvent,
    SkillLifecycleResult,
    SkillLifecycleStore,
    SkillRegistry,
    parse_skill_file,
)
from .usage import (
    CapabilityUsageRecord,
    CapabilityUsageStats,
    CapabilityUsageStore,
    JsonlCapabilityUsageStore,
)

__all__ = [
    "CapabilityCard",
    "CapabilityConfig",
    "CapabilityConfigPatch",
    "CapabilityConfigPatchRequest",
    "CapabilityConfigPatchResult",
    "CapabilityConfigReloadResult",
    "CapabilityRouter",
    "CapabilitySearchHit",
    "CapabilityConfigSnapshot",
    "CapabilityGrantScope",
    "CapabilityUsageRecord",
    "CapabilityUsageStats",
    "CapabilityUsageStore",
    "JsonlCapabilityUsageStore",
    "InMemoryMcpExecutor",
    "McpExecutionRequest",
    "McpRegistryConfig",
    "McpStdioServerSpec",
    "McpTool",
    "McpToolDescriptor",
    "SkillCard",
    "SkillDraftRequest",
    "SkillLifecycleEvent",
    "SkillLifecycleResult",
    "SkillLifecycleStore",
    "SkillRegistry",
    "StdioMcpExecutor",
    "apply_capability_config_patch",
    "capability_config_version",
    "classify_tool_risk",
    "default_capability_cards",
    "default_capability_config_path",
    "filter_cards_by_grant_scope",
    "from_mcp_tool",
    "from_skill_card",
    "from_tool_spec",
    "load_capability_config",
    "load_capability_config_snapshot",
    "mcp_registry_config_from_agent_config",
    "parse_skill_file",
    "reload_capability_config_if_changed",
    "score_card",
    "tokenize",
]
