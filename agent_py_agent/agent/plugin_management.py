# LLM: 命令分路只消费宿主身份、权限与原线程 Store；业务和收尾状态分别投影，联测原宿主运行及重复查询。
# 模块用途: 组合插件目录、装卸、调用和原请求查询，不借用聊天回合，也不把业务结果当作资源已释放。

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .command_catalog import COMMAND_INDEX
from .common.cancellation import CancellationToken
from .path_access_policy import PathAccessPolicy
from .plugin_cleanup import consume_plugin_cleanup
from .plugin_command_service import execute_plugin_command, read_plugin_catalog
from .plugin_commands import (
    parse_plugin_command,
    plugin_command_response,
    plugin_namespace,
    render_plugin_use_card,
)
from .plugin_configure_tool import PLUGIN_CONFIGURE_TOOL, PluginConfigureTool
from .plugin_disable_tool import PLUGIN_DISABLE_TOOL, PluginDisableTool
from .plugin_enable_tool import PLUGIN_ENABLE_TOOL, PluginEnableTool
from .plugin_install_store import PluginInstallStore
from .plugin_install_tool import PLUGIN_INSTALL_TOOL, PluginInstallTool
from .plugin_invocation import (
    PluginInvocation,
    execute_plugin_invocation,
    plugin_invocation_context,
)
from .plugin_removal import PLUGIN_REMOVE_TOOL
from .plugin_remove_tool import PluginRemoveTool
from .plugin_runtime import plugin_tool_name
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
                     "enable": PLUGIN_ENABLE_TOOL, "disable": PLUGIN_DISABLE_TOOL, "remove": PLUGIN_REMOVE_TOOL}


# LLM: 所有字段来自宿主，布尔授权不是客户端参数；ThreadStore 是原会话权威，目录投影不能提供执行身份。
# 类用途: 给插件命令传必要身份、权限和存储依赖，业务调用不借用管理员资格或聊天审批缓存。
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
    remove_allowed: bool = True
    business_allowed: bool = True
    disabled_tools: frozenset[str] = frozenset()
    approval_mode: str = "ask"


# LLM: 冷入口与完整代理共用 owner 权限和审批配置；管理/业务分别受工具禁用约束，查询仍绑定原身份。
# 函数用途: 从当前私有配置和原线程路径组装命令依赖，不创建 owner、线程或完整 Agent。
def plugin_management_context(
    owner: OwnerHomeResult, home: object, config: object, threads: object, *,
    actor_id: str, channel: str, conversation_id: str, is_admin: bool,
) -> PluginManagementContext:
    from .user_space.approval_mode import permission_config, read_approval_mode
    from .user_space.owner_access import resolve_owner_scope_and_access
    from .user_space.owner_policy import resolve_effective_owner_policy

    policy = resolve_effective_owner_policy(home)
    allowed = not policy.load_errors and bool(config.enable_tools)
    approval_mode = "ask"
    try:
        effective = permission_config(config, home)
        if hasattr(home, "owner_tool_policy_json"):
            approval_mode = read_approval_mode(home)
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
                                   allowed and PLUGIN_ENABLE_TOOL not in policy.disabled_tools,
                                   allowed and PLUGIN_REMOVE_TOOL not in policy.disabled_tools,
                                   business_allowed=allowed, disabled_tools=frozenset(policy.disabled_tools),
                                   approval_mode=approval_mode)


