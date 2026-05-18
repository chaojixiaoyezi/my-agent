from __future__ import annotations

# LLM: MCP capability module; keep external tool descriptors separate from execution or credentials.
# 模块用途: 定义 MCP tool 的可路由能力描述，并转换为统一 CapabilityCard。
from dataclasses import dataclass, field


# LLM: McpToolDescriptor is the explicit MCP capability bundle; do not replace it with long-lived dict payloads.
# 类用途: 描述一个外部 MCP tool 的可路由能力，不包含连接凭据，也不执行工具。
@dataclass(frozen=True)
class McpToolDescriptor:
    server: str
    name: str
    description: str
    capabilities: list[str] = field(default_factory=list)
    when_to_use: list[str] = field(default_factory=list)
    not_when_to_use: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    risk_level: str = "medium"
    source: str = "mcp"


# LLM: from_mcp_tool maps external MCP tool metadata into the same routable card shape without execution hooks.
# 函数用途: 把 MCP tool 描述符映射成统一能力卡，供披露、搜索和 capability route 使用。
def from_mcp_tool(descriptor: McpToolDescriptor):
    """把 MCP tool 描述符映射成统一能力卡。"""

    from .router import CapabilityCard

    server = descriptor.server.strip()
    name = descriptor.name.strip()
    if not server or not name:
        raise ValueError("MCP tool descriptor 需要 server 和 name。")
    return CapabilityCard(
        id=f"mcp:{server}:{name}",
        kind="mcp_tool",
        name=name,
        description=descriptor.description.strip() or name,
        capabilities=list(descriptor.capabilities),
        when_to_use=list(descriptor.when_to_use),
        not_when_to_use=list(descriptor.not_when_to_use),
        keywords=[server, name, *descriptor.keywords, *descriptor.capabilities],
        risk_level=descriptor.risk_level or "medium",
        source=descriptor.source or "mcp",
        metadata={"server": server},
    )
