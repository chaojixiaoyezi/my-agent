# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""Endpoint handlers used by the gateway HTTP server."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


# LLM: _ResultAccessContext 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存结果access上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class _ResultAccessContext:

    request_id: str
    user_id: str
    permission: Any


# LLM: _AskRequestContext 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存ask请求上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class _AskRequestContext:

    body: dict
    goal: str
    request_id: str
    user_id: str
    channel: str


# LLM: _request_identity 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 发送identity请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _request_identity(handler) -> tuple[str, Any]:
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return "admin", None
    user_id, _ = mw.extract_identity(dict(handler.headers))
    return user_id, mw.get_permission(dict(handler.headers))


# LLM: _request_channel 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 发送通道请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _request_channel(handler) -> tuple[str, str]:
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return "admin", "chat"
    return mw.extract_identity(dict(handler.headers))


# LLM: _can_read_payload 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 判断载荷条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _can_read_payload(payload: dict, user_id: str, permission: Any) -> bool:
    if permission is None or permission.can_access_all_users:
        return True
    payload_user = payload.get("user_id", payload.get("metadata", {}).get("user_id", ""))
    return payload_user == user_id


# LLM: handle_status 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进状态的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def handle_status(handler, server) -> None:
    if server is None:
        handler._send_json(500, {"error": "server not initialized"})
        return
    state = _read_state(server.paths.state)
    counts = _request_counts(server.paths)
    response = {
        "status": state.get("status", "unknown"),
        "pid": state.get("pid"),
        "uptime": time.time() - state.get("started_at", time.time()),
        "requests": counts,
    }
    handler._send_json(200, response)


# LLM: _read_state 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 读取或查询状态需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _read_state(state_path) -> dict:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


# LLM: _request_counts 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 发送counts请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _request_counts(paths) -> dict[str, int]:
    counts = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
    for name, dir_path in (
        ("pending", paths.inbox),
        ("processing", paths.processing),
        ("done", paths.done),
        ("failed", paths.failed),
    ):
        if dir_path.exists():
            counts[name] = len(list(dir_path.iterdir()))
    return counts


# LLM: handle_result 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进结果的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def handle_result(handler, server) -> None:
    request_id = handler.path[len("/result/"):]
    if server is None:
        handler._send_json(500, {"error": "server not initialized"})
        return
    user_id, permission = _request_identity(handler)
    access = _ResultAccessContext(request_id, user_id, permission)
    response_path = server.paths.responses / f"{request_id}.json"
    if response_path.exists():
        _send_finished_result(handler, response_path, access)
        return
    if _send_pending_state(handler, server.paths.processing, "processing", access):
        return
    if _send_pending_state(handler, server.paths.inbox, "queued", access):
        return
    handler._send_json(404, {"error": "not found", "request_id": request_id})


# LLM: _send_pending_state 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 发送pending状态请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _send_pending_state(handler, folder, status: str, access: _ResultAccessContext) -> bool:
    request_path = folder / f"{access.request_id}.json"
    if not request_path.exists():
        return False
    payload = _read_payload(request_path)
    if payload and not _can_read_payload(payload, access.user_id, access.permission):
        handler._send_json(403, {"error": "forbidden", "request_id": access.request_id})
        return True
    handler._send_json(202, {"status": status, "request_id": access.request_id})
    return True


