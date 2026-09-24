# LLM: 插件连接沿原 MCP 客户端和托管资源账，缓存只保存固定连接的已验代理；安装表仍是唯一激活权威。
# 模块用途: 从已准备环境构造隔离服务，完整核对工具声明，并为原执行器生成固定代次的工具。

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, replace

from .common.nofollow_fs import open_directory_beneath
from .common.strict_json import load_strict_json
from .plugin_activation_ref import PluginActivationRef
from .plugin_host_api import HOST_API_READ, issue_host_api_env
from .plugin_installation import PluginInstallation
from .plugin_manifest import canonical_plugin_settings
from .tooling.input_schema import canonicalize_tool_input_schema
from .tooling.mcp_client import MCPError, MCPServerConfig, MCPStdioClient, sanitize_credentials
from .tooling.mcp_registration import MCPProxyTool, build_proxy_tool, sanitize_name_component
from .tooling.models import (
    ResourceScopePolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolInvocationContext,
)
from .tooling.process_session_store import ProcessSessionStore, process_session_store_root
from .workspace_read_context import WORKSPACE_READ_EXTENSION, WORKSPACE_READ_VERSION
from .workspace_write_context import WORKSPACE_WRITE_EXTENSION, WORKSPACE_WRITE_VERSION


# LLM: 展示名由完整大小写敏感身份派生，截断只作用于可读部分；发布还必须检测实际名称冲突，不能覆盖核心工具。
# 函数用途: 为普通模型调用和显式插件命令生成同一个稳定工具名。
def plugin_tool_name(plugin_id: str, tool_name: str) -> str:
    parts = []
    for value in (plugin_id, tool_name):
        parts.append(sanitize_name_component(value)[:16] + "_" + hashlib.sha256(value.encode()).hexdigest()[:8])
    return "plugin__" + "__".join(parts)