# LLM: owner 只有原安装 Store/runtime.db；实例不建后台任务或业务历史，结果投影须保留 finalization_pending。
# 类用途: 承接管理和业务命令，安装默认停用、调用遵守审批、撤销精确资源，查询区分业务结束与运行收尾。
class PluginManagement:
    # LLM: 构造不创建目录或线程；管理写入检查管理员，业务另检查工具策略，均须遵守插件开关。
    # 函数用途: 保存这次已鉴权入口的必要依赖。
    def __init__(self, context: PluginManagementContext) -> None:
        self.context = context
        self.installations = PluginInstallStore(context.owner)

    # LLM: 声明只投影唯一安装表，不扫描包目录；安装版本参与摘要，值与值摘要不进入公开投影。
    # 函数用途: 为帮助与补全读取当前 owner 的公开声明。
    def catalog(self):
        return self._catalog(self.installations.snapshot())

    # LLM: 同次快照派生原提交引用以区分重装，避免目录/目标读取竞态；可用性和引用均不授予权限。
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
                                             installation_ref=row.installation_ref,
                                             enabled=row.enabled, activation_id=row.activation_id)
                                     for row in entries))

    # LLM: 已接受请求先读原账，不先读来源或新插件；详情只从同次安装快照和声明生成使用卡，审批仍由宿主绑定。
    # 函数用途: 分派明确的管理或业务命令，不进入聊天；详情说明不启动插件，业务批准只恢复同一原调用。
    def command(self, text: str, *, revision: str, request_id: str,
                request_permission: Callable | None = None,
                cancellation_token: CancellationToken | None = None) -> dict:
        namespace = plugin_namespace(text)
        if namespace is None:
            raise ValueError("不是插件命令")
        if namespace.plugin_id:
            return self._reply(self._business(text, revision, request_id, request_permission, cancellation_token), request_id)
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
            return self._reply(self._submit(tool_name, dict(values), revision, request_id,
                                           request_permission, cancellation_token), request_id)
        entries = self.installations.snapshot()
        catalog = self._catalog(entries)
        if not revision or revision != catalog.revision:
            return execute_plugin_command(catalog, text, revision=revision)
        if parsed.action.name == "list":
            entries = [item for item in catalog.plugins if not values.get("enabled") or item.enabled]
            return self._reply({"ok": True, "message": "\n".join(
                f"{item.plugin_id} {item.package_version}（{'启用' if item.enabled else '停用'}）  {item.summary}"
                for item in entries) or "当前没有符合条件的已安装插件。"})
        if parsed.action.name == "info":
            plugin = next((item for item in catalog.plugins if item.plugin_id == values["plugin"]), None)
            entry = next((item for item in entries if item.manifest.plugin_id == values["plugin"]), None)
            return self._reply({"ok": plugin is not None and entry is not None, "message": (
                render_plugin_use_card(plugin, entry.manifest.settings_schema)
                if plugin is not None and entry is not None else "未找到该插件。")})
        return execute_plugin_command(catalog, text, revision=revision)

    # LLM: 先按原身份查冻结请求，再解释当前目录；旧终态不启动新连接，原 pending 只能在原选择仍成立时执行。
    # 函数用途: 让普通授权用户明确调用插件工具，参数与宿主上下文分别登记，管理权限不授予业务执行权。
    def _business(self, text: str, revision: str, request_id: str,
                  request_permission: Callable | None, cancellation_token: CancellationToken | None) -> dict:
        context_digest = plugin_invocation_context(self.context, text, revision)
        thread = self._thread(create=False)
        path = runtime_db_path(self.context.owner.home_dir)
        binding = repo = None
        if request_id and thread is not None and path.is_file():
            _validate_request_id(request_id)
            repo = RuntimeRepository(path)
            binding = repo.find_host_command(self._identity(thread.thread_id, request_id))
        selection = None
        if binding is not None:
            request = replace(binding.request, context_digest=context_digest)
            repo.find_host_command(request)
            if not self.context.enabled:
                return query_host_command(repo, request)
        else:
            selection = self._business_selection(text, revision)
            if isinstance(selection, dict):
                return selection
            _validate_request_id(request_id)
            thread = self._thread(create=True)
            repo = RuntimeRepository(path)
            request = replace(self._request(thread.thread_id, request_id, selection.tool_name, selection.arguments),
                              context_digest=context_digest)

        # LLM: 新请求持原不可变安装，恢复只核对原目录；拒绝结果不伪造选择或把新安装塞入旧请求。
        # 函数用途: 在唯一 HostCommand 执行区间内交付本次确定的插件和参数。
        def select():
            selected = selection if selection is not None else self._business_selection(text, revision)
            if isinstance(selected, dict):
                raise RuntimeConflictError("原插件命令选择已不可执行")
            return selected

        return execute_plugin_invocation(self.context, repo, request, select,
                                         request_permission=request_permission, cancellation_token=cancellation_token)

    # LLM: 同一快照绑定安装、目录和公共参数；仅 kind=tool 可调用本包目标，包自述不降低宿主权限。
    # 函数用途: 在建立运行和连接前处理静态帮助、目录过期、停用与策略拒绝，并取实际工具参数。
    def _business_selection(self, text: str, revision: str) -> PluginInvocation | dict:
        entries = self.installations.snapshot()
        catalog = self._catalog(entries)
        static = execute_plugin_command(catalog, text, revision=revision)
        if static.get("reason") != "not_implemented":
            return static
        if not self.context.enabled:
            return {"ok": False, "state": "rejected", "error_code": "PLUGIN_DISABLED"}
        parsed = parse_plugin_command(text, plugins=catalog.plugins, management_actions=catalog.management_actions)
        if not parsed.plugin.enabled:
            return {"ok": False, "state": "rejected", "error_code": "PLUGIN_NOT_ENABLED"}
        if parsed.action.kind != "tool" or not parsed.action.available:
            return static
        name = plugin_tool_name(parsed.plugin.plugin_id, parsed.action.target)
        if not self.context.business_allowed or name in self.context.disabled_tools:
            return {"ok": False, "state": "rejected", "error_code": "TOOL_DISABLED"}
        entry = next(row for row in entries if row.manifest.plugin_id == parsed.plugin.plugin_id)
        arguments = json.loads(json.dumps(dict(parsed.arguments.values), ensure_ascii=False, allow_nan=False))
        return PluginInvocation(entry, name, arguments)

    # LLM: 认证和开关先于新写入，旧请求先读账；交互和取消仅属于当前提交，不能把旧动作重放到新激活。
    # 函数用途: 按已见目录版本提交管理请求，将当前连接取消信号传给原执行器。
    def _submit(self, tool_name: str, values: dict, revision: str, request_id: str,
                request_permission: Callable | None, cancellation_token: CancellationToken | None) -> dict:
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
                return self._execute(repo, request, arguments, request_permission, cancellation_token)
        if not self.context.enabled:
            return {"ok": False, "state": "rejected", "error_code": "PLUGIN_DISABLED"}
        if not revision or revision != self.catalog().revision:
            return {"ok": False, "state": "rejected", "error_code": "PLUGIN_CATALOG_STALE"}
        thread = self._thread(create=True)
        repo = RuntimeRepository(runtime_db_path(self.context.owner.home_dir))
        request = self._request(thread.thread_id, request_id, tool_name, arguments)
        return self._execute(repo, request, arguments, request_permission, cancellation_token)

    # LLM: 原 HostCommand 只使用当前消费者与令牌；结果严格读回后才消费退出证据，清理失败不翻转成功或重跑。
    # 函数用途: 在原执行区间处理明确管理动作，并分别返回执行结果与退出证据消费情况。
    def _execute(self, repo: RuntimeRepository, request: HostCommandRequest, arguments: dict,
                 request_permission: Callable | None, cancellation_token: CancellationToken | None) -> dict:
        result = execute_host_command(repo, request, lambda binding: replace(
            self._prepare(repo, binding, arguments), cancellation_token=cancellation_token),
            request_permission=request_permission)
        cleanup = consume_plugin_cleanup(self.context.owner, repo, request)
        return {**result, "cleanup_consumption": cleanup} if cleanup else result

    # LLM: 查询绑定原操作者，管理结果另要求当前管理员；业务用户只能读取自己原身份的请求，不创建或补齐执行。
    # 函数用途: 按稳定请求编号读取本会话的插件管理或调用结果。
    def _query(self, request_id: str) -> dict:
        _validate_request_id(request_id)
        thread = self._thread(create=False)
        path = runtime_db_path(self.context.owner.home_dir)
        if thread is None or not path.is_file():
            if not self.context.is_admin:
                return {"ok": False, "state": "rejected", "error_code": "PLUGIN_PERMISSION_DENIED"}
            return {"ok": False, "state": "not_found"}
        repo = RuntimeRepository(path, read_only=True)
        identity = self._identity(thread.thread_id, request_id)
        binding = repo.find_host_command(identity)
        if not self.context.is_admin and (binding is None or binding.request.command_name in _MANAGEMENT_TOOLS.values()):
            return {"ok": False, "state": "rejected", "error_code": "PLUGIN_PERMISSION_DENIED"}
        return query_host_command(repo, identity)

    # LLM: canonical thread 只从原会话 Store 取得；身份由宿主绑定，输入正文不能提供 thread/owner。
    # 函数用途: 查原线程，首次获授权命令才允许经原入口创建线程。
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
                "owner_home": str(context.owner.home_dir), "title": "插件命令",
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

    # LLM: 原目录与安装同读，构造阶段不执行管理动作；启用工具在这里固定原计划，卸载工具只取得旧记录。
    # 函数用途: 为已登记的明确动作选择唯一管理工具，并冻结它实际需要的依赖。
    def _management_tool(self, repo: RuntimeRepository, binding, arguments: dict):
        context = self.context
        tool_name = binding.request.command_name
        if tool_name == PLUGIN_INSTALL_TOOL:
            return PluginInstallTool(self.installations, context.path_policy, context.workspace, binding.request.operation_id)
        if tool_name not in {PLUGIN_CONFIGURE_TOOL, PLUGIN_DISABLE_TOOL, PLUGIN_ENABLE_TOOL, PLUGIN_REMOVE_TOOL}:
            raise RuntimeConflictError("未知插件管理工具")
        entries = self.installations.snapshot()
        existing = next((row for row in entries if row.manifest.plugin_id == arguments["plugin"]), None)
        catalog_revision = self._catalog(entries).revision
        if tool_name == PLUGIN_CONFIGURE_TOOL:
            return PluginConfigureTool(self.installations, context.path_policy, context.workspace,
                                       binding.request.operation_id, existing, catalog_revision)
        if tool_name == PLUGIN_ENABLE_TOOL:
            return PluginEnableTool(context.owner, repo, binding, existing, catalog_revision)
        if tool_name == PLUGIN_REMOVE_TOOL:
            return PluginRemoveTool(context.owner, repo, binding.request.operation_id, existing, catalog_revision)
        return PluginDisableTool(context.owner, repo, binding.request.operation_id, existing, catalog_revision)

    # LLM: 管理工具共用原快照/权限/操作账；构造计划在 claim 前声明，装卸不豁免工具禁用或运行身份。
    # 函数用途: 将冻结的管理工具接到唯一 ToolExecutor，不创建普通代理或另一执行链。
    def _prepare(self, repo: RuntimeRepository, binding, arguments: dict) -> ToolExecutorRequest:
        context = self.context
        tool_name = binding.request.command_name
        tool = self._management_tool(repo, binding, arguments)
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

    # LLM: 装卸与启停权限各取宿主既有 owner 策略，不由动作可用性或包自述推断；未知内部工具关闭。
    # 函数用途: 让每种管理动作分别遵守当前禁用策略。
    def _allowed(self, tool_name: str) -> bool:
        return {PLUGIN_INSTALL_TOOL: self.context.tool_allowed,
                PLUGIN_CONFIGURE_TOOL: self.context.configure_allowed,
                PLUGIN_DISABLE_TOOL: self.context.disable_allowed,
                PLUGIN_ENABLE_TOOL: self.context.enable_allowed,
                PLUGIN_REMOVE_TOOL: self.context.remove_allowed}.get(tool_name, False)

    # LLM: 展示只读原结果；成功启用卡片只投影当前安装声明，finalization_pending 优先于可用文案。
    # 函数用途: 分开展示业务结果与运行收尾，启用确实完成时告知使用方法并保留原查询编号。
    def _reply(self, payload: dict, request_id: str = "") -> dict:
        payload = dict(payload)
        envelope = payload.pop("result", {})
        if envelope:
            payload["details"] = next((envelope[key] for key in _MANAGEMENT_TOOLS.values() if key in envelope), {})
        business = bool(payload.get("tool_name")) and payload["tool_name"] not in _MANAGEMENT_TOOLS.values()
        if business:
            payload["details"] = envelope
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
            "PLUGIN_NOT_ENABLED": "插件尚未启用，请先启用后再调用。",
            "PLUGIN_CATALOG_STALE": "目录已经变化或尚未读取，请重新查看帮助后确认输入。",
            "INVALID_COMMAND_ARGUMENTS": "插件命令参数或请求编号无效。",
            "TOOL_DISABLED": "当前用户策略禁止此插件管理操作。",
            "TOOL_INVALID_ARGUMENTS": "插件、来源文件或配置无效，或读取未获授权。",
        }
        message = messages.get(state, "插件命令已处理。")
        details = payload.get("details", {})
        if state == "succeeded" and details.get("removed") is True:
            message = "插件已卸载，用户产物与操作历史保留。"
        elif state == "succeeded" and details.get("release_pending"):
            message = "插件已停用，原准备执行器尚未确认退出；请稍后再次停用以完成环境释放。"
        elif state == "succeeded" and details.get("released") is True:
            message = "插件已停用并释放，可以再次启用。"
        if payload.get("cleanup_consumption", {}).get("state") == "pending":
            message += "资源或包回收尚未确认；重送原请求可继续收尾。"
        if business:
            message = {"succeeded": "插件调用已完成。", "failed": "插件调用失败，请核对原结果。",
                       "approval_required": "插件调用需要审批，尚未执行。",
                       "outcome_unknown": "插件调用结果尚未确认，请查询原请求。"}.get(state, message)
            if payload.get("output"):
                message += "\n" + payload["output"]
        if state in {"succeeded", "failed", "cancelled"} and payload.get("finalization_pending"):
            message = "插件调用已有结果，连接收尾尚未确认。" if business else "插件管理操作已有结果，运行收尾尚未确认。"
        if payload.get("connection_cleanup", {}).get("confirmed") is False:
            message += "\n本次插件连接退出尚未确认；原调用结果保持，请核对资源。"
        result.setdefault("message", explanations.get(payload.get("error_code"), message))
        try:
            entries = self.installations.snapshot()
            catalog = self._catalog(entries)
            result["catalog"] = catalog.to_payload()
            if (state == "succeeded" and payload.get("tool_name") == PLUGIN_ENABLE_TOOL
                    and details.get("enabled") is True and not payload.get("finalization_pending")):
                plugin_id = details.get("plugin_id")
                plugin = next((item for item in catalog.plugins if item.plugin_id == plugin_id and item.enabled
                               and item.activation_id == details.get("activation_id")), None)
                entry = next((item for item in entries if item.manifest.plugin_id == plugin_id), None)
                if plugin is not None and entry is not None:
                    result["message"] += "\n" + render_plugin_use_card(plugin, entry.manifest.settings_schema)
        except (OSError, ValueError):
            result["catalog_error"] = True
        if request_id:
            result["message"] += f"\n查询：/plugins status {request_id}"
        return result


# LLM: 公共请求编号限制为可直接输入查询命令的结构化 token，不解析自然语言决定身份或操作。
# 函数用途: 拒绝空值、空白或超长请求编号，避免一次写入无法准确查询。
def _validate_request_id(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
        raise ValueError("插件请求编号无效")
