# LLM: 配置接口沿用 Gateway 可信来源与 owner 解析；正文只能传显式操作和模型字段，不能指定 owner 或文件路径。
# 模块用途: 为 TUI 提供私有模型列表、保存和选择，不把密钥写入聊天队列。

from __future__ import annotations

import json

from ..settings.model_profiles import ModelProfileError, execute_model_profile_operation
from .control_service import resolve_gateway_scope_agent


# LLM: 列表永不回传密钥；只返回受控错误，不序列化任意异常或请求内容，不发起模型调用。
# 函数用途: 接受一个已认证用户的模型配置操作，持久化成功后才返回成功。
def handle_client_models(handler, server) -> None:
    from .http_handlers import (
        _gateway_control_scope,
        _http_conversation_id,
        _request_channel,
        require_trusted_source,
    )

    if require_trusted_source(handler):
        return
    if server is None or server.agent is None:
        handler._send_json(503, {"ok": False, "message": "Gateway 尚未就绪。"})
        return
    try:
        body = handler._read_json()
        if not isinstance(body, dict) or not _http_conversation_id(body):
            raise ModelProfileError("缺少会话编号。")
        user_id, channel = _request_channel(handler)
        scope = _gateway_control_scope(handler, body, user_id=user_id, channel=channel)
        agent = resolve_gateway_scope_agent(server.agent, scope)
        result = execute_model_profile_operation(agent, str(body.get("operation") or ""), body)
    except json.JSONDecodeError:
        handler._send_json(400, {"ok": False, "message": "模型配置请求格式错误。"})
        return
    except ModelProfileError as exc:
        handler._send_json(400, {"ok": False, "message": str(exc)})
        return
    except Exception:  # noqa: BLE001 配置错误不得泄露凭证或内部路径
        handler._send_json(500, {"ok": False, "message": "无法读取或保存模型配置，原配置未被主动清除。"})
        return
    handler._send_json(200, result)