# LLM: _send_finished_result 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 发送finished结果请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _send_finished_result(handler, response_path, access: _ResultAccessContext) -> None:
    try:
        result = json.loads(response_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        handler._send_json(500, {"error": f"failed to read result: {exc}"})
        return
    if not _can_read_payload(result, access.user_id, access.permission):
        handler._send_json(403, {"error": "forbidden", "request_id": access.request_id})
        return
    handler._send_json(200, result)


# LLM: _read_payload 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 读取或查询载荷需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _read_payload(path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


# LLM: handle_ask 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进ask的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def handle_ask(handler, server, request_id_factory: Callable[[], str]) -> None:
    try:
        body = handler._read_json()
    except json.JSONDecodeError as exc:
        handler._send_json(400, {"error": f"invalid JSON: {exc}"})
        return
    kind = body.get("kind", "ask")
    if kind != "ask":
        handler._send_json(400, {"error": f"unsupported kind: {kind}"})
        return
    goal = body.get("goal", body.get("prompt", ""))
    if not goal:
        handler._send_json(400, {"error": "goal is required"})
        return
    if server is None:
        handler._send_json(500, {"error": "server not initialized"})
        return
    request_id = request_id_factory()
    user_id, channel = _request_channel(handler)
    request_data = _build_ask_request(_AskRequestContext(body, goal, request_id, user_id, channel))
    pending_path = server.paths.inbox / f"{request_id}.json"
    try:
        pending_path.write_text(json.dumps(request_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        handler._send_json(500, {"error": f"failed to write request: {exc}"})
        return
    handler._send_json(202, {"request_id": request_id, "status": "queued"})


# LLM: _build_ask_request 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 构建ask请求所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _build_ask_request(context: _AskRequestContext) -> dict:
    metadata = context.body.get("metadata", {})
    metadata["user_id"] = context.user_id
    metadata["channel"] = context.channel
    return {
        "id": context.request_id,
        "request_id": context.request_id,
        "kind": "ask",
        "goal": context.goal,
        "metadata": metadata,
        "submitted_at": time.time(),
        "user_id": context.user_id,
    }


# LLM: handle_stop 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进handlestop的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def handle_stop(handler, server) -> None:
    if server is None:
        handler._send_json(500, {"error": "server not initialized"})
        return
    server.paths.root.mkdir(parents=True, exist_ok=True)
    server.paths.stop_request.write_text(
        json.dumps({"requested_at": time.time(), "reason": "http stop request"}, ensure_ascii=False),
        encoding="utf-8",
    )
    handler._send_json(200, {"status": "stopping"})


# LLM: handle_session_channels 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进会话channels的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def handle_session_channels(handler, server) -> None:
    from ..auth.middleware import require_admin_handler

    if require_admin_handler(handler):
        return
    parts = handler.path.split("/")
    if len(parts) < 4:
        handler._send_json(400, {"error": "invalid path"})
        return
    if server is None or server.cross_channel is None:
        handler._send_json(500, {"error": "cross channel not initialized"})
        return
    session_id = parts[2]
    bound = server.cross_channel.get_bound_sessions(session_id)
    primary = server.cross_channel.get_primary_channel(session_id)
    handler._send_json(200, {"session_id": session_id, "bound_channels": bound, "primary_channel": primary})


# LLM: handle_session_bind 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进会话bind的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def handle_session_bind(handler, server) -> None:
    from ..auth.middleware import require_admin_handler

    if require_admin_handler(handler):
        return
    parts = handler.path.split("/")
    if len(parts) < 4:
        handler._send_json(400, {"error": "invalid path"})
        return
    body = _read_bind_body(handler)
    if body is None:
        return
    if not body.get("channel"):
        handler._send_json(400, {"error": "channel is required"})
        return
    if server is None or server.cross_channel is None:
        handler._send_json(500, {"error": "cross channel not initialized"})
        return
    session_id = parts[2]
    success = server.cross_channel.bind_session(session_id, body["channel"], body.get("user_id", "admin"))
    handler._send_json(200, {"success": success, "session_id": session_id, "channel": body["channel"]})


# LLM: _read_bind_body 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 读取或查询bindbody需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _read_bind_body(handler) -> dict | None:
    try:
        return handler._read_json()
    except json.JSONDecodeError as exc:
        handler._send_json(400, {"error": f"invalid JSON: {exc}"})
        return None


# LLM: handle_admin_summary 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进管理summary的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
def handle_admin_summary(handler, server) -> None:
    from ..auth.middleware import require_admin_handler

    if require_admin_handler(handler):
        return
    if server is None or server.admin_query is None:
        handler._send_json(500, {"error": "admin query not initialized"})
        return
    summary = server.admin_query.format_admin_summary("admin")
    handler._send_json(200, {"summary": summary})
