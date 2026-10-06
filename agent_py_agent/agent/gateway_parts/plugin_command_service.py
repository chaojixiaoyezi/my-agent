# LLM: 入口复用可信来源检查与 GatewayControlScope；命令文本、客户端 scope_ref/revision 不参与 owner 解析。
# 模块用途: 向 TUI 与 IM 提供同一插件管理和业务入口，冷用户保持未加载，未知结果可按原请求查询。

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import is_dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from ..conversation.control_commands import ConversationControlCommand, ConversationControlResult
from ..plugin_command_service import (
    plugin_catalog_unavailable,
    plugin_command_unknown,
)
from ..plugin_management import PluginManagement, plugin_management_context
from ..user_space.owner_access import is_complete_local_admin_owner
from ..user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from .control_service import resolve_gateway_scope_owner, resolve_loaded_gateway_scope_agent
from .owner_conversation_store import owner_conversation_store


# LLM: 调用方已完成来源鉴权；管理仍独立检查管理员，只在首次授权安装时创建原线程与宿主运行，不启动模型。
# 函数用途: 为新目录接口以及 /ask、/control 的插件输入生成同一宿主回执。
def plugin_http_response(handler, server, body: dict, *, text: str | None = None) -> dict:
    request_id = body.get("plugin_request_id", "")
    if not isinstance(request_id, str):
        request_id = ""
    if text is not None:
        from ..command_arguments import CommandArgumentError
        from ..plugin_commands import parse_plugin_command

        try:
            parsed = parse_plugin_command(text)
            if parsed is not None and parsed.action and parsed.action.name == "status" and not parsed.help_requested:
                request_id = parsed.arguments.values["request"]
        except CommandArgumentError:
            pass

    try:
        if server is None or server.agent is None:
            return plugin_catalog_unavailable()
        manager = _management(handler, server, body)
        if text is None:
            return {"ok": True, "catalog": manager.catalog().to_payload()}
        revision = body.get("catalog_revision", "")
        if not isinstance(revision, str):
            raise ValueError("目录版本必须是字符串")
        return _log_rejection(manager.command(text, revision=revision, request_id=request_id), text, request_id)
    except Exception:  # noqa: BLE001 回包失败不能推断已接受命令未执行，也不能泄露私有路径
        return plugin_catalog_unavailable() if text is None else plugin_command_unknown(request_id)


# LLM: 非成功回执的错误码同时保留在结构字段和纯文本；原 rejected 回执已由管理层带码，不重复追加。
#   确认预览回执（details.reason == "confirmation_required"）不是失败，同样不追加错误码文本；
#   只在结构化 state=rejected 时记一行，只含错误码、子命令名和请求编号，不记命令原文、路径或 owner 目录；
#   用 WARNING 是因为 Gateway 进程不配置日志级别，只有 WARNING 及以上会进 gateway.log。管理员正常执行不产生这行。
# 函数用途: 为 TUI 和 IM 保留可见错误码，并把执行前拒绝写入无私有正文的 Gateway 诊断。
def _log_rejection(result: dict, text: str, request_id: str) -> dict:
    code = result.get("error_code") if isinstance(result, dict) else None
    details = result.get("details") if isinstance(result, dict) else None
    confirmation = (isinstance(details, dict) and details.get("reason") == "confirmation_required")
    if code and not result.get("ok") and result.get("state") != "rejected" and not confirmation:
        result = {**result, "message": f"{result.get('message', '')}\n错误码：{code}"}
    if isinstance(code, str) and code and result.get("state") == "rejected":
        logging.getLogger(__name__).warning("PLUGIN_COMMAND_REJECTED error_code=%s action=%s request_id=%s",
                                            code, _action_name(text), request_id or "-")
    return result


# LLM: 子命令名只取解析器给出的结构化动作名，业务调用统一记 plugin_call；解析失败记 "-"，不回显原文。
# 函数用途: 为拒绝日志给出不含参数的子命令名。
def _action_name(text: str) -> str:
    from ..command_arguments import CommandArgumentError
    from ..plugin_commands import parse_plugin_command, plugin_namespace

    namespace = plugin_namespace(text)
    if namespace is not None and namespace.plugin_id:
        return "plugin_call"
    try:
        parsed = parse_plugin_command(text)
    except CommandArgumentError:
        return "-"
    return parsed.action.name if parsed is not None and parsed.action else "-"


