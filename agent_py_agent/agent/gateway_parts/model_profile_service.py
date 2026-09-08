# LLM: 配置接口沿用 Gateway 可信 owner 解析和 canonical 路径；冷用户不能触发 Agent/后端/工具初始化，保持配置读写独立。
# 模块用途: 为 TUI 提供私有模型列表、保存和选择，不把密钥写入聊天队列，也不启动用户后台服务。

from __future__ import annotations

import json
from types import SimpleNamespace

from ..settings.model_profiles import ModelProfileError, execute_model_profile_operation
from ..user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from .control_service import resolve_gateway_scope_owner


# LLM: 列表永不回传密钥；只解析可信身份并读写唯一配置，不进入 owner pool；同步检查冷 owner 与跨用户 API 用例。
# 函数用途: 为已认证用户直接读取或原子保存模型配置，落盘后才返回成功，避免被完整 Agent 初始化拖慢。
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
        owner = resolve_gateway_scope_owner(server.agent, scope)
        base_home = server.agent.home_paths
        scoped_home = home_paths_with_owner(base_home, resolve_owner_home(base_home.root, owner))
        config_host = SimpleNamespace(home_paths=scoped_home, config=server.agent.config)
        result = execute_model_profile_operation(config_host, str(body.get("operation") or ""), body)
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
