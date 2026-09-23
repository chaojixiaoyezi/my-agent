# LLM: 入口复用可信来源检查与 GatewayControlScope；命令文本、客户端 scope_ref/revision 不参与 owner 解析。
# 模块用途: 向 TUI 提供 owner 安装目录和原管理执行入口，冷用户保持未加载，未知结果可按原请求查询。

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from ..plugin_command_service import (
    plugin_catalog_unavailable,
    plugin_command_unknown,
)
from ..plugin_management import PluginManagement, plugin_management_context
from ..user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from .control_service import resolve_gateway_scope_owner
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
        return manager.command(text, revision=revision, request_id=request_id)
    except Exception:  # noqa: BLE001 回包失败不能推断已接受命令未执行，也不能泄露私有路径
        return plugin_catalog_unavailable() if text is None else plugin_command_unknown(request_id)


# LLM: 使用原认证中间件裁决角色；无认证模式仅在显式关闭 auth 且监听及对端均为本机时授权，不信任正文 user/channel。
# 函数用途: 固定管理请求的操作者和权限，并按原 owner 与线程路径组装冷服务。
def _management(handler, server, body: dict) -> PluginManagement:
    from ..auth.middleware import _handler_peer_ip, _is_loopback_peer
    from .http_handlers import _gateway_control_scope, _request_channel
    from .http_service import _is_loopback_host
    from .workspace_scope import gateway_request_workspace_scope

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
    owner = resolve_owner_home(base.home_paths.root, resolve_gateway_scope_owner(base, scope))
    home = home_paths_with_owner(base.home_paths, owner)
    store = owner_conversation_store(base, home, initialize=False)
    context = plugin_management_context(
        owner, home, base.config, store.threads, actor_id=actor, channel=channel,
        conversation_id=scope.conversation_id, is_admin=is_admin,
    )
    if "workspace" in body:
        narrow_host = SimpleNamespace(
            config=SimpleNamespace(my_agent_owner_provider=owner.identity.provider),
            tools=SimpleNamespace(owner_scope_root=str(context.path_policy.owner_scope_root or "")),
        )
        cwd, _roots = gateway_request_workspace_scope(narrow_host, body)
        context = replace(context, workspace=Path(cwd))
    return PluginManagement(context)


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
            return manager.command(text, revision=revision, request_id=request_id,
                                   request_permission=request_permission, cancellation_token=cancellation_token)
        except Exception:  # noqa: BLE001 已执行与清理状态只由原账本裁决
            return plugin_command_unknown(request_id)

    serve_command_stream(handler, paths, manager.context.owner.identity, request_id, execute)