# LLM: 使用原认证中间件裁决角色；无认证模式仅在显式关闭 auth 且监听及对端均为本机时授权，不信任正文 user/channel。
# 函数用途: 固定管理请求的操作者和权限，并按原 owner 与线程路径组装冷服务。
def _management(handler, server, body: dict) -> PluginManagement:
    from ..auth.middleware import _handler_peer_ip, _is_loopback_peer
    from .http_handlers import _gateway_control_scope, _request_channel
    from .http_service import _is_loopback_host

    base = server.agent
    middleware = getattr(handler, "_auth_middleware", None)
    peer = _handler_peer_ip(handler)
    if middleware is not None:
        headers = dict(handler.headers)
        if peer is None or not middleware.check_trusted(headers, peer):
            raise PermissionError("插件入口来源未获授权")
        actor, channel = middleware.extract_identity(headers, peer)
        is_admin = middleware.require_admin(headers, peer)[0]
    else:
        actor, channel = _request_channel(handler)
        is_admin = (base.config.auth_enabled is False
                    and getattr(server, "auth_middleware", None) is None
                    and _is_loopback_host(str(getattr(server, "bind_host", "")))
                    and peer is not None and _is_loopback_peer(peer))
    scope = _gateway_control_scope(handler, body, user_id=actor, channel=channel)
    scope = replace(scope, user_id=actor, channel=channel)
    # 事件中心与 TUI 的 HTTP 目录入口共用 server 上的同一个 hub（没有就按“暂无记录”展示，不新建）；
    # 放进 scope 而不是额外形参：_scope_management 是会被测试打桩的内部函数，多一个必传关键字会让老替身直接 TypeError。
    scope = replace(scope, event_hub=getattr(server, "plugin_event_hub", None))
    return _scope_management(base, scope, is_admin)


# LLM: TUI 的 HTTP 角色或 IM 的已解析完整 owner 是唯一授权源；正文不能选择管理员，冷读取不构造 Agent。
#   IM 路径的管理员判定用 owner_access.is_complete_local_admin_owner(home)，与 /settings 同一条规则、同一种 home。
#   /plugins list 的 MCP 段只借用已经加载的 owner 实例（resolve_loaded_gateway_scope_agent 的被动查找），冷 owner 保持冷、
#   段里如实写“未加载”。
#   event_hub 从 scope.event_hub 取（调用方即 Gateway 入口把它放进 scope）：只读展示依赖，缺省 None 时展示“暂无记录”。
# 函数用途: 为两个入口组装同一 PluginManagement，显式工作目录仍经原 Gateway 路径权限门。
def _scope_management(base, scope, is_admin: bool | None = None) -> PluginManagement:
    from .workspace_scope import gateway_request_workspace_scope

    owner = resolve_owner_home(base.home_paths.root, resolve_gateway_scope_owner(base, scope))
    home = home_paths_with_owner(base.home_paths, owner)
    if is_admin is None:
        is_admin = is_complete_local_admin_owner(home)
    store = owner_conversation_store(base, home, initialize=False)
    context = plugin_management_context(
        owner, home, base.config, store.threads, actor_id=scope.user_id, channel=scope.channel,
        conversation_id=scope.conversation_id, is_admin=is_admin,
    )
    loaded = resolve_loaded_gateway_scope_agent(base, scope)
    context = replace(context, live_registry=getattr(loaded, "tools", None),
                      event_hub=getattr(scope, "event_hub", None))
    if scope.workspace is not None:
        narrow_host = SimpleNamespace(
            config=SimpleNamespace(my_agent_owner_provider=owner.identity.provider),
            tools=SimpleNamespace(owner_scope_root=str(context.path_policy.owner_scope_root or "")),
        )
        cwd, _roots = gateway_request_workspace_scope(narrow_host, {"workspace": scope.workspace})
        context = replace(context, workspace=Path(cwd))
    return PluginManagement(context)


