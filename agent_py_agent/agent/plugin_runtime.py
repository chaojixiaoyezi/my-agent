# LLM: 插件连接沿原 MCP 客户端和托管资源账，缓存只保存固定连接的已验代理；安装表仍是唯一激活权威。
# 模块用途: 从已准备环境构造隔离服务，完整核对工具声明，并为原执行器生成固定代次的工具。

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, replace

from .common.nofollow_fs import open_directory_beneath
from .common.strict_json import load_strict_json
from .plugin_activation_ref import PluginActivationRef
from .plugin_installation import PluginInstallation
from .plugin_manifest import canonical_plugin_settings
from .tooling.input_schema import canonicalize_tool_input_schema
from .tooling.mcp_client import MCPError, MCPServerConfig, MCPStdioClient
from .tooling.mcp_registration import MCPProxyTool, build_proxy_tool, sanitize_name_component
from .tooling.models import ResourceScopePolicy, ToolAvailability
from .tooling.process_session_store import ProcessSessionStore, process_session_store_root


# LLM: 展示名由完整大小写敏感身份派生，截断只作用于可读部分；发布还必须检测实际名称冲突，不能覆盖核心工具。
# 函数用途: 为普通模型调用和显式插件命令生成同一个稳定工具名。
def plugin_tool_name(plugin_id: str, tool_name: str) -> str:
    parts = []
    for value in (plugin_id, tool_name):
        parts.append(sanitize_name_component(value)[:16] + "_" + hashlib.sha256(value.encode()).hexdigest()[:8])
    return "plugin__" + "__".join(parts)


# LLM: 代理继承原执行/结果链，仅补鲜活激活可用性；可用性查询不启动服务，也不能代替排队后的发送准入。
# 类用途: 让停用插件立即从新快照消失，而旧快照仍绑定原连接并在执行时拒绝。
class PluginProxyTool(MCPProxyTool):
    # LLM: 配置和代次只取固定 client；原表坏、撤销或换代都按不可用处理，不追随最新安装。
    # 函数用途: 只读检查原插件和已握手连接能否进入本轮目录。
    def availability(self) -> ToolAvailability:
        try:
            self.client.activation_ref.require()
        except (OSError, ValueError):
            return ToolAvailability.unavailable("原插件已停用或激活不可用")
        return super().availability()


# LLM: 一个客户端只属于安装表中的固定代次；沿原 MCP 重连/关闭与资源登记，不接收包提供的 owner、argv 或宿主地址。
# 类用途: 保存插件连接和同连接已验工具，供共享权限视图分别生成目录。
class PluginMCPClient(MCPStdioClient):
    # LLM: 构造只读规范环境和设置；私有值只放子进程环境，包声明的 effect 不降低原危险工具门。
    # 函数用途: 将已准备 Python 环境接到原 MCP 客户端，不在构造时启动进程。
    def __init__(self, owner, installation: PluginInstallation):
        activation = installation.activation
        if activation is None or activation.phase not in {"preparing", "active"}:
            raise ValueError("插件没有可启动的固定激活")
        self.activation_ref = PluginActivationRef.from_owner(owner, installation.manifest.plugin_id, activation.activation_id)
        current = self.activation_ref.require(allow_preparing=True)
        if current != installation:
            raise ValueError("插件安装已变化")
        environment = owner.plugins_dir / "environments" / activation.plan.environment_ref
        descriptor = open_directory_beneath(owner.root, (*environment.relative_to(owner.root).parts, "python", "bin"))
        os.close(descriptor)
        settings = canonical_plugin_settings(load_strict_json(installation.settings_json or "{}"),
                                             installation.manifest.settings_schema)
        self.installation = installation
        self.validated_tools: tuple[PluginProxyTool, ...] = ()
        super().__init__(MCPServerConfig(
            name="plugin_" + installation.manifest.plugin_id,
            command=str(environment / "python" / "bin" / "python"),
            args=["-I", "-m", installation.manifest.entry_module], cwd=str(environment),
            env={"MY_AGENT_PLUGIN_SETTINGS": settings}, catalog_category="plugins",
        ), activation=self.activation_ref)

    # LLM: 原 Store 同代资源是唯一事实源；其他运行实例可共存，未知和未确认停止不能靠新客户端绕过。
    # 函数用途: 在模型或显式命令建立业务连接前，核对这个激活留下的资源清理结果。
    def require_settled_previous_resources(self) -> None:
        owner = self.activation_ref.owner()
        store = ProcessSessionStore(process_session_store_root(owner.home_dir, owner.home_dir))
        with store.transaction() as transaction:
            records, errors = transaction.list_records()
            if errors:
                raise ValueError("插件原资源目录不可读")
            for record in records:
                if record.get("activation_scope") != asdict(self.activation_ref.scope):
                    continue
                if record["status"] == "unknown" or (record["stop_requested"]
                        and ((record.get("termination") or {}).get("cleanup") or {}).get("confirmed") is not True):
                    raise ValueError("插件原资源退出尚未确认")

    # LLM: 完整分页来自固定 transport；名称、说明和规范 schema 全部相符才构造代理，任一坏项拒绝整包而非部分发布。
    # 函数用途: 验证当前服务确实提供所安装的工具，并沿原 MCP 代理生成受控能力。
    def discover_tools(self, transport) -> tuple[PluginProxyTool, ...]:
        actual = self.list_tools(transport=transport)
        declared = {tool.name: tool for tool in self.installation.manifest.tools}
        if len(actual) != len(declared) or {tool.name for tool in actual} != set(declared):
            raise MCPError("插件工具集合与安装声明不一致", code="MCP_PROTOCOL_ERROR")
        proxies = []
        for info in actual:
            expected = declared[info.name]
            try:
                schema = canonicalize_tool_input_schema(info.input_schema)
            except (TypeError, ValueError) as exc:
                raise MCPError("插件输入结构不可执行", code="MCP_PROTOCOL_ERROR") from exc
            if (info.description != expected.description
                    or schema != expected.input_schema):
                raise MCPError("插件工具说明或输入结构与安装声明不一致", code="MCP_PROTOCOL_ERROR")
            proxy = build_proxy_tool(self, self.config.name, info, transport=transport, catalog_category="plugins")
            name = plugin_tool_name(self.installation.manifest.plugin_id, info.name)
            policy = replace(proxy.runtime_policy, resource_scopes=ResourceScopePolicy(
                "declared", static_scopes=(f"logical:plugin:{self.activation_ref.scope.activation_id}:{info.name}",),
            ))
            proxies.append(PluginProxyTool(self, info.name, replace(proxy.model_spec, name=name), policy, transport=transport))
        if len({tool.model_spec.name for tool in proxies}) != len(proxies):
            raise MCPError("插件工具名称冲突", code="MCP_PROTOCOL_ERROR")
        return tuple(proxies)

    # LLM: 缓存只在原连接/激活再次核验后整体替换；当前安装版本不能改变同连接代理的实现身份。
    # 函数用途: 为下一轮缓存完整工具贡献，调用方仍须在自己的权限视图中投影。
    def publish_discovered_tools(self, transport, tools: tuple[PluginProxyTool, ...]) -> None:
        # LLM: 回调只换内存元组，不取安装写锁、发请求或写持久状态。
        # 函数用途: 在原 MCP 发布临界区保存已构造的同连接工具。
        def publish():
            self.validated_tools = tools

        self.publish_tools(transport, publish)
