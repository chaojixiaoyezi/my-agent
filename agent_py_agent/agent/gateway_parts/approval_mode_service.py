# LLM: 审批模式变更必须经过现有 HTTP 认证与 owner 解析；正文 owner/path/自然语言无授权效力，冷用户不初始化完整 Agent。
# 模块用途: 为 TUI 提供当前用户审批模式读写入口，不进入聊天、LLM 或任务队列。

from __future__ import annotations

import json

from ..user_space.approval_mode import ApprovalModeError, execute_approval_mode_operation
from ..user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from .control_service import resolve_gateway_scope_owner


# LLM: 复用可信源和精确 owner 作用域；只传枚举给唯一策略写入口，禁止客户端改权限/禁用表/其他用户。
# 函数用途: 读取或原子保存本用户审批模式，配置错误明确失败且不泄漏策略正文。
def handle_client_approval_mode(handler, server) -> None:
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
            raise ApprovalModeError("缺少会话编号。")
        user_id, channel = _request_channel(handler)
        scope = _gateway_control_scope(handler, body, user_id=user_id, channel=channel)
        owner = resolve_gateway_scope_owner(server.agent, scope)
        base = server.agent.home_paths
        home = home_paths_with_owner(base, resolve_owner_home(base.root, owner))
        result = execute_approval_mode_operation(home, str(body.get("operation") or ""), body.get("mode"))
    except (json.JSONDecodeError, ApprovalModeError) as exc:
        handler._send_json(400, {"ok": False, "message": str(exc) if isinstance(exc, ApprovalModeError) else "审批请求格式错误。"})
        return
    except Exception:  # noqa: BLE001 不外发私有策略和鉴权异常
        handler._send_json(500, {"ok": False, "message": "审批配置操作失败，未确认保存成功。"})
        return
    handler._send_json(200, result)