# LLM: IM 没有 Tab 目录握手，因此宿主冻结本次目录版本；解析、权限、原 HostCommand 和 --confirm 均沿同一管理服务。
#   同消息重送由调用方的持久控制回执去重；异常保留原插件请求编号，不能重跑或宣称没有执行。
#   event_hub 由调用方沿控制链透传：事件中心挂在 Gateway server 上（唯一来源），IM 没有 handler，
#   由 http_handlers 从 server 取到后放进 scope.event_hub 带下来；缺省 None 时展示“暂无记录”，绝不为了展示新建 hub。
# 函数用途: 把会话插件命令交给 TUI 的同一服务，投影为纯文本回执，不调用模型或新建审批通道。
def execute_plugin_control(base, command: ConversationControlCommand, scope, *, event_hub=None) -> ConversationControlResult:
    request_id = uuid.uuid4().hex
    try:
        # hub 优先取显式入参（后台/无 scope 的调用），否则取 scope 上带下来的；两者都为空就按“暂无记录”展示。
        # 这里只补 scope 上的展示依赖，不改身份字段；scope 不是本模块的 dataclass 时也不许因此报错。
        hub = event_hub if event_hub is not None else getattr(scope, "event_hub", None)
        # 只有 dataclass 实例才能 replace；否则原样交给管理服务（3a 10-05：此前 replace(None) 抛 TypeError，
        #   被下面的兜底吞成 OUTCOME_UNKNOWN，管理员看到“未能确认插件请求结果”而不是真实回执）。
        scoped = replace(scope, event_hub=hub) if is_dataclass(scope) and not isinstance(scope, type) else scope
        manager = _scope_management(base, scoped)
        result = manager.command(command.value, revision=manager.catalog().revision, request_id=request_id)
    except Exception:  # noqa: BLE001 副作用可能已发生；不泄露内部路径，不自动重试。
        result = plugin_command_unknown(request_id)
    result = _log_rejection(result, command.value, request_id)
    return ConversationControlResult("plugins", bool(result.get("ok")), str(result.get("message") or ""),
                                     request_id=str(result.get("request_id") or request_id),
                                     error_code=str(result.get("error_code") or ""))


# LLM: 来源检查先于正文读取；交互只接受布尔声明，路径和 owner 由原服务器绑定，命令仍在原 HTTP 线程执行一次。
# 函数用途: 读取目录或提交命令，按显式客户端能力选择原 JSON 回执或持续审批流。
def handle_client_plugins(handler, server) -> None:
    from ..plugin_commands import plugin_namespace
    from .http_handlers import require_trusted_source

    if require_trusted_source(handler):
        return
    try:
        body = handler._read_json()
        if not isinstance(body, dict) or body.get("operation") not in {"catalog", "command"}:
            raise ValueError("请求操作无效")
        if "interactive" in body and type(body["interactive"]) is not bool:
            raise ValueError("交互能力必须是布尔值")
        text = body.get("command") if body["operation"] == "command" else None
        if body["operation"] == "command" and (
            not isinstance(text, str) or plugin_namespace(text) is None
        ):
            raise ValueError("请求必须是插件命令")
    except (json.JSONDecodeError, TypeError, ValueError):
        handler._send_json(
            400,
            {
                "ok": False,
                "error_code": "INVALID_COMMAND_ARGUMENTS",
                "message": "插件目录请求格式错误。",
            },
        )
        return
    if text is not None and body.get("interactive") is True:
        _interactive_command(handler, server, body, text)
        return
    result = plugin_http_response(handler, server, body, text=text)
    handler._send_json(
        503 if result.get("error_code") == "PLUGIN_CATALOG_UNAVAILABLE" else 200, result
    )


# LLM: 认证后的 manager 和 server.paths 是唯一身份与地址来源；错误不暴露私有配置，HTTP 不能指定审批路径。
# 函数用途: 为一次本机命令连接原审批运输，仍由原服务完成授权与执行。
def _interactive_command(handler, server, body: dict, text: str) -> None:
    from ..auth.middleware import _handler_peer_ip, _is_loopback_peer
    from ..common.opaque_id import validate_opaque_id
    from .command_stream import serve_command_stream

    request_id = body.get("plugin_request_id", "")
    try:
        peer = _handler_peer_ip(handler)
        if peer is None or not _is_loopback_peer(peer):
            raise ValueError("当前交互审批需要同机客户端")
        validate_opaque_id(request_id, kind="host_command_request")
        revision = body.get("catalog_revision", "")
        if not isinstance(revision, str):
            raise ValueError("目录版本必须是字符串")
        manager = _management(handler, server, body)
        paths = server.paths
    except Exception:  # noqa: BLE001 准备失败不能暴露路径、认证详情或创建旁路执行
        handler._send_json(400, {"ok": False, "error_code": "INVALID_COMMAND_ARGUMENTS",
                                 "message": "插件交互请求或连接无效。"})
        return

    # LLM: 原服务接收当前请求的唯一消费者与令牌；异常保持原请求可查，不重试或另建业务执行线程。
    # 函数用途: 在 HTTP 执行区间提交命令并保留无法确认的原结果。
    def execute(request_permission, cancellation_token) -> dict:
        try:
            return _log_rejection(manager.command(text, revision=revision, request_id=request_id,
                                                  request_permission=request_permission,
                                                  cancellation_token=cancellation_token), text, request_id)
        except Exception:  # noqa: BLE001 已执行与清理状态只由原账本裁决
            return plugin_command_unknown(request_id)

    serve_command_stream(handler, paths, manager.context.owner.identity, request_id, execute)
