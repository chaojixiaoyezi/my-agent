# LLM: 插件连接沿原 MCP 客户端和托管资源账，缓存只保存固定连接的已验代理；安装表仍是唯一激活权威。
# 模块用途: 从已准备环境构造隔离服务，完整核对工具声明，并为原执行器生成固定代次的工具。

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path

from .attempt.sandbox import SandboxUnavailableError
from .capability._agents_contract_fields import AgentsContractError
from .capability.agents_source_processes import (
    process_source_change_affects,
    process_source_configurations,
    process_source_configurations_current,
    register_source_registry,
    source_process_boundary,
)
from .common.nofollow_fs import open_directory_beneath
from .common.strict_json import load_strict_json
from .plugin_activation_record import installation_tool_declarations, pinned_catalog_json_from_tools
from .plugin_activation_ref import PluginActivationRef
from .plugin_entry import FILES_DIRECTORY
from .plugin_host_api import HOST_API_READ, issue_host_api_env
from .plugin_installation import PluginInstallation
from .plugin_manifest import PluginToolDeclaration, canonical_plugin_settings
from .plugin_permissions.sandbox import LegacyLaunchDenied, legacy_launch_policy
from .plugin_runtime_facts import verified_runtime_command
from .plugin_sandbox import (
    SANDBOX_TMP_DIRECTORY,
    PluginInterpreterPaths,
    PluginV8Sandbox,
    plugin_sandbox_problem,
    plugin_sandbox_spec,
    plugin_v8_sandbox_options,
    sandboxed_plugin_argv,
)
from .tooling.input_schema import canonicalize_tool_input_schema
from .tooling.mcp_client import (
    MCPError,
    MCPServerConfig,
    MCPStdioClient,
    MCPToolInfo,
    sanitize_credentials,
)
from .tooling.mcp_registration import (
    MCPProxyTool,
    build_proxy_tool,
    disconnect_failed_mcp_transport,
    sanitize_name_component,
)
from .tooling.models import (
    ResourceScopePolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolInvocationContext,
)
from .tooling.observation_binding import ObservationBinding
from .tooling.process_session_store import ProcessSessionStore, process_session_store_root
from .workspace_read_context import WORKSPACE_READ_EXTENSION, WORKSPACE_READ_VERSION
from .workspace_write_context import WORKSPACE_WRITE_EXTENSION, WORKSPACE_WRITE_VERSION

# 能力包握手等待上限（秒）；仍在来源边界内，D9 后续再缩小锁的持有范围。
PACK_MCP_CONNECT_TIMEOUT_SECONDS: float = float("10.0")


