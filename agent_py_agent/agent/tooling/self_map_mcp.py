# LLM: 管理员读取原 MCP 元数据接缝，其它身份仅从授权规格取服务；不联机、不显示配置秘密。
# 模块用途: 登记 MCP 配置、连接和发布状态；配置与本机 transport 观测分别计数。
from .mcp_registration import mcp_server_facts
from .self_map_registry import (
    SELF_MAP_UNKNOWN_OBSERVATION,
    SectionDescriptor,
    SectionProjection,
    map_item,
)
from .self_map_tools import public_catalog_specs


# LLM: 服务领域登记固定用途和原计数标签；工具说明不用解析服务名称或连接状态。
# 函数用途: 接入 MCP 分块。
def register_mcp_section(registry):
    registry.register(SectionDescriptor("mcp", "MCP 服务", 60, mcp_projection, "MCP服务已配置、已连接数量与状态"))


# LLM: 配置/连接数取原权威；显示标签只解释结构化分项，未知仍null，不从工具数或名字推断连接。
# 函数用途: 给本范围服务及明确的配置/连接数量，空配置和未观察连接分开。
def mcp_projection(context):
    specs = public_catalog_specs(context)
    services = {spec.hints.provider_id[4:] for spec in specs if spec.hints.provider_id.startswith("mcp:")}
    rows = {row["server"]: _public_fact(row) for row in mcp_server_facts(context.agent.tools)} if context.admin else {}
    configured = set((getattr(context.agent.config, "mcp_servers", None) or {}).keys()) if context.admin else set()
    if context.admin:
        services.update(rows)
        services.update(configured)
    items = tuple(map_item(name, name, fields={**rows.get(name, {"transport_running": None}),
                  "configured": name in configured if context.admin else None,
                  "authorized_tool_count": sum(spec.hints.provider_id == "mcp:" + name for spec in specs),
                  "remote_call": "not_verified", "observation_point": "current_host_transport"})
                  for name in sorted(services))
    observed = [rows.get(name, {}).get("transport_running") for name in services]
    connected = sum(value is True for value in observed) if context.admin and all(value is not None for value in observed) else None
    counts = {"configured": len(configured) if context.admin else None, "connected": connected}
    reasons = {key: SELF_MAP_UNKNOWN_OBSERVATION for key, value in counts.items() if value is None}
    return SectionProjection(items, source="AgentConfig / bound MCP clients / ToolRuntimeSnapshot", counts=counts, count_reasons=reasons,
                             count_labels={"configured": "已配置", "connected": "已连接"})


# LLM: 原facts含发布元数据，只白名单状态和安全码；错误正文、命令/环境和连接动作不能进目录。
# 函数用途: 从已有MCP状态读口裁剪安全字段，避免新造重复接缝。
def _public_fact(row):
    publication = row.get("publication") or {}
    return {"configured": True, "transport_running": row["running"],
            "publication_state": publication.get("state", "not_observed"), "error_code": publication.get("code", "")}
