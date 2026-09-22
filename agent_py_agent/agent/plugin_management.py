# LLM: 管理分路只消费宿主已认证身份、当前权限与原线程 Store；有副作用必须经过原宿主运行和工具执行链。
# 模块用途: 组合目录、安装、配置、启停及原请求查询；不初始化完整 Agent，所有副作用走原管理执行器。

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .command_catalog import COMMAND_INDEX
from .path_access_policy import PathAccessPolicy
from .plugin_command_service import execute_plugin_command, read_plugin_catalog
from .plugin_commands import parse_plugin_command, plugin_command_response, plugin_namespace
from .plugin_configure_tool import PLUGIN_CONFIGURE_TOOL, PluginConfigureTool
from .plugin_disable_tool import PLUGIN_DISABLE_TOOL, PluginDisableTool
from .plugin_enable_tool import PLUGIN_ENABLE_TOOL, PluginEnableTool
from .plugin_install_store import PluginInstallStore
from .plugin_install_tool import PLUGIN_INSTALL_TOOL, PluginInstallTool
from .runtime_db.host_command_execution import execute_host_command, query_host_command
from .runtime_db.host_commands import HostCommandIdentity, HostCommandRequest
from .runtime_db.managed_operation_store import ManagedOperationStore
from .runtime_db.operations import RuntimeConflictError
from .runtime_db.repository import RuntimeRepository
from .runtime_db.schema import runtime_db_path
from .tooling.executor import ToolExecutorRequest
from .tooling.models import ToolAvailability, ToolExposure, ToolRuntime, ToolRuntimeSnapshot
from .tooling.runtime_contracts import ToolCall, tool_arguments_hash
from .user_space.owner_resolver import OwnerHomeResult

_MANAGEMENT_TOOLS = {"install": PLUGIN_INSTALL_TOOL, "configure": PLUGIN_CONFIGURE_TOOL,
                     "enable": PLUGIN_ENABLE_TOOL, "disable": PLUGIN_DISABLE_TOOL}


# LLM: 所有字段来自宿主，布尔授权不是客户端参数；ThreadStore 是原会话权威，目录投影不能提供执行身份。
# 类用途: 给安装、配置和启停传所需身份、权限及存储依赖，不携带完整 Agent。
@dataclass(frozen=True)
class PluginManagementContext:
    owner: OwnerHomeResult
    actor_id: str
    channel: str
    conversation_id: str
    threads: object
    workspace: Path
    path_policy: PathAccessPolicy
    is_admin: bool
    enabled: bool
    tool_allowed: bool = True
    configure_allowed: bool = True
    disable_allowed: bool = True
    enable_allowed: bool = True


# LLM: 冷入口与完整代理共用 owner 权限裁决；各管理工具分别受禁用策略约束，查询仍绑定原身份。
# 函数用途: 从当前私有配置和原线程路径组装管理依赖，不创建 owner、线程或完整 Agent。
def plugin_management_context(
    owner: OwnerHomeResult, home: object, config: object, threads: object, *,
    actor_id: str, channel: str, conversation_id: str, is_admin: bool,
) -> PluginManagementContext:
    from .user_space.approval_mode import permission_config
    from .user_space.owner_access import resolve_owner_scope_and_access
    from .user_space.owner_policy import resolve_effective_owner_policy

    policy = resolve_effective_owner_policy(home)
    allowed = not policy.load_errors and bool(config.enable_tools)
    try:
        effective = permission_config(config, home)
    except (OSError, ValueError):
        effective, allowed = config, False
    scope, access = resolve_owner_scope_and_access(home, effective, policy)
    path_policy = PathAccessPolicy.from_values(
        mode="full" if access == "full-access" and not scope else effective.path_access_mode,
        dangerous_roots=effective.path_dangerous_roots, owner_scope_root=scope,
    )
    thread, error = threads.resolve_report(channel=channel, channel_conversation_id=conversation_id,
                                           channel_user_id=actor_id)
    if error:
        raise RuntimeConflictError("原会话记录不可读")
    workspace = Path(thread.cwd) if thread is not None and thread.cwd else owner.home_dir
    return PluginManagementContext(owner, actor_id, channel, conversation_id, threads, workspace,
                                   path_policy, is_admin, config.enable_plugins,
                                   allowed and PLUGIN_INSTALL_TOOL not in policy.disabled_tools,
                                   allowed and PLUGIN_CONFIGURE_TOOL not in policy.disabled_tools,
                                   allowed and PLUGIN_DISABLE_TOOL not in policy.disabled_tools,
                                   allowed and PLUGIN_ENABLE_TOOL not in policy.disabled_tools)