# LLM: 仅解析能力包连接阶段的宿主预算；无效、非有限或非正值保留旧值，不扩展外层 deadline。
# 函数用途: 读取可选配置中的能力包 MCP 连接超时，返回有限正浮点秒数。
def _pack_connect_timeout(config: object | None) -> float:
    value = getattr(config, "plugin_pack_mcp_connect_timeout_seconds", PACK_MCP_CONNECT_TIMEOUT_SECONDS)
    try:
        timeout = float(value) if type(value) in (int, float) else 0.0
    except OverflowError:
        timeout = 0.0
    return timeout if math.isfinite(timeout) and timeout > 0 else PACK_MCP_CONNECT_TIMEOUT_SECONDS


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
        ref = getattr(self.client, "activation_ref", None)
        if ref is not None and not ref.available():
            return ToolAvailability.unavailable("原插件已停用或激活不可用")
        return super().availability()

    # LLM: 先复核原激活（一次有界安装表读取），失效报 PLUGIN_ACTIVATION_UNAVAILABLE；再复核连接内存状态。不启动进程、不追随新代次。
    # 函数用途: 审批前/批准后执行前复核原插件是否仍启用且连接仍在。
    def precheck_availability(self) -> ToolAvailability:
        ref = getattr(self.client, "activation_ref", None)
        if ref is not None and not ref.available():
            return ToolAvailability.unavailable("原插件已停用或激活不可用", error_code="PLUGIN_ACTIVATION_UNAVAILABLE")
        return super().precheck_availability()

    # LLM: 免审批调用不经过执行器复核，而 MCPProxyTool 先看连接再做发送准入：停用把连接关掉后，旧快照调用会先撞上
    #   MCP_CONNECTION_CLOSED（映射为可重试的 TOOL_EXECUTION_FAILED），结果码随清理快慢摆动。这里在发送前先鲜活复核原激活，
    #   撤销一律报 TOOL_UNAVAILABLE、effect_outcome=not_started，与审批前/批准后复核同一事实源；
    #   这是生命周期第 6 条"旧快照在执行门检查撤销"的落点。不启动进程、不追随新代次。观察三件套在
    #   tooling/observation_binding.ObservationBinding（与 MCPProxyTool 共用），这里只按声明构造绑定。
    # 函数用途: 停用后的插件调用固定报不可用且未执行；有观察声明的工具走共用的观察执行链，否则原样发送。
    def _execute(self, params: dict[str, object], context: ToolInvocationContext | None) -> ToolHandlerOutcome:
        if self._activation_revoked():
            return ToolHandlerOutcome(
                self.model_spec.name, False, '{"error": "原插件已停用或激活不可用"}',
                error_code="TOOL_UNAVAILABLE", reported_error_code="PLUGIN_ACTIVATION_UNAVAILABLE",
                effect_outcome="not_started",
            )
        if self.observation_binding is None:
            return self._execute_with_meta(params, context, None)
        return self.observation_binding.execute(params, context, self._execute_with_meta)

    # LLM: MCPProxyTool 的构造钩子：公开属性 observation_binding 在构造时按安装描述设好（与普通 MCP 代理同名同义，决策点的自动执行
    #   规划只读这个属性）。身份全部取宿主：provider_id 是 plugin:<id>，activation 来自固定 client 的激活代次，复核库是
    #   client.runtime_repo（owner 权威库）；actions 映射只含本包同 target_kind 的 observation_ref 工具。没有观察声明（含测试夹具的
    #   普通客户端）返回 None。client 的安装描述与激活代次在构造时已固定，不在执行时重算。
    # 函数用途: 按已安装描述与固定激活构造本工具的观察绑定（只在构造时调用一次）。
    def _initial_observation_binding(self) -> ObservationBinding | None:
        declared = self._declared_tool()
        if declared is None or (declared.observation is None and declared.observation_ref is None):
            return None
        manifest = self.client.installation.manifest
        kind = (declared.observation or declared.observation_ref).target_kind
        return ObservationBinding(
            provider_id="plugin:" + manifest.plugin_id, activation_id=self.client.activation_ref.scope.activation_id,
            tool_name=self.model_spec.name, observation=declared.observation, observation_ref=declared.observation_ref,
            action_tools={tool.name: plugin_tool_name(manifest.plugin_id, tool.name) for tool in manifest.tools
                          if tool.observation_ref is not None and tool.observation_ref.target_kind == kind},
            repo=getattr(self.client, "runtime_repo", None),
        )

    # LLM: 声明来自固定 client 的已安装包描述；找不到（测试夹具的普通客户端）返回 None，此时不做任何观察处理。
    # 函数用途: 取本代理对应的工具声明。
    def _declared_tool(self) -> PluginToolDeclaration | None:
        manifest = getattr(getattr(self.client, "installation", None), "manifest", None)
        return next((tool for tool in getattr(manifest, "tools", ()) if tool.name == self.remote_tool), None)

    # LLM: 只看代理固定 transport 的声明；普通 arguments 不能伪造元数据，缺可信上下文须在发送前失败。
    #   写入上下文只给协商了写入扩展、且本工具声明 mutating/dangerous 的调用；只读工具永远拿不到写权限。
    # 函数用途: 为支持当前扩展版本的插件生成本次工作区读取/写入元数据。
    def _request_meta(self, context: ToolInvocationContext | None) -> dict[str, object] | None:
        if getattr(self.client, "lazy", False):
            return None
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


