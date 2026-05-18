from __future__ import annotations

# LLM: Capability grant scope separates discovery, authorization, and execution boundaries.
# 模块用途: 定义 tool/skill/MCP 的授权范围，并过滤当前上下文可见能力。
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .router import CapabilityCard


# LLM: CapabilityGrantScope is the normalized scope bundle for visible and executable capabilities.
# 类用途: 保存当前上下文已授权的 tool、skill 和 MCP tool 范围；空列表表示该类型不可见。
@dataclass(frozen=True)
class CapabilityGrantScope:
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    mcp_tools: list[str] = field(default_factory=list)
    path_scope: list[str] = field(default_factory=list)
    network_scope: list[str] = field(default_factory=list)
    command_allowlist: list[str] = field(default_factory=list)
    risk_level: str = ""

    # LLM: allows_tool checks tool name or full tool capability id without granting execution by itself.
    # 函数用途: 判断 tool 是否在当前授权范围内。
    def allows_tool(self, name_or_id: str) -> bool:
        name = _normalize_tool_name(name_or_id)
        allowed = {_normalize_tool_name(item) for item in self.tools}
        return name in allowed

    # LLM: allows_skill checks explicit skill names only; skill body loading remains a separate step.
    # 函数用途: 判断 skill 是否在当前授权范围内。
    def allows_skill(self, name_or_id: str) -> bool:
        name = _strip_prefix(name_or_id, "skill:")
        return name in {_strip_prefix(item, "skill:") for item in self.skills}

    # LLM: allows_mcp_tool supports server:name, server.name, and full mcp:server:name ids.
    # 函数用途: 判断 MCP tool 是否在当前授权范围内。
    def allows_mcp_tool(self, server: str, name: str = "") -> bool:
        wanted = _normalize_mcp_id(server, name)
        allowed = {_normalize_mcp_id(item) for item in self.mcp_tools}
        return wanted in allowed

    # LLM: allows_card applies scope filtering uniformly for capability catalogs and routing.
    # 函数用途: 判断一张能力卡是否属于当前可见授权范围。
    def allows_card(self, card: CapabilityCard) -> bool:
        if card.kind == "tool":
            return self.allows_tool(card.name) or self.allows_tool(card.id)
        if card.kind == "skill":
            return self.allows_skill(card.name) or self.allows_skill(card.id)
        if card.kind == "mcp_tool":
            server = str(card.metadata.get("server") or "")
            return self.allows_mcp_tool(server, card.name) or self.allows_mcp_tool(card.id)
        return False


# LLM: filter_cards_by_grant_scope is pure so tests and catalog rendering can share one rule.
# 函数用途: 按授权范围过滤能力卡；scope=None 表示不过滤。
def filter_cards_by_grant_scope(
    cards: list[CapabilityCard],
    scope: CapabilityGrantScope | None,
) -> list[CapabilityCard]:
    if scope is None:
        return cards
    return [card for card in cards if scope.allows_card(card)]


# LLM: _normalize_tool_name accepts both bare tool names and full capability ids.
# 函数用途: 去掉 tool: 前缀，得到权限比较用的稳定工具名。
def _normalize_tool_name(value: str) -> str:
    return _strip_prefix(str(value or "").strip(), "tool:")


# LLM: _strip_prefix centralizes simple capability id prefix normalization.
# 函数用途: 如果文本带有指定前缀就剥离，否则原样返回清理后的文本。
def _strip_prefix(value: str, prefix: str) -> str:
    text = str(value or "").strip()
    return text[len(prefix) :] if text.startswith(prefix) else text


# LLM: _normalize_mcp_id accepts the MCP id variants used by tools, cards, and grants.
# 函数用途: 把 server/name、server.name 或 mcp:server:name 统一成 MCP capability id。
def _normalize_mcp_id(server: str, name: str = "") -> str:
    first = str(server or "").strip()
    second = str(name or "").strip()
    if first.startswith("mcp:"):
        return first
    if second:
        return f"mcp:{first}:{second}"
    if ":" in first:
        parts = first.split(":")
        if len(parts) == 2:
            return f"mcp:{parts[0]}:{parts[1]}"
    if "." in first:
        server_part, name_part = first.split(".", 1)
        return f"mcp:{server_part}:{name_part}"
    return f"mcp:{first}"
