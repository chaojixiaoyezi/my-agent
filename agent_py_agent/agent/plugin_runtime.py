# LLM: 插件连接沿原 MCP 客户端和托管资源账，缓存只保存固定连接的已验代理；安装表仍是唯一激活权威。
# 模块用途: 从已准备环境构造隔离服务，完整核对工具声明，并为原执行器生成固定代次的工具。

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, replace

from .common.nofollow_fs import open_directory_beneath
from .common.strict_json import load_strict_json
from .plugin_activation_ref import PluginActivationRef
from .plugin_entry import FILES_DIRECTORY
from .plugin_host_api import HOST_API_READ, issue_host_api_env
from .plugin_installation import PluginInstallation
from .plugin_manifest import PluginToolDeclaration, canonical_plugin_settings
from .plugin_observation import (
    OBSERVATION_CANDIDATE_UNKNOWN,
    OBSERVATION_ERROR_KEY,
    OBSERVATION_KEY,
    OBSERVATION_META_EXTENSION,
    OBSERVATION_STALE,
    ObservationHostContext,
    ObservationRejected,
    observation_meta,
    parse_observation,
    resolve_action_candidate,
)

# 插件层候选复核失败码 → 宿主结构化 reported_error_code；插件合同保证这两种拒绝不产生副作用
_PLUGIN_OBSERVATION_ERROR_CODES = {"stale": OBSERVATION_STALE, "not_found": OBSERVATION_CANDIDATE_UNKNOWN}
from .plugin_runtime_facts import verified_runtime_command
from .plugin_sandbox import SANDBOX_TMP_DIRECTORY, sandboxed_plugin_argv
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
    # 函数用途: 停用后的插件调用固定报不可用且未执行；动作工具填了候选 ID 先复核新鲜度再发送；观察工具成功后校验并铸 ID。
    def _execute(self, params: dict[str, object], context: ToolInvocationContext | None) -> ToolHandlerOutcome:
        if self._activation_revoked():
            return ToolHandlerOutcome(
                self.model_spec.name, False, '{"error": "原插件已停用或激活不可用"}',
                error_code="TOOL_UNAVAILABLE", reported_error_code="PLUGIN_ACTIVATION_UNAVAILABLE",
                effect_outcome="not_started",
            )
        declared = self._declared_tool()
        extra_meta = None
        if declared is not None and declared.observation_ref is not None:
            try:
                extra_meta = self._observation_action_meta(params, declared)
            except ObservationRejected as exc:
                return ToolHandlerOutcome(
                    self.model_spec.name, False,
                    json.dumps({"error": "候选已过期或不存在，请先重新观察再操作", "code": exc.code}, ensure_ascii=False),
                    error_code="TOOL_INVALID_ARGUMENTS", reported_error_code=exc.code, effect_outcome="not_started",
                    result_envelope={"observation_rejected": exc.code},
                )
        outcome = self._execute_with_meta(params, context, extra_meta)
        if declared is not None and declared.observation is not None and outcome.ok:
            outcome = self._attach_observation(outcome, params, context, declared)
        if extra_meta is not None and not outcome.ok:
            outcome = self._lift_observation_error(outcome)
        return outcome

    # LLM: 插件按代次复核候选后拒绝执行时，在 isError 结果的 structuredContent.my_agent_observation_error.code 里给结构化原因
    #   （stale / not_found）。这里把它提升成宿主的 reported_error_code（OBSERVATION_STALE / OBSERVATION_CANDIDATE_UNKNOWN）、
    #   error_code TOOL_INVALID_ARGUMENTS、effect_outcome not_started（插件合同：这两种拒绝零副作用），信封记 observation_rejected；
    #   其它错误原样保留，不解析正文。
    # 函数用途: 让候选过期/不存在成为可结构化分支的失败，而不是笼统的 TOOL_EXECUTION_FAILED。
    def _lift_observation_error(self, outcome: ToolHandlerOutcome) -> ToolHandlerOutcome:
        try:
            payload = json.loads(outcome.output)
        except (TypeError, ValueError):
            return outcome
        structured = payload.get("structuredContent") if isinstance(payload, dict) else None
        error = structured.get(OBSERVATION_ERROR_KEY) if isinstance(structured, dict) else None
        code = _PLUGIN_OBSERVATION_ERROR_CODES.get(str(error.get("code") or "")) if isinstance(error, dict) else None
        if code is None:
            return outcome
        return replace(
            outcome, error_code="TOOL_INVALID_ARGUMENTS", reported_error_code=code, effect_outcome="not_started",
            result_envelope={**outcome.result_envelope, "observation_rejected": code},
        )

    # LLM: 只在模型填了 observation_ref.param 时复核：候选按当前 run/task 的权威事件流解析，过期/未知抛 ObservationRejected
    #   （调用方转成 TOOL_INVALID_ARGUMENTS、not_started，不发送）；没填参数保持现状（如按选择器执行）。复核通过才把插件自己的
    #   key 与目标代次放进 _meta，arguments 不能冒充。
    # 函数用途: 生成动作调用要附给插件的观察 _meta，或判定候选不可用。
    def _observation_action_meta(self, params: dict[str, object], declared: PluginToolDeclaration) -> dict[str, object] | None:
        candidate_id = params.get(declared.observation_ref.param)
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            return None
        scope = params.get("__run_scope") if isinstance(params.get("__run_scope"), dict) else {}
        observation, candidate = resolve_action_candidate(
            getattr(self.client, "runtime_repo", None), run_id=str(scope.get("run_id") or ""),
            task_id=str(scope.get("task_id") or ""), candidate_id=candidate_id.strip(), action_tool=self.model_spec.name,
        )
        return {OBSERVATION_META_EXTENSION: observation_meta(observation, candidate)}

    # LLM: 只处理成功结果里 structuredContent.my_agent_observation；形状合规则铸 ID、把归档权威写进 result_envelope.observation，
    #   模型可见投影只留 candidate_id/role/label/actions；不合规整份丢弃、模型看不到候选，信封记 observation_rejected 原因码。
    #   身份全部取宿主：run/task 来自 __run_scope、operation 来自 __operation_id、激活来自固定 client，不取插件自报。
    # 函数用途: 把插件的观察载荷变成宿主的结构化观察事实。
    def _attach_observation(self, outcome: ToolHandlerOutcome, params: dict[str, object],
                            context: ToolInvocationContext | None, declared: PluginToolDeclaration) -> ToolHandlerOutcome:
        try:
            payload = json.loads(outcome.output)
        except (TypeError, ValueError):
            return outcome
        structured = payload.get("structuredContent") if isinstance(payload, dict) else None
        if not isinstance(structured, dict) or OBSERVATION_KEY not in structured:
            return outcome
        envelope = dict(outcome.result_envelope)
        try:
            record = parse_observation(structured[OBSERVATION_KEY], self._observation_context(params, context, declared))
        except ObservationRejected as exc:
            structured.pop(OBSERVATION_KEY, None)
            envelope["observation_rejected"] = exc.code
        else:
            structured[OBSERVATION_KEY] = record.model_projection()
            envelope["observation"] = record.to_envelope()
        return replace(outcome, output=json.dumps(payload, ensure_ascii=False), result_envelope=envelope)

    # 函数用途: 汇集铸 ID 所需的宿主身份与本包同类动作工具的注册名映射。
    def _observation_context(self, params: dict[str, object], context: ToolInvocationContext | None,
                             declared: PluginToolDeclaration) -> ObservationHostContext:
        scope = params.get("__run_scope") if isinstance(params.get("__run_scope"), dict) else {}
        snapshot = getattr(context, "runtime_snapshot", None)
        manifest = self.client.installation.manifest
        kind = declared.observation.target_kind
        return ObservationHostContext(
            run_id=str(scope.get("run_id") or getattr(snapshot, "run_id", "") or ""),
            task_id=str(scope.get("task_id") or ""), operation_id=str(params.get("__operation_id") or ""),
            activation_id=self.client.activation_ref.scope.activation_id, plugin_id=manifest.plugin_id,
            tool_name=self.model_spec.name, target_kind=kind, max_candidates=declared.observation.max_candidates,
            action_tools={tool.name: plugin_tool_name(manifest.plugin_id, tool.name) for tool in manifest.tools
                          if tool.observation_ref is not None and tool.observation_ref.target_kind == kind},
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


# LLM: 一个客户端只属于安装表中的固定代次；沿原 MCP 重连/关闭与资源登记，不接收包提供的 owner、argv 或宿主地址。
# 类用途: 保存插件连接和同连接已验工具，供共享权限视图分别生成目录。
class PluginMCPClient(MCPStdioClient):
    # LLM: 构造只读规范环境和设置；私有值只放子进程环境，包声明的 effect 不降低原危险工具门。
#   同时 no-follow 创建插件数据目录（有副作用：可能新建目录），经 MY_AGENT_PLUGIN_DATA_DIR 传给子进程；
#   声明了 host_api=["read"] 的包另获宿主只读 API 地址与令牌（有副作用：登记令牌）。
#   process_sandbox=True（配置 plugin_process_sandbox）时启动命令套进平台沙箱，只可写数据目录，TMPDIR 指向其中的 .tmp；
#   沙箱不可用时这里抛 SandboxUnavailable，调用方应先用 plugin_sandbox_problem 结构化拒绝。
    # 函数用途: 将已准备的插件环境（Python 或 v6 随包文件）接到原 MCP 客户端，不在构造时启动进程；runtime_repo 是 owner 权威库，供观察新鲜度复核只读。
    def __init__(self, owner, installation: PluginInstallation, *, runtime_repo: object | None = None,
                 process_sandbox: bool = False):
        self.runtime_repo = runtime_repo
        activation = installation.activation
        if activation is None or activation.phase not in {"preparing", "active"}:
            raise ValueError("插件没有可启动的固定激活")
        self.activation_ref = PluginActivationRef.from_owner(owner, installation.manifest.plugin_id, activation.activation_id)
        current = self.activation_ref.require(allow_preparing=True)
        if current != installation:
            raise ValueError("插件安装已变化")
        environment = owner.plugins_dir / "environments" / activation.plan.environment_ref
        command, args, cwd = _launch_argv(owner, installation.manifest, environment, activation.plan.interpreter_fingerprint)
        # 插件自有数据按 owner + 插件 ID 固定一处，跨版本、停用和重新启用保留；卸载默认也不清（见 PLUGIN_LIFECYCLE）
        data_dir = plugin_data_dir(owner, installation.manifest.plugin_id)
        descriptor = open_directory_beneath(owner.root, data_dir.relative_to(owner.root).parts, create=True)
        os.close(descriptor)
        sandbox_env = {}
        if process_sandbox:
            os.close(open_directory_beneath(owner.root, (*data_dir.relative_to(owner.root).parts, SANDBOX_TMP_DIRECTORY),
                                            create=True))
            command, *args = sandboxed_plugin_argv([command, *args], cwd=cwd, data_dir=data_dir, owner_home=owner.home_dir)
            sandbox_env = {"TMPDIR": str(data_dir / SANDBOX_TMP_DIRECTORY)}
        settings = canonical_plugin_settings(load_strict_json(installation.settings_json or "{}"),
                                             installation.manifest.settings_schema)
        self.installation = installation
        self.validated_tools: tuple[PluginProxyTool, ...] = ()
        # 只有声明了宿主只读 API 的包才拿到地址和令牌；令牌绑定本激活，停用/换代后宿主侧即失效
        host_api_env = (issue_host_api_env(self.activation_ref, installation.manifest.plugin_id)
                        if HOST_API_READ in getattr(installation.manifest, "host_api", ()) else {})
        super().__init__(MCPServerConfig(
            name="plugin_" + installation.manifest.plugin_id,
            command=command, args=args, cwd=str(cwd),
            env={"MY_AGENT_PLUGIN_SETTINGS": settings, PLUGIN_DATA_DIR_ENV: str(data_dir), **host_api_env, **sandbox_env},
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
            # 观察 ID 与新鲜度复核需要宿主的 run/task/operation 身份：只经声明的宿主参数注入，模型参数不能冒充，发送前会被过滤
            policy = replace(proxy.runtime_policy, resource_scopes=ResourceScopePolicy(
                "declared", static_scopes=(f"logical:plugin:{self.activation_ref.scope.activation_id}:{info.name}",),
            ), input_policy=replace(proxy.runtime_policy.input_policy, internal_parameters=tuple(dict.fromkeys(
                (*proxy.runtime_policy.input_policy.internal_parameters, "__operation_id", "__run_scope")))))
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