class PackMCPProxyTool(PluginProxyTool):
    """稳定的无连接门面；首次调用才启动，所有业务请求仍委托固定 transport 的已验代理。"""

    def availability(self) -> ToolAvailability:
        if not self.client.activation_ref.available():
            return ToolAvailability.unavailable("原能力包已停用或激活不可用", error_code="PLUGIN_ACTIVATION_UNAVAILABLE")
        if self.client.drift:
            return ToolAvailability.unavailable("能力包工具目录已变化，须重新启用", error_code="MCP_PACK_CATALOG_DRIFT")
        return ToolAvailability.ready()

    def precheck_availability(self) -> ToolAvailability:
        available = self.availability()
        if not available.available:
            return available
        if time.monotonic() < self.client.retry_at:
            return ToolAvailability.unavailable("能力包服务暂不可用", error_code="MCP_PACK_SERVER_UNAVAILABLE")
        return available

    def _execute(self, params: dict[str, object], context: ToolInvocationContext | None) -> ToolHandlerOutcome:
        if self._activation_revoked():
            return self._not_started("PLUGIN_ACTIVATION_UNAVAILABLE")
        # 在 ensure 之前预留在途名额，封住 ensure 返回与计数递增之间的 idle-stop 窗口。
        with self.client._lazy_lock:
            self.client._in_flight += 1
        try:
            transport = self.client.ensure_started()
            with self.client._lazy_lock:
                bound = next((tool for tool in self.client.validated_tools
                              if tool.remote_tool == self.remote_tool and tool.transport is transport), None)
            if bound is None:
                return self._not_started("MCP_PACK_SERVER_UNAVAILABLE")
            return bound._execute_with_meta(params, context, None)
        except MCPError as exc:
            return self._not_started(exc.code)
        finally:
            with self.client._lazy_lock:
                self.client._in_flight -= 1
                self.client.last_used = time.monotonic()

    def _not_started(self, code: str) -> ToolHandlerOutcome:
        """只描述门面尚未发业务请求的拒绝，不覆盖已发送请求的 UNKNOWN。"""
        return ToolHandlerOutcome(
            self.model_spec.name, False, json.dumps({"error": code}), error_code="TOOL_UNAVAILABLE",
            reported_error_code=code, effect_outcome="not_started",
        )


PLUGIN_DATA_DIR_ENV = "MY_AGENT_PLUGIN_DATA_DIR"


# LLM: 启动命令只由宿主从安装快照与准备结果推出：Python 包沿 venv 解释器 -I -m 入口模块；可执行入口直接运行随包文件；
#   解释器入口先按计划指纹复核定位文件（解释器被替换即拒绝），再以其绝对路径运行随包脚本。包不能提供宿主路径或额外 argv。
# 函数用途: 返回插件进程的命令、参数与工作目录，并确认所需目录仍在原环境里。
def _launch_argv(owner, manifest, environment, fingerprint: str) -> tuple[str, list[str], object]:
    entry = manifest.entry
    if entry is None:
        os.close(open_directory_beneath(owner.root, (*environment.relative_to(owner.root).parts, "python", "bin")))
        return str(environment / "python" / "bin" / "python"), ["-I", "-m", manifest.entry_module], environment
    files = environment / FILES_DIRECTORY
    os.close(open_directory_beneath(owner.root, files.relative_to(owner.root).parts))
    target = str(files / entry.command)
    interpreter = verified_runtime_command(owner.root, environment, entry.kind, fingerprint)
    if entry.kind == "executable":
        return target, list(entry.args), files
    return interpreter, [target, *entry.args], files


# LLM: 唯一插件数据位置；只由 owner 与已校验的插件 ID 推出，不接受包声明的路径。调用方负责 no-follow 创建。
# 函数用途: 返回某 owner 下某插件的私有数据目录，供启动插件进程和管理命令共用。
def plugin_data_dir(owner, plugin_id: str):
    return owner.plugins_dir / "data" / plugin_id