# LLM: 代理继承原执行/结果链，补固定激活可用性和能力协商的逐次元数据；不追随新连接或更改共享 cwd。
# 类用途: 将插件接入原权限与撤销链，并把已冻结读取范围交给明确支持的插件。
class PluginProxyTool(MCPProxyTool):
    # LLM: 激活合同只存在于 PluginMCPClient（activation_ref）；用普通 MCPStdioClient 组装的代理（工作区上下文等测试夹具）
    #   没有该合同，不做激活复核、只走原 MCP 连接检查——不是放宽撤销门，因为产品里插件代理一律由 PluginMCPClient 发现并持有
    #   activation_ref。有合同时任何 OSError/ValueError（表坏、撤销、换代）都按已撤销处理，不追随最新安装。
    # 函数用途: 统一目录可用性、审批前后复核、发送前三处的激活复核判定。
    def _activation_revoked(self) -> bool:
        ref = getattr(self.client, "activation_ref", None)
        if ref is None:
            return False
        try:
            ref.require()
        except (OSError, ValueError):
            return True
        return False

    # LLM: 配置和代次只取固定 client；原表坏、撤销或换代都按不可用处理，不追随最新安装。
    # 函数用途: 只读检查原插件和已握手连接能否进入本轮目录。
    def availability(self) -> ToolAvailability:
        if self._activation_revoked():
            return ToolAvailability.unavailable("原插件已停用或激活不可用")
        return super().availability()

    # LLM: 先复核原激活（一次有界安装表读取），失效报 PLUGIN_ACTIVATION_UNAVAILABLE；再复核连接内存状态。不启动进程、不追随新代次。
    # 函数用途: 审批前/批准后执行前复核原插件是否仍启用且连接仍在。
    def precheck_availability(self) -> ToolAvailability:
        if self._activation_revoked():
            return ToolAvailability.unavailable("原插件已停用或激活不可用", error_code="PLUGIN_ACTIVATION_UNAVAILABLE")
        return super().precheck_availability()

    # LLM: 免审批调用不经过执行器复核，而 MCPProxyTool 先看连接再做发送准入：停用把连接关掉后，旧快照调用会先撞上
    #   MCP_CONNECTION_CLOSED（映射为可重试的 TOOL_EXECUTION_FAILED），结果码随清理快慢摆动。这里在发送前先鲜活复核原激活，
    #   撤销一律报 TOOL_UNAVAILABLE、effect_outcome=not_started，与审批前/批准后复核同一事实源；
    #   这是生命周期第 6 条"旧快照在执行门检查撤销"的落点。不启动进程、不追随新代次。
    # 函数用途: 停用后的插件调用固定报不可用且未执行，其余沿原 MCP 代理执行链。
    def _execute(self, params: dict[str, object], context: ToolInvocationContext | None) -> ToolHandlerOutcome:
        if self._activation_revoked():
            return ToolHandlerOutcome(
                self.model_spec.name, False, '{"error": "原插件已停用或激活不可用"}',
                error_code="TOOL_UNAVAILABLE", reported_error_code="PLUGIN_ACTIVATION_UNAVAILABLE",
                effect_outcome="not_started",
            )
        return super()._execute(params, context)

    # LLM: 只看代理固定 transport 的声明；普通 arguments 不能伪造元数据，缺可信上下文须在发送前失败。
    #   写入上下文只给协商了写入扩展、且本工具声明 mutating/dangerous 的调用；只读工具永远拿不到写权限。
    # 函数用途: 为支持当前扩展版本的插件生成本次工作区读取/写入元数据。
    def _request_meta(self, context: ToolInvocationContext | None) -> dict[str, object] | None:
        meta: dict[str, object] = {}
        if self._negotiated(WORKSPACE_READ_EXTENSION, WORKSPACE_READ_VERSION):
            if context is None or context.workspace_read_context is None:
                raise MCPError("插件缺少本次工作区读取上下文", code="MCP_PROTOCOL_ERROR", effect_outcome="not_started")
            meta[WORKSPACE_READ_EXTENSION] = context.workspace_read_context.to_payload()
        if (self._negotiated(WORKSPACE_WRITE_EXTENSION, WORKSPACE_WRITE_VERSION)
                and self._declared_effect() in {"mutating", "dangerous"}):
            if context is None or context.workspace_write_context is None:
                raise MCPError("插件缺少本次工作区写入上下文", code="MCP_PROTOCOL_ERROR", effect_outcome="not_started")
            meta[WORKSPACE_WRITE_EXTENSION] = context.workspace_write_context.to_payload()
        return meta or None

    # LLM: 只读固定 transport 握手时的 experimental 声明；声明不授予权限，仅决定是否附带宿主上下文。
    # 函数用途: 判断插件是否声明支持某个扩展的指定版本。
    def _negotiated(self, extension: str, version: str) -> bool:
        capabilities = self.transport.capabilities if self.transport is not None else {}
        experimental = capabilities.get("experimental")
        declared = experimental.get(extension) if isinstance(experimental, dict) else None
        versions = declared.get("versions") if isinstance(declared, dict) else None
        return isinstance(versions, list) and version in versions

    # LLM: 效果来自已安装包描述中同名工具的声明；找不到按只读处理，不放宽。
    # 函数用途: 读取本工具声明的副作用类型。
    def _declared_effect(self) -> str:
        tools = getattr(getattr(self.client, "installation", None), "manifest", None)
        for tool in getattr(tools, "tools", ()):
            if tool.name == self.remote_tool:
                return tool.requested_effect
        return "read_only"


PLUGIN_DATA_DIR_ENV = "MY_AGENT_PLUGIN_DATA_DIR"


# LLM: 唯一插件数据位置；只由 owner 与已校验的插件 ID 推出，不接受包声明的路径。调用方负责 no-follow 创建。
# 函数用途: 返回某 owner 下某插件的私有数据目录，供启动插件进程和管理命令共用。
def plugin_data_dir(owner, plugin_id: str):
    return owner.plugins_dir / "data" / plugin_id


