from __future__ import annotations

# LLM: Tool registry extension module; keep capability catalog registration separate from registry growth.
# 模块用途: 把 capability 目录工具挂到 ToolRegistry，避免主注册表继续膨胀。
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import ToolRegistry


# LLM: register_capability_catalog_tools exposes only already visible tool specs captured at registration time.
# 函数用途: 注册能力目录检索工具；它只披露目录信息，不执行或授权任何能力。
def register_capability_catalog_tools(registry: ToolRegistry) -> None:
    from .capability_catalog import CapabilityDescribeTool, CapabilitySearchTool

    visible_specs = [
        spec
        for spec in registry.specs(include_orchestration=True)
        if spec.category != "mcp"
    ]
    registry.register(
        CapabilitySearchTool(
            tool_specs=visible_specs,
            skill_registry=registry.capability_skill_registry,
            extra_cards=registry.capability_extra_cards,
            grant_scope=registry.capability_grant_scope,
        )
    )
    registry.register(
        CapabilityDescribeTool(
            tool_specs=visible_specs,
            skill_registry=registry.capability_skill_registry,
            extra_cards=registry.capability_extra_cards,
            grant_scope=registry.capability_grant_scope,
        )
    )