# LLM: v8 权限、解释器及沙箱能力来自固定安装快照；任何不完整规格都在启动前拒绝，不能退回普通沙箱。
#   就绪门与实际启动消费同一份宿主独占来源，不另算第二套隐藏集合。
# 函数用途: 为插件连接构造 v8 沙箱规格，旧版插件返回 None 以保留原平台沙箱行为。
def _plugin_v8_sandbox(owner, installation: PluginInstallation, command: str,
                       host_exclusive_roots: tuple[Path, ...] = ()) -> PluginV8Sandbox | None:
    permissions = installation.manifest.permissions
    if permissions is None:
        return None
    entry = installation.manifest.entry
    interpreter = None
    if entry is not None and entry.kind == "interpreter":
        interpreter = PluginInterpreterPaths(entry.interpreter, Path(command), Path(command))
    v8, problem = plugin_v8_sandbox_options(
        owner.home_dir, network=bool(permissions.network), interpreter=interpreter)
    if problem:
        raise SandboxUnavailableError(f"SANDBOX_UNAVAILABLE: {problem}")
    problem = plugin_sandbox_problem(True, owner.home_dir, sandbox_policy=v8, host_exclusive_roots=host_exclusive_roots)
    if problem:
        raise SandboxUnavailableError(f"SANDBOX_UNAVAILABLE: {problem}")
    return v8


# LLM: 受限策略与 v8 共用同一就绪门：网络 true 在 Linux 需要端口隔离，平台沙箱不可用一律拒绝，不退回无沙箱。
# 函数用途: 老格式 restricted 在构造与启动前确认统一沙箱底座可用。
def _require_legacy_policy_ready(owner, policy, host_exclusive_roots: tuple[Path, ...] = ()) -> None:
    problem = plugin_sandbox_problem(True, owner.home_dir, sandbox_policy=policy,
                                     host_exclusive_roots=host_exclusive_roots)
    if problem:
        raise SandboxUnavailableError(f"SANDBOX_UNAVAILABLE: {problem}")


# LLM: 一个客户端只属于安装表中的固定代次；沿原 MCP 重连/关闭与资源登记，不接收包提供的 owner、argv 或宿主地址。
# 类用途: 保存插件连接和同连接已验工具，供共享权限视图分别生成目录。
class PluginMCPClient(MCPStdioClient):
    # LLM: 构造冻结宿主继承擦洗来源；明确插件设置/host API 原 native env 保持授权语义，包 effect 不降低危险工具门。
