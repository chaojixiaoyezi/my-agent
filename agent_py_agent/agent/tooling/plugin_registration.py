# LLM: 本模块只组合原安装投影与原 MCP 客户端集合，不写另一份激活表；调用方持原 Registry prepare 锁。
# 模块用途: 在新运行前接入当前插件，并让每个共享连接的权限视图都获得自己的完整工具目录。

from __future__ import annotations

import logging
import time
from collections import OrderedDict

from ..plugin_install_store import PluginInstallStore
from ..plugin_runtime import PluginMCPClient, PluginProxyTool
from .mcp_client import MCPError

logger = logging.getLogger(__name__)
_INSTALLATION_SNAPSHOTS: OrderedDict[tuple, tuple] = OrderedDict()
_MISSING_ENVIRONMENTS: OrderedDict[tuple, float] = OrderedDict()
_MISSING_ENV_WARNED: OrderedDict[tuple, None] = OrderedDict()
# 插件安装快照及缺失环境提示缓存各自最多保留的条数。
_PLUGIN_CACHE_MAX_ENTRIES = 128
# 缺失插件环境重新检查的最短间隔秒数，不改变激活或安装授权。
_MISSING_ENV_RETRY_SECONDS = 30.0


# LLM: 安装表仍经原 Store 解码；stat 失败不复用旧激活，原子替换/权限变化均使缓存失效。
# 函数用途: 复用未变化的安装快照，返回本轮安装文件戳。
def _discover_plugin_manifests(owner) -> tuple[tuple, tuple]:
    path = owner.plugins_dir / "installations.json"
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino, stat.st_ctime_ns, stat.st_mode)
    except OSError:
        return PluginInstallStore(owner).snapshot(), ()
    key = (str(path), *stamp)
    if key not in _INSTALLATION_SNAPSHOTS:
        rows = PluginInstallStore(owner).snapshot()
        # 原子替换横跨读取时不缓存；下拍必须重读新表，旧客户端仍须逐个检查撤销。
        after = path.stat()
        if (after.st_mtime_ns, after.st_size, after.st_ino, after.st_ctime_ns, after.st_mode) != stamp:
            return PluginInstallStore(owner).snapshot(), ()
        _INSTALLATION_SNAPSHOTS[key] = rows
        while len(_INSTALLATION_SNAPSHOTS) > _PLUGIN_CACHE_MAX_ENTRIES:
            _INSTALLATION_SNAPSHOTS.popitem(last=False)
    _INSTALLATION_SNAPSHOTS.move_to_end(key)
    return _INSTALLATION_SNAPSHOTS[key], stamp


# LLM: 键绑定 owner、安装 revision、原激活代和环境 inode/mtime；未知环境不永久视作缺失。
# 函数用途: 读取本次插件环境的轻量变化戳，恢复目录后立即允许重试。
def _plugin_environment_key(owner, installation, stamp: tuple) -> tuple:
    activation = installation.activation
    if activation is None:
        return (str(owner.home_dir), installation.manifest.plugin_id, installation.revision,
                installation.activation_id, stamp, (None,))
    environment = owner.plugins_dir / "environments" / activation.plan.environment_ref
    try:
        stat = environment.stat()
        env_stamp = (True, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns)
    except FileNotFoundError:
        env_stamp = (False,)
    except OSError:
        env_stamp = (None,)
    return (str(owner.home_dir), installation.manifest.plugin_id, installation.revision,
            installation.activation_id, stamp, env_stamp)


# LLM: 负缓存只吞缺失环境重试，不跳过上游撤销/stop 或下游工具视图重建；日志按代只报一次。
# 函数用途: 保存有期限的环境失败观察，限制坏插件重复构造与刷屏。
def _remember_missing_environment(key: tuple) -> None:
    _MISSING_ENVIRONMENTS[key] = time.monotonic() + _MISSING_ENV_RETRY_SECONDS
    warning_key = key[:-1]
    if warning_key not in _MISSING_ENV_WARNED:
        logger.warning("插件业务连接暂不可创建：FileNotFoundError")
        _MISSING_ENV_WARNED[warning_key] = None
    for cache in (_MISSING_ENVIRONMENTS, _MISSING_ENV_WARNED):
        while len(cache) > _PLUGIN_CACHE_MAX_ENTRIES:
            cache.popitem(last=False)


# LLM: 原关闭/撤销链先于激活代负缓存；快照仅避重复解码，失败后不跳过权限视图重建。
# 函数用途: 将当前 owner 的已启用程序插件同步到原 MCP 集合；纯内容能力包不创建连接，撤销只影响准确旧连接。
def synchronize_plugin_clients(registry) -> None:
    from ..user_space.owner_resolver import OwnerIdentity

    owner = registry._construction_params.plugin_owner
    if owner is None:
        return
    try:
        rows, stamp = _discover_plugin_manifests(owner) if owner.identity == OwnerIdentity.local_main() else ((), ())
        active = ({row.activation_id: row for row in rows
                   if row.enabled and not row.manifest.is_content_only}
                  if owner.identity == OwnerIdentity.local_main() else {})
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
        key = _plugin_environment_key(owner, installation, stamp)
        if time.monotonic() < _MISSING_ENVIRONMENTS.get(key, 0.0):
            continue
        client = None
        try:
            client = PluginMCPClient(owner, installation,
                                     runtime_repo=getattr(registry._construction_params, "runtime_repo", None),
                                     process_sandbox=bool(getattr(registry._construction_params, "plugin_process_sandbox", False)),
                                     host_credential_env_names=registry._construction_params.host_credential_env_names)
            client.require_settled_previous_resources()
            registry._mcp_clients.append(client)
            if registry._mcp_closed.is_set():
                client.stop()
        except (OSError, ValueError, RuntimeError, MCPError) as exc:
            if client is not None:
                client.stop()
            if isinstance(exc, FileNotFoundError) or key[-1] == (False,):
                _remember_missing_environment(key)
            else:
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