# LLM: 一个客户端只属于安装表中的固定代次；沿原 MCP 重连/关闭与资源登记，不接收包提供的 owner、argv 或宿主地址。
# 类用途: 保存插件连接和同连接已验工具，供共享权限视图分别生成目录。
class PluginMCPClient(MCPStdioClient):
    # LLM: 构造只读规范环境和设置；私有值只放子进程环境，包声明的 effect 不降低原危险工具门。
#   同时 no-follow 创建插件数据目录（有副作用：可能新建目录），经 MY_AGENT_PLUGIN_DATA_DIR 传给子进程；
#   声明了 host_api=["read"] 的包另获宿主只读 API 地址与令牌（有副作用：登记令牌）。
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
        # 插件自有数据按 owner + 插件 ID 固定一处，跨版本、停用和重新启用保留；卸载默认也不清（见 PLUGIN_LIFECYCLE）
        data_dir = plugin_data_dir(owner, installation.manifest.plugin_id)
        descriptor = open_directory_beneath(owner.root, data_dir.relative_to(owner.root).parts, create=True)
        os.close(descriptor)
        settings = canonical_plugin_settings(load_strict_json(installation.settings_json or "{}"),
                                             installation.manifest.settings_schema)
        self.installation = installation
        self.validated_tools: tuple[PluginProxyTool, ...] = ()
        # 只有声明了宿主只读 API 的包才拿到地址和令牌；令牌绑定本激活，停用/换代后宿主侧即失效
        host_api_env = (issue_host_api_env(self.activation_ref, installation.manifest.plugin_id)
                        if HOST_API_READ in getattr(installation.manifest, "host_api", ()) else {})
        super().__init__(MCPServerConfig(
            name="plugin_" + installation.manifest.plugin_id,
            command=str(environment / "python" / "bin" / "python"),
            args=["-I", "-m", installation.manifest.entry_module], cwd=str(environment),
            env={"MY_AGENT_PLUGIN_SETTINGS": settings, PLUGIN_DATA_DIR_ENV: str(data_dir), **host_api_env},
            catalog_category="plugins",
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
            proxies.append(PluginProxyTool(self, info.name, self._plugin_model_spec(proxy.model_spec, name, info),
                                           policy, transport=transport))
        if len({tool.model_spec.name for tool in proxies}) != len(proxies):
            raise MCPError("插件工具名称冲突", code="MCP_PROTOCOL_ERROR")
        return tuple(proxies)

    # LLM: 只改模型可见的说明与检索提示（软信息），不改权限、效果或实现身份；插件 ID 与简介来自已安装包描述。
    #   让模型、tool_search 与能力推荐都能按"插件 <ID>"找到它，而不是只看到通用 MCP 文案和哈希后的工具名。
    # 函数用途: 生成带插件身份的工具说明和关键词。
    def _plugin_model_spec(self, spec, name: str, info):
        manifest = self.installation.manifest
        plugin_id = manifest.plugin_id
        description = f"插件 {plugin_id}（{' '.join(manifest.summary.split())}）的 {info.name} 工具。{info.description}"
        keywords = tuple(dict.fromkeys((*spec.hints.keywords, plugin_id, plugin_id.replace("-", "_"),
                                        *plugin_id.replace("_", "-").split("-"), info.name)))
        return replace(spec, name=name, description=sanitize_credentials(description),
                       hints=replace(spec.hints, keywords=keywords, provider_id="plugin:" + plugin_id))

    # LLM: 缓存只在原连接/激活再次核验后整体替换；当前安装版本不能改变同连接代理的实现身份。
    # 函数用途: 为下一轮缓存完整工具贡献，调用方仍须在自己的权限视图中投影。
    def publish_discovered_tools(self, transport, tools: tuple[PluginProxyTool, ...]) -> None:
        # LLM: 回调只换内存元组，不取安装写锁、发请求或写持久状态。
        # 函数用途: 在原 MCP 发布临界区保存已构造的同连接工具。
        def publish():
            self.validated_tools = tools

        self.publish_tools(transport, publish)
