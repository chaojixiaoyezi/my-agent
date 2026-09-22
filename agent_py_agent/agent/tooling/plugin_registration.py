# LLM: 本模块只组合原安装投影与原 MCP 客户端集合，不写另一份激活表；调用方持原 Registry prepare 锁。
# 模块用途: 在新运行前接入当前插件，并让每个共享连接的权限视图都获得自己的完整工具目录。

from __future__ import annotations

import logging

from ..plugin_install_store import PluginInstallStore
from ..plugin_runtime import PluginMCPClient, PluginProxyTool
from .mcp_client import MCPError

logger = logging.getLogger(__name__)


# LLM: 只在新运行准备调用；先登记再复查共享关闭标记，未登记候选须关闭，旧代清理未知仍保留原句柄。
# 函数用途: 将当前 owner 的已启用版本同步到原 MCP 集合，撤销只影响插件自己的连接。
def synchronize_plugin_clients(registry) -> None:
    owner = registry._construction_params.plugin_owner
    if owner is None:
        return
    try:
        active = {row.activation_id: row for row in PluginInstallStore(owner).snapshot() if row.enabled}
    except (OSError, ValueError) as exc:
        logger.warning("插件安装目录不可读：%s", type(exc).__name__)
        active = {}
    clients = [client for client in registry._mcp_clients if isinstance(client, PluginMCPClient)]
    for client in clients:
        if client.activation_ref.scope.activation_id not in active:
            registry._mcp_retry_state.pop(id(client), None)
            try:
                if client.stop().confirmed:
                    registry._mcp_clients.remove(client)
            except Exception as exc:  # noqa: BLE001 清理未知留原客户端，不阻断核心及其他插件
                logger.warning("原插件连接清理尚未确认：%s", type(exc).__name__)
    known = {client.activation_ref.scope.activation_id for client in registry._mcp_clients
             if isinstance(client, PluginMCPClient)}
    for identity, installation in active.items():
        if identity in known or registry._mcp_closed.is_set():
            continue
        client = None
        try:
            client = PluginMCPClient(owner, installation)
            client.require_settled_previous_resources()
            registry._mcp_clients.append(client)
            if registry._mcp_closed.is_set():
                client.stop()
        except (OSError, ValueError, RuntimeError, MCPError) as exc:
            if client is not None:
                client.stop()
            logger.warning("插件业务连接暂不可创建：%s", type(exc).__name__)


# LLM: 完整目录验收与名称冲突先于任何内存发布，既有普通 MCP/核心工具不能被插件覆盖。
# 函数用途: 经原连接发布门一次保存候选代理，失败交回原 MCP 退避与精确清理链。
def refresh_plugin_client(registry, client: PluginMCPClient, transport) -> None:
    tools = client.discover_tools(transport)
    occupied = {name for name, tool in registry.tools.items() if not isinstance(tool, PluginProxyTool)}
    for other in registry._mcp_clients:
        if isinstance(other, PluginMCPClient) and other is not client:
            occupied.update(tool.model_spec.name for tool in other.validated_tools if tool.availability().available)
    if any(tool.model_spec.name in occupied for tool in tools):
        raise MCPError("插件工具与已有目录名称冲突", code="MCP_PROTOCOL_ERROR")
    client.publish_discovered_tools(transport, tools)


# LLM: 每个权限视图都重建自己的映射，不把共享 client 已 running 当成本视图已经同步；旧快照对象绝不原位改写。
# 函数用途: 从原客户端缓存的已验同代代理整体替换插件贡献，停用/坏连接不进入新快照。
def project_plugin_tools(registry) -> None:
    replacement = {name: tool for name, tool in registry.tools.items() if not isinstance(tool, PluginProxyTool)}
    for client in registry._mcp_clients:
        if not isinstance(client, PluginMCPClient):
            continue
        tools = client.validated_tools
        if not tools or not all(tool.availability().available for tool in tools):
            continue
        if any(tool.model_spec.name in replacement for tool in tools):
            logger.warning("插件工具名称冲突，本视图未接入该插件")
            continue
        replacement.update((tool.model_spec.name, tool) for tool in tools)
    registry.tools = replacement