# LLM: 一个 owner 只有一个安装事实 Store 和原 runtime.db；实例本身无后台任务、可变注册表或业务历史。
# 类用途: 承接显式插件命令，安装默认停用，启用先验完整服务，撤销清理精确资源，查询只读原结果。
class PluginManagement:
    # LLM: 构造不创建目录或线程；后续写入前仍须显式检查管理员与开关。
    # 函数用途: 保存这次已鉴权入口的必要依赖。
    def __init__(self, context: PluginManagementContext) -> None:
        self.context = context
        self.installations = PluginInstallStore(context.owner)

    # LLM: 声明只投影唯一安装表，不扫描包目录；安装版本参与摘要，值与值摘要不进入公开投影。
    # 函数用途: 为帮助与补全读取当前 owner 的公开声明。
    def catalog(self):
        return self._catalog(self.installations.snapshot())

    # LLM: 调用方传同一次安装快照，避免目录摘要与待修改记录分别读取产生竞态；动作可用性不授予权限。
    # 函数用途: 将当前权限及同一份安装事实投影成不含私有配置的目录。
    def _catalog(self, entries):
        context = self.context
        actions = tuple(replace(action, available=(
            action.name in {"help", "list", "info", "status"}
            or action.name in _MANAGEMENT_TOOLS and context.enabled and context.is_admin
            and self._allowed(_MANAGEMENT_TOOLS[action.name])
        )) for action in COMMAND_INDEX["plugins"].actions)
        return replace(read_plugin_catalog(context.owner.identity, channel=context.channel,
                                           conversation_id=context.conversation_id),
                       management_actions=actions,
                       plugins=tuple(replace(row.manifest.command_spec, installation_revision=row.revision,
                                             enabled=row.enabled, activation_id=row.activation_id)
                                     for row in entries))

    # LLM: 状态查询和已接受请求先读原账，不先打开来源；参数错误与帮助沿公共解析，不进入普通聊天。
    # 函数用途: 执行一次明确管理命令，停用也走原请求链，并附同一权威的当前目录。
    def command(self, text: str, *, revision: str, request_id: str) -> dict:
        namespace = plugin_namespace(text)
        if namespace is None:
            raise ValueError("不是插件命令")
        if namespace.plugin_id:
            return execute_plugin_command(self.catalog(), text, revision=revision)
        static = plugin_command_response(text)
        if static.get("reason") not in {None, "not_implemented"}:
            return self._reply(static)
        parsed = parse_plugin_command(text)
        if parsed.help_requested:
            return execute_plugin_command(self.catalog(), text, revision=revision)
        values = parsed.arguments.values
        if parsed.action.name == "status":
            return self._reply(self._query(values["request"]), values["request"])
        if parsed.action.name in _MANAGEMENT_TOOLS:
            tool_name = _MANAGEMENT_TOOLS[parsed.action.name]
            return self._reply(self._submit(tool_name, dict(values), revision, request_id), request_id)
        catalog = self.catalog()
        if not revision or revision != catalog.revision:
            return execute_plugin_command(catalog, text, revision=revision)
        if parsed.action.name == "list":
            entries = [item for item in catalog.plugins if not values.get("enabled") or item.enabled]
            return self._reply({"ok": True, "message": "\n".join(
                f"{item.plugin_id} {item.package_version}（{'启用' if item.enabled else '停用'}）"
                for item in entries) or "当前没有符合条件的已安装插件。"})
        if parsed.action.name == "info":
            plugin = next((item for item in catalog.plugins if item.plugin_id == values["plugin"]), None)
            return self._reply({"ok": plugin is not None, "message": (
                f"{plugin.plugin_id} {plugin.package_version}（{'启用' if plugin.enabled else '停用'}）\n{plugin.summary}"
                if plugin else "未找到该插件。")})
        return execute_plugin_command(catalog, text, revision=revision)

    # LLM: 明确动作共用原请求链；认证和开关先于新写入，旧请求先读账，不得将旧停用重放到新激活。
    # 函数用途: 提交管理请求，首次必须携带已见目录版本，重送复用原操作。
    def _submit(self, tool_name: str, values: dict, revision: str, request_id: str) -> dict:
        if not self.context.is_admin:
            return {"ok": False, "state": "rejected", "error_code": "PLUGIN_PERMISSION_DENIED"}
        try:
            _validate_request_id(request_id)
        except ValueError:
            return {"ok": False, "state": "rejected", "error_code": "INVALID_COMMAND_ARGUMENTS"}
        arguments = self._arguments(tool_name, values, revision)
        thread = self._thread(create=False)
        if thread is not None and runtime_db_path(self.context.owner.home_dir).is_file():
            repo = RuntimeRepository(runtime_db_path(self.context.owner.home_dir))
            request = self._request(thread.thread_id, request_id, tool_name, arguments)
            if repo.find_host_command(request) is not None:
                if not self.context.enabled:
                    return query_host_command(repo, request)
                return execute_host_command(repo, request, lambda binding: self._prepare(repo, binding, arguments))
        if not self.context.enabled:
            return {"ok": False, "state": "rejected", "error_code": "PLUGIN_DISABLED"}
        if not revision or revision != self.catalog().revision:
            return {"ok": False, "state": "rejected", "error_code": "PLUGIN_CATALOG_STALE"}
        thread = self._thread(create=True)
        repo = RuntimeRepository(runtime_db_path(self.context.owner.home_dir))
        request = self._request(thread.thread_id, request_id, tool_name, arguments)
        return execute_host_command(repo, request, lambda binding: self._prepare(repo, binding, arguments))

    # LLM: 查询仍要求当前管理员身份且绑定原操作者；不创建线程、数据库、attempt 或对账工作。
    # 函数用途: 按稳定请求编号读取本会话的安装结果。
    def _query(self, request_id: str) -> dict:
        _validate_request_id(request_id)
        if not self.context.is_admin:
            return {"ok": False, "state": "rejected", "error_code": "PLUGIN_PERMISSION_DENIED"}
        thread = self._thread(create=False)
        path = runtime_db_path(self.context.owner.home_dir)
        if thread is None or not path.is_file():
            return {"ok": False, "state": "not_found"}
        return query_host_command(RuntimeRepository(path, read_only=True), self._identity(thread.thread_id, request_id))

    # LLM: canonical thread 只从原会话 Store 取得；身份由宿主绑定，输入正文不能提供 thread/owner。
    # 函数用途: 查原线程，首次获授权安装才允许经原入口创建线程。
    def _thread(self, *, create: bool):
        context = self.context
        fields = {"channel": context.channel, "channel_conversation_id": context.conversation_id,
                  "channel_user_id": context.actor_id}
        thread, error = context.threads.resolve_report(**fields)
        if error:
            raise RuntimeConflictError("原会话记录不可读")
        if thread is None and create:
            thread = context.threads.get_or_create({
                **fields, "canonical_user_id": context.actor_id, "owner_id": context.owner.owner_id,
                "owner_home": str(context.owner.home_dir), "title": "插件管理",
            })
        if thread is not None and thread.owner_id != context.owner.owner_id:
            raise RuntimeConflictError("原会话不属于当前 owner")
        return thread

    # LLM: 摘要覆盖明确参数、目录版本、工作根和当前工具权限；配置正文留在 handler 内，不能进入参数。
    # 函数用途: 固定原请求全部工具参数，重送无需重新读取配置或包来源。
    def _arguments(self, tool_name: str, values: dict, revision: str) -> dict:
        policy = self.context.path_policy
        permission = {"mode": policy.mode, "owner_root": str(policy.owner_scope_root or ""),
                      "dangerous_roots": [str(path) for path in policy.dangerous_roots],
                      "tool_allowed": self._allowed(tool_name)}
        return {**values, "catalog_revision": revision, "workspace": str(self.context.workspace),
                "permission_digest": tool_arguments_hash(permission)}

    # LLM: 原身份字段仅取宿主上下文；查询编号不会覆盖 owner/channel/actor。
    # 函数用途: 为提交与查询生成相同可信去重范围。
    def _identity(self, thread_id: str, request_id: str) -> HostCommandIdentity:
        context = self.context
        return HostCommandIdentity(context.owner.owner_id, context.actor_id, context.channel, thread_id, request_id)

    # LLM: 输入摘要与原 ToolCall 参数摘要相同；完整来源字节的身份由安装领域回执另外保存。
    # 函数用途: 为首次执行或完全相同的重送绑定命令输入。
    def _request(self, thread_id: str, request_id: str, tool_name: str, arguments: dict) -> HostCommandRequest:
        return HostCommandRequest(**asdict(self._identity(thread_id, request_id)),
                                  command_name=tool_name,
                                  input_digest=tool_arguments_hash(arguments).removeprefix("sha256:"))

    # LLM: 管理 handler 共用原执行器；配置/启停冻结同次目录和安装读，启用计划在 claim 前声明，不豁免工具禁用。
    # 函数用途: 将已登记管理请求接到唯一工具执行器及原操作 Store，不创建普通代理。
    def _prepare(self, repo: RuntimeRepository, binding, arguments: dict) -> ToolExecutorRequest:
        context = self.context
        tool_name = binding.request.command_name
        if tool_name == PLUGIN_INSTALL_TOOL:
            tool = PluginInstallTool(self.installations, context.path_policy, context.workspace, binding.request.operation_id)
        elif tool_name in {PLUGIN_CONFIGURE_TOOL, PLUGIN_DISABLE_TOOL, PLUGIN_ENABLE_TOOL}:
            entries = self.installations.snapshot()
            existing = next((row for row in entries if row.manifest.plugin_id == arguments["plugin"]), None)
            catalog_revision = self._catalog(entries).revision
            if tool_name == PLUGIN_CONFIGURE_TOOL:
                tool = PluginConfigureTool(self.installations, context.path_policy, context.workspace,
                                           binding.request.operation_id, existing, catalog_revision)
            elif tool_name == PLUGIN_ENABLE_TOOL:
                tool = PluginEnableTool(context.owner, repo, binding, existing, catalog_revision)
            else:
                tool = PluginDisableTool(context.owner, repo, binding.request.operation_id, existing, catalog_revision)
        else:
            raise RuntimeConflictError("未知插件管理工具")
        allowed = self._allowed(tool_name)
        runtime = ToolRuntime(tool.model_spec, tool.runtime_policy, tool, exposure=ToolExposure(model_visible=False),
                              availability=ToolAvailability(allowed, "TOOL_DISABLED" if not allowed else ""))
        names = frozenset({tool_name})
        snapshot = ToolRuntimeSnapshot(binding.run_id, (runtime,), names, (), names if allowed else frozenset())
        call = ToolCall(binding.request.request_id, tool_name, arguments, "native",
                        tool.model_spec.schema_hash, binding.run_id, binding.request.request_id,
                        binding.attempt_id, operation_id=binding.request.operation_id)
        policy = context.path_policy
        return ToolExecutorRequest(
            call, snapshot, context.workspace, path_access_mode=policy.mode,
            path_dangerous_roots=tuple(str(path) for path in policy.dangerous_roots),
            owner_scope_root=str(policy.owner_scope_root or ""), operation_owner_id=context.owner.owner_id,
            operation_store=ManagedOperationStore(repo), operation_store_required=True,
            trusted_run_context={"run_scope": {"task_id": binding.task_id}},
        )

    # LLM: 工具权限取宿主既有 owner 策略，不由动作可用性或包自述推断；未知内部工具关闭。
    # 函数用途: 让每种管理动作分别遵守当前禁用策略。
    def _allowed(self, tool_name: str) -> bool:
        return {PLUGIN_INSTALL_TOOL: self.context.tool_allowed,
                PLUGIN_CONFIGURE_TOOL: self.context.configure_allowed,
                PLUGIN_DISABLE_TOOL: self.context.disable_allowed,
                PLUGIN_ENABLE_TOOL: self.context.enable_allowed}.get(tool_name, False)

    # LLM: 目录刷新失败不能抹掉原操作结果；公开回执只取管理结果投影，不输出配置值或整个工具账。
    # 函数用途: 为 TUI/HTTP 返回原管理动作结果和查询编号，执行权关闭与资源未知分别展示。
    def _reply(self, payload: dict, request_id: str = "") -> dict:
        payload = dict(payload)
        envelope = payload.pop("result", {})
        if envelope:
            payload["details"] = next((envelope[key] for key in _MANAGEMENT_TOOLS.values() if key in envelope), {})
        state = payload.get("state", "")
        messages = {"succeeded": "插件管理请求已完成。", "failed": "插件管理请求失败，请核对原因。",
                    "cancelled": "插件管理请求已取消，请查看原请求结果。", "rejected": "插件管理请求被拒绝，没有开始执行。",
                    "approval_required": "插件管理需要审批，尚未执行。", "pending": "请求已登记，尚未开始执行。",
                    "running": "请求正在执行。", "outcome_unknown": "插件管理结果尚未确认，请查询原请求。",
                    "not_found": "当前会话没有查到该请求；这不证明其他会话或未确认请求没有执行。"}
        result = {"kind": "plugin_command", "request_id": request_id, **payload}
        explanations = {
            "PLUGIN_PERMISSION_DENIED": "当前身份没有插件管理权限。",
            "PLUGIN_DISABLED": "插件管理已关闭，原请求结果仍可查询。",
            "PLUGIN_CATALOG_STALE": "目录已经变化或尚未读取，请重新查看帮助后确认输入。",
            "INVALID_COMMAND_ARGUMENTS": "插件命令参数或请求编号无效。",
            "TOOL_DISABLED": "当前用户策略禁止此插件管理操作。",
            "TOOL_INVALID_ARGUMENTS": "插件、来源文件或配置无效，或读取未获授权。",
        }
        message = messages.get(state, "插件命令已处理。")
        if state in {"succeeded", "failed", "cancelled"} and payload.get("finalization_pending"):
            message = "插件管理操作已有结果，运行收尾尚未确认。"
        result.setdefault("message", explanations.get(payload.get("error_code"), message))
        if request_id:
            result["message"] += f"\n查询：/plugins status {request_id}"
        try:
            result["catalog"] = self.catalog().to_payload()
        except (OSError, ValueError):
            result["catalog_error"] = True
        return result


# LLM: 公共请求编号限制为可直接输入查询命令的结构化 token，不解析自然语言决定身份或操作。
# 函数用途: 拒绝空值、空白或超长请求编号，避免一次写入无法准确查询。
def _validate_request_id(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
        raise ValueError("插件请求编号无效")
