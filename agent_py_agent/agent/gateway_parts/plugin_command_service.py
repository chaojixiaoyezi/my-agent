# LLM: 入口复用可信来源检查与 GatewayControlScope；命令文本、客户端 scope_ref/revision 不参与 owner 解析。
# 模块用途: 向 TUI 提供静态插件目录，并将 HTTP 插件命令留在独立命令分路，冷用户保持未加载。

from __future__ import annotations

import json

from ..plugin_command_service import (
    execute_plugin_command,
    plugin_catalog_unavailable,
    read_plugin_catalog,
)
from .control_service import resolve_gateway_scope_owner


# LLM: 调用方必须已完成来源鉴权；只复用原 owner 解析，不创建 Agent、thread、模型请求或控制账本。
# 函数用途: 为新目录接口以及 /ask、/control 的插件输入生成同一宿主回执。
def plugin_http_response(handler, server, body: dict, *, text: str | None = None) -> dict:
    from .http_handlers import _gateway_control_scope, _request_channel

    try:
        if server is None or server.agent is None:
            return plugin_catalog_unavailable()
        user_id, channel = _request_channel(handler)
        scope = _gateway_control_scope(handler, body, user_id=user_id, channel=channel)
        owner = resolve_gateway_scope_owner(server.agent, scope)
        catalog = read_plugin_catalog(
            owner, channel=scope.channel, conversation_id=scope.conversation_id
        )
        if text is None:
            return {"ok": True, "catalog": catalog.to_payload()}
        revision = body.get("catalog_revision", "")
        if not isinstance(revision, str):
            raise ValueError("目录版本必须是字符串")
        result = execute_plugin_command(catalog, text, revision=revision)
        return result if result is not None else plugin_catalog_unavailable()
    except Exception:  # noqa: BLE001 目录错误不能泄露内部身份、路径或凭证，也不能进入模型队列
        return plugin_catalog_unavailable()


# LLM: 来源检查先于正文读取；catalog/command 是唯一显式操作，非法载荷不能隐式转换成业务请求。
# 函数用途: 读取插件命令声明或提交一次插件命令，错误时保留原任务不动。
def handle_client_plugins(handler, server) -> None:
    from ..plugin_commands import plugin_namespace
    from .http_handlers import require_trusted_source

    if require_trusted_source(handler):
        return
    try:
        body = handler._read_json()
        if not isinstance(body, dict) or body.get("operation") not in {"catalog", "command"}:
            raise ValueError("请求操作无效")
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
    result = plugin_http_response(handler, server, body, text=text)
    handler._send_json(
        503 if result.get("error_code") == "PLUGIN_CATALOG_UNAVAILABLE" else 200, result
    )
