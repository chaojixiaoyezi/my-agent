# LLM: 配置接口沿用可信 owner 与共享 owner_conversation_store；冷用户不能触发 Agent/后端/工具初始化，配置读写仍独立。
# 模块用途: 为 TUI 提供私有模型列表、保存和选择，共用原会话路径，不把密钥写入聊天队列或启动后台服务。

from __future__ import annotations

import json
from types import SimpleNamespace

from ..settings.model_profiles import ModelProfileError, execute_model_profile_operation
from ..user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from .control_service import resolve_gateway_scope_owner
from .owner_conversation_store import owner_conversation_store


# LLM: 列表永不回传密钥；只从可信 owner/channel binding 取得会话，冷菜单可创建线程但不进入 owner pool。
# 函数用途: 为已认证用户管理共享于本人各会话的模型目录，选择仅保存当前会话，不被完整 Agent 初始化拖慢。
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
        store = owner_conversation_store(server.agent, scoped_home)
        config_host.conversation_store = store
        thread = store.threads.get_or_create({
            "canonical_user_id": scope.user_id, "owner_id": scoped_home.owner_id,
            "owner_home": str(scoped_home.owner_home_dir), "channel": scope.channel,
            "channel_conversation_id": scope.conversation_id, "channel_user_id": scope.user_id,
            "title": "会话设置",
        })
        result = execute_model_profile_operation(
            config_host, str(body.get("operation") or ""), body, thread_id=thread.thread_id,
        )
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