#   同时 no-follow 创建插件数据目录（有副作用：可能新建目录），经 MY_AGENT_PLUGIN_DATA_DIR 传给子进程；
#   声明旧read或显式v9的包获得对应宿主API地址与固定激活令牌（有副作用：登记令牌）。
#   process_sandbox=True（配置 plugin_process_sandbox）时启动命令套进平台沙箱，只可写数据目录，TMPDIR 指向其中的 .tmp；
#   v8 无视该开关强制沙箱、按清单断网、隐藏 Gateway 家目录，只放行插件目录与安装前缀；
#   沙箱不可用时这里抛 SandboxUnavailable，调用方应先用 plugin_sandbox_problem 结构化拒绝。
#   host_exclusive_roots 是宿主冻结的可信来源（含配置外部 file: 凭据），只进隐藏规格，不进插件 argv、env 或公开结果。
#   可选 config 只绑定能力包连接预算及展示缓存容量，不改变普通插件连接默认或激活授权。
    # 函数用途: 将已准备的插件环境（Python 或 v6 随包文件）接到原 MCP 客户端，不在构造时启动进程；runtime_repo 是 owner 权威库，供观察新鲜度复核只读。
    def __init__(self, owner, installation: PluginInstallation, *, runtime_repo: object | None = None,
                 process_sandbox: bool = False, host_credential_env_names: frozenset[str] = frozenset(),
                 host_exclusive_roots: tuple[Path, ...] = (), config: object | None = None):
        self.runtime_repo = runtime_repo
        self.lazy = bool(getattr(installation.manifest, "has_mcp_runtime", False))
        self._lazy_lock = threading.Lock()
        self.attempts, self.retry_at = 0, 0.0
        self.drift = False
        self._in_flight = 0
        self.last_used = time.monotonic()
        # 首次启动与重连都只用构造时冻结的这份来源集合；重连沿 _source_sandbox_spec，不重新推导。
        self._host_exclusive_roots = tuple(Path(path) for path in (host_exclusive_roots or ()))
        activation = installation.activation
        if activation is None or activation.phase not in {"preparing", "active"}:
            raise ValueError("插件没有可启动的固定激活")
        self.activation_ref = PluginActivationRef.from_owner(
            owner, installation.manifest.plugin_id, activation.activation_id, config=config)
        if self.lazy and activation.phase == "active":
            # 构造只生成展示门面，可复用同表已解码投影；实际启动与发送仍鲜活 require。
            if not self.activation_ref.available():
                raise ValueError("能力包激活已不可用")
        else:
            current = self.activation_ref.require(allow_preparing=True)
            if current != installation:
                raise ValueError("插件安装已变化")
        environment = owner.plugins_dir / "environments" / activation.plan.environment_ref
        command, args, cwd = _launch_argv(owner, installation.manifest, environment, activation.plan.interpreter_fingerprint)
        self._source_raw_argv = (command, *args)
        self._source_sandbox_spec = None
        self._source_configurations = ()
        # 插件自有数据按 owner + 插件 ID 固定一处，跨版本、停用和重新启用保留；卸载默认也不清（见 PLUGIN_LIFECYCLE）
        data_dir = plugin_data_dir(owner, installation.manifest.plugin_id)
        descriptor = open_directory_beneath(owner.root, data_dir.relative_to(owner.root).parts, create=True)
        os.close(descriptor)
        # B7：v8 事件插件（声明了 permissions）无条件强制进沙箱——不管调用方传的 process_sandbox 开没开；
        # 按声明断网、隐藏 Gateway 整个家目录，只放行插件环境/数据目录与解释器安装前缀。
        # 老格式 restricted 走同一底座：构造前复核固定授权，策略来自固定 permission_json，不再停 pending。
        v8_permissions = installation.manifest.permissions
        legacy_policy = legacy_launch_policy(owner, installation) if v8_permissions is None else None
        sandbox_env = {}
        if process_sandbox or v8_permissions is not None or legacy_policy is not None:
            os.close(open_directory_beneath(owner.root, (*data_dir.relative_to(owner.root).parts, SANDBOX_TMP_DIRECTORY),
                                            create=True))
            policy = (_plugin_v8_sandbox(owner, installation, command, self._host_exclusive_roots)
                      if v8_permissions is not None else legacy_policy)
            if legacy_policy is not None:
                _require_legacy_policy_ready(owner, legacy_policy, self._host_exclusive_roots)
            spec = plugin_sandbox_spec(cwd=cwd, data_dir=data_dir, owner_home=owner.home_dir, sandbox_policy=policy,
                                       host_exclusive_roots=self._host_exclusive_roots)
            self._source_sandbox_spec = spec
            command, *args = sandboxed_plugin_argv([command, *args], spec)
            sandbox_env = {"TMPDIR": str(data_dir / SANDBOX_TMP_DIRECTORY)}
        settings = canonical_plugin_settings(load_strict_json(installation.settings_json or "{}"),
                                             installation.manifest.settings_schema)
        self.installation = installation
        self.validated_tools: tuple[PluginProxyTool, ...] = ()
        # v9不要求read隐含授权；任一显式能力申请都取得自己的v2入口，令牌仍只绑定本激活。
        host_api_env = (issue_host_api_env(self.activation_ref, installation.manifest.plugin_id)
                        if installation.manifest.host_api_v9
                        or HOST_API_READ in installation.manifest.host_api else {})
        super().__init__(MCPServerConfig(
            name="plugin_" + installation.manifest.plugin_id,
            command=command, args=args, cwd=str(cwd),
            env={"MY_AGENT_PLUGIN_SETTINGS": settings, PLUGIN_DATA_DIR_ENV: str(data_dir), **host_api_env, **sandbox_env},
            host_credential_env_names=frozenset(host_credential_env_names),
            catalog_category="plugins",
        ), activation=self.activation_ref)
        if self.lazy:
            self.config.connect_timeout = _pack_connect_timeout(config)
        self.pack_tools: tuple[PackMCPProxyTool, ...] = tuple(
            self._proxy_for_info(MCPToolInfo(tool.name, tool.description, tool.input_schema), None, facade=True)
            for tool in installation_tool_declarations(installation)
        ) if self.lazy else ()
        register_source_registry(self)

    def ensure_started(self) -> object:
        """一把客户端锁串行完成启动/发现/发布；只尝试一次，撤销门和原资源清理不得被缓存绕过。"""
        with self._lazy_lock:
            if self.drift:
                raise MCPError("能力包目录已变化，须重新启用", code="MCP_PACK_CATALOG_DRIFT", effect_outcome="not_started")
            if time.monotonic() < self.retry_at:
                raise MCPError("能力包服务处于退避期", code="MCP_PACK_SERVER_UNAVAILABLE", effect_outcome="not_started")
            try:
                self.activation_ref.require()
            except (OSError, ValueError) as exc:
                raise MCPError("能力包激活已不可用", code="PLUGIN_ACTIVATION_UNAVAILABLE", effect_outcome="not_started") from exc
            if self.is_running() and not self.tools_changed and self.validated_tools:
                transport = self.connection()
                if all(tool.transport is transport for tool in self.validated_tools):
                    return transport
            # 已验连接在调用间崩溃：先记录一次退避，不让其它工具或 prepare 承担重启成本。
            if not self.is_running() and self.validated_tools and not self.attempts:
                self.validated_tools = ()
                self._advance_lazy_retry()
                raise MCPError("能力包服务已退出", code="MCP_PACK_SERVER_UNAVAILABLE", effect_outcome="not_started")
            transport = None
            try:
                transport = self.start() if self._transport is None else self.reconnect()
                tools = self.discover_tools(transport)
                self.publish_discovered_tools(transport, tools)
                self.attempts, self.retry_at = 0, 0.0
                return transport
            except Exception as exc:
                self.validated_tools = ()
                if transport is not None:
                    disconnect_failed_mcp_transport(self, transport)
                if isinstance(exc, MCPError) and exc.code == "MCP_PACK_CATALOG_DRIFT":
                    self.drift = True
                    self.stop()
                    raise
                self._advance_lazy_retry()
                code = "PLUGIN_ACTIVATION_UNAVAILABLE" if not self.activation_ref.available() else "MCP_PACK_SERVER_UNAVAILABLE"
                raise MCPError("能力包服务暂不可用", code=code, effect_outcome="not_started") from exc

    def _advance_lazy_retry(self) -> None:
        """调用方持 lazy 锁；退避与普通 MCP 同公式，只属于这一个固定激活。"""
        self.attempts += 1
        self.retry_at = time.monotonic() + min(60.0, float(2 ** min(self.attempts - 1, 6)))

    def idle_stop_if_due(self, now, idle_stop_seconds):
        """运行边界精确回收空闲 transport，保留客户端；永久 stop 事件不能清除或复活。"""
        if not self._lazy_lock.acquire(blocking=False):
            return None
        try:
            if (not self.lazy or not self.is_running() or self._in_flight or idle_stop_seconds <= 0
                    or now - self.last_used < idle_stop_seconds):
                return None
            transport = self.connection()
            receipt = self.disconnect(transport=transport)
            if receipt.confirmed:
                self.validated_tools = ()
            return receipt
        finally:
            self._lazy_lock.release()

    # LLM: 先拿来源协调锁、再由原MCP取连接锁；只在无旧transport时从未包装argv重建最新投影，不重绑活连接。
    # 函数用途: 鲜活复核固定激活和来源版本后启动插件，旧策略失效须原资源清理再重连。
    def start(self):
        with source_process_boundary():
            self._require_legacy_launch_facts()
            if self.is_running() and not self._source_configurations_current():
                self._retire_source_transport()
                raise MCPError("插件来源版本已变化，须重连", code="MCP_CONNECTION_CLOSED")
            with self._state_lock:
                fresh = self._transport is None and not self.is_closed()
            if fresh:
                self._refresh_source_launch()
            return super().start()

    # LLM: 先鲜活读表确认固定激活仍在（未撤销、未换代），再按固定 permission_json 重算全部事实。
    # 函数用途: 为本次启动复核老格式插件授权；事实变化一律拒绝，不自动换绑、不降 wide。
    def _require_legacy_launch_facts(self) -> None:
        installation = self.installation
        if installation.manifest.permissions is not None or installation.manifest.is_content_only:
            return
        try:
            current = self.activation_ref.require(allow_preparing=True)
        except (OSError, ValueError) as exc:
            raise LegacyLaunchDenied("legacy_permission_changed", "固定激活已不可用，本次没有启动。") from exc
        if current != installation:
            raise LegacyLaunchDenied("legacy_permission_changed", "固定激活已变化，本次没有启动。")
        legacy_launch_policy(self.activation_ref.owner(), current)

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
        declared = {tool.name: tool for tool in installation_tool_declarations(self.installation)}
        code = "MCP_PACK_CATALOG_DRIFT" if self.lazy else "MCP_PROTOCOL_ERROR"
        pin = getattr(self.installation.activation, "pinned_catalog_json", None)
        if pin is not None:
            try:
                if pinned_catalog_json_from_tools(actual) != pin:
                    raise ValueError("固定目录已变化")
            except ValueError as exc:
                raise MCPError("能力包固定目录已变化", code=code, effect_outcome="not_started") from exc
        if len(actual) != len(declared) or {tool.name for tool in actual} != set(declared):
            raise MCPError("插件工具集合与安装声明不一致", code=code, effect_outcome="not_started")
        proxies = []
        for info in actual:
            expected = declared[info.name]
            try:
                schema = canonicalize_tool_input_schema(info.input_schema)
            except (TypeError, ValueError) as exc:
                raise MCPError("插件输入结构不可执行", code=code, effect_outcome="not_started") from exc
            if ((pin is None and info.description != expected.description)
                    or schema != expected.input_schema):
                raise MCPError("插件工具说明或输入结构与安装声明不一致", code=code, effect_outcome="not_started")
            proxies.append(self._proxy_for_info(info, transport))
        if len({tool.model_spec.name for tool in proxies}) != len(proxies):
            raise MCPError("插件工具名称冲突", code="MCP_PROTOCOL_ERROR")
        return tuple(proxies)

    def _proxy_for_info(self, info, transport, *, facade=False):
        """门面与连接代理共用 Schema/危险效果/内部参数，只有连接所有权不同。"""
        proxy = build_proxy_tool(self, self.config.name, info, transport=transport, catalog_category="plugins")
        name = plugin_tool_name(self.installation.manifest.plugin_id, info.name)
        policy = replace(proxy.runtime_policy, resource_scopes=ResourceScopePolicy(
            "declared", static_scopes=(f"logical:plugin:{self.activation_ref.scope.activation_id}:{info.name}",),
        ), input_policy=replace(proxy.runtime_policy.input_policy, internal_parameters=tuple(dict.fromkeys(
            (*proxy.runtime_policy.input_policy.internal_parameters, "__operation_id", "__run_scope")))))
        cls = PackMCPProxyTool if facade else PluginProxyTool
        return cls(self, info.name, self._plugin_model_spec(proxy.model_spec, name, info), policy, transport=transport)

    # LLM: 只改模型可见的说明与检索提示（软信息），不改权限、效果或实现身份；插件 ID 与简介来自已安装包描述。
    #   让模型、tool_search 与能力推荐都能按"插件 <ID>"找到它，而不是只看到通用 MCP 文案和哈希后的工具名。
    # 函数用途: 生成带插件身份的工具说明和关键词。
    def _plugin_model_spec(self, spec, name: str, info):
        manifest = self.installation.manifest
        plugin_id = manifest.plugin_id
        description = f"插件 {plugin_id}（{' '.join(manifest.summary.split())}）的 {info.name} 工具。{info.description}"
        keywords = tuple(dict.fromkeys((*spec.hints.keywords, plugin_id, plugin_id.replace("-", "_"),
                                        *plugin_id.replace("_", "-").split("-"), info.name)))
        hints = replace(spec.hints, keywords=keywords, provider_id="plugin:" + plugin_id)
        if getattr(self, "lazy", False):
            hints = replace(hints, provider_id="package:" + plugin_id, default_deferred=True,
                            deferred_summary=manifest.summary)
        return replace(spec, name=name, description=sanitize_credentials(description), hints=hints)

    # LLM: 缓存只在原连接/激活再次核验后整体替换；当前安装版本不能改变同连接代理的实现身份。
    # 函数用途: 为下一轮缓存完整工具贡献，调用方仍须在自己的权限视图中投影。
    def publish_discovered_tools(self, transport, tools: tuple[PluginProxyTool, ...]) -> None:
        # LLM: 回调只换内存元组，不取安装写锁、发请求或写持久状态。
        # 函数用途: 在原 MCP 发布临界区保存已构造的同连接工具。
        def publish():
            self.validated_tools = tools

        self.publish_tools(transport, publish)


    # LLM: 原重连仍先核旧树清理；外层锁固定为来源→连接，避免set_source通知与基类重连反序死锁。
    # 函数用途: 清退失效来源连接，再由原MCP创建最新策略的固定transport。
    def reconnect(self):
        with source_process_boundary():
            if self._transport is not None and not self._source_configurations_current():
                self._retire_source_transport()
            return super().reconnect()


    # LLM: 只用固定激活owner、原未包装argv和原沙箱权限spec；来源从唯一repository现读，替换config不修改旧transport。
    # 函数用途: 为下一次实际启动刷新来源CAS和平台argv，禁止嵌套包装或沿用构造时的旧profile。
    def _refresh_source_launch(self):
        spec = self._source_sandbox_spec
        configurations = process_source_configurations(self.activation_ref.owner().home_dir,
                                                       full_access=bool(spec and spec.full_access))
        argv = list(self._source_raw_argv)
        if spec is not None:
            argv = sandboxed_plugin_argv(argv, spec)
        self.config = replace(self.config, command=argv[0], args=argv[1:])
        self._source_configurations = configurations


    # LLM: 固定激活owner不来自模型；沙箱FullAccess依赖集合每次鲜活复核，普通插件只比较本owner。
    # 函数用途: 判断当前连接来源版本及owner保护集合是否仍可用。
    def _source_configurations_current(self):
        spec = self._source_sandbox_spec
        return process_source_configurations_current(self._source_configurations,
                                                     owner_home=self.activation_ref.owner().home_dir,
                                                     full_access=bool(spec and spec.full_access))


    # LLM: 仅消费结构化完整CAS通知；资源仍是本客户端原transport/Store，未确认停止不放行来源提交。
    # 函数用途: 切源前临时撤销旧插件连接，不永久关闭客户端，使确认清理后能按新策略重连。
    def retire_agents_source(self, owner_home, proposed_cas):
        spec = self._source_sandbox_spec
        if process_source_change_affects(self._source_configurations, owner_home, proposed_cas,
                                         bound_scope=(self.activation_ref.owner().home_dir,
                                                      bool(spec and spec.full_access))):
            if str(self.activation_ref.owner().home_dir) != owner_home:
                raise AgentsContractError("AGENTS_SOURCE_RESOURCE_UNCONFIRMED", "cross_owner_plugin")
            self._retire_source_transport()


    # LLM: 撤销和清理沿原MCP/Managed Store；原句柄保留供reconnect核回执，不靠Popen.poll猜后代已退出。
    # 函数用途: 请求原插件资源退役，清理未知时抛错而不启动替代连接。
    def _retire_source_transport(self):
        with self._state_lock:
            transport = self._transport
            launch_unknown = self._starting or self._launch_failure is not None
        if transport is None:
            if launch_unknown:
                raise AgentsContractError("AGENTS_SOURCE_RESOURCE_UNCONFIRMED", "plugin_launch")
            return
        receipt = self.disconnect(transport=transport)
        if not receipt.confirmed:
            raise AgentsContractError("AGENTS_SOURCE_RESOURCE_UNCONFIRMED", "plugin_resource")
