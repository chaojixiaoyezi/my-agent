# LLM: This read-only transport projects immutable public rows after trusted owner/thread lookup.
# 模块用途: 为主代理和同会话子代理提供原文分页；不接受宿主路径，不启动模型任务。

from __future__ import annotations

import json

from ..conversation.display_archive import (
    DisplayArchiveError,
    read_display_archive_page,
    validate_display_archive_reference,
)
from .control_service import GatewayControlScope, resolve_gateway_scope_agent


# LLM: Owner resolution precedes all file reads; thread metadata comes from the scoped canonical
# store. Historical descendants remain readable after task completion, unrelated sessions do not.
# 函数用途: 核对当前用户和会话的原文读取权限，再取一页，不改变任务状态或上下文。
def read_gateway_display_page(
    base_agent: object, *, scope: GatewayControlScope, reference: object, page_index: object,
) -> dict[str, object]:
    _archive_id, target_id = validate_display_archive_reference(reference)
    owner_agent = resolve_gateway_scope_agent(base_agent, scope)
    store = owner_agent.conversation_store
    thread, error = store.threads.resolve_report(
        channel=scope.channel, channel_conversation_id=scope.conversation_id, channel_user_id=scope.user_id,
    )
    if error is not None or thread is None:
        raise DisplayArchiveError("DISPLAY_SCOPE_DENIED", "当前会话无权读取这份原文。")
    if target_id != thread.thread_id:
        target, error = store.threads.load_report(target_id)
        metadata = target.metadata if target is not None else {}
        if (error is not None or metadata.get("thread_kind") != "agent"
                or metadata.get("root_agent_thread_id") != thread.thread_id):
            raise DisplayArchiveError("DISPLAY_SCOPE_DENIED", "当前会话无权读取这份原文。")
    return read_display_archive_page(store, reference, page_index)


# LLM: The endpoint requires existing authentication and structured conversation identity. Bodies
# cannot select another owner or specify disk paths; public errors contain no underlying exception.
# 函数用途: 接收 TUI 分页请求并返回有界原文，失败保留预览，绝不伪造“已经全部展开”。
def handle_client_display_page(handler, server) -> None:
    from .http_handlers import (
        _gateway_control_scope,
        _http_conversation_id,
        _request_channel,
        require_trusted_source,
    )

    if require_trusted_source(handler):
        return
    if server is None or server.agent is None:
        handler._send_json(503, {"ok": False, "error_code": "GATEWAY_UNAVAILABLE"})
        return
    try:
        body = handler._read_json()
        if not isinstance(body, dict) or not _http_conversation_id(body):
            raise DisplayArchiveError("DISPLAY_REF_INVALID", "缺少会话编号。")
        user_id, channel = _request_channel(handler)
        scope = _gateway_control_scope(handler, body, user_id=user_id, channel=channel)
        result = read_gateway_display_page(
            server.agent, scope=scope, reference=body.get("reference"), page_index=body.get("page_index"),
        )
    except json.JSONDecodeError:
        handler._send_json(400, {"ok": False, "error_code": "DISPLAY_REF_INVALID", "message": "分页请求格式错误。"})
        return
    except DisplayArchiveError as exc:
        status = 403 if exc.code == "DISPLAY_SCOPE_DENIED" else 400
        handler._send_json(status, {"ok": False, "error_code": exc.code, "message": str(exc)})
        return
    except Exception:  # noqa: BLE001 身份/磁盘异常不能向客户端泄露服务器路径
        handler._send_json(503, {"ok": False, "error_code": "DISPLAY_ARCHIVE_UNAVAILABLE", "message": "原文暂时不可读取。"})
        return
    handler._send_json(200, result)
