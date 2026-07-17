
from __future__ import annotations

"""Endpoint handlers used by the gateway HTTP server."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..auth.middleware import _handler_peer_ip, require_admin_handler, require_trusted_source
from ..conversation.channels import project_user_reply, redact_host_absolute_paths
from ..conversation.control_commands import parse_conversation_control
from ..runtime_errors import runtime_error_report
from .control_service import (
    GatewayControlScope,
    execute_gateway_conversation_control,
    steer_active_conversation_if_running,
)
from .io import gateway_request_counts
from .paths import gateway_chunk_path, gateway_chunk_path_candidates


@dataclass(frozen=True)
class _ResultAccessContext:

    request_id: str
    user_id: str
    permission: Any


@dataclass(frozen=True)
class _AskRequestContext:

    body: dict
    goal: str
    request_id: str
    user_id: str
    channel: str


def _request_identity(handler) -> tuple[str, Any]:
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return "admin", None
    peer_ip = _handler_peer_ip(handler)
    user_id, _ = mw.extract_identity(dict(handler.headers), peer_ip)
    return user_id, mw.get_permission(dict(handler.headers), peer_ip)


def _request_channel(handler) -> tuple[str, str]:
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return "admin", "chat"
    return mw.extract_identity(dict(handler.headers), _handler_peer_ip(handler))


def _can_read_payload(payload: dict, user_id: str, permission: Any) -> bool:
    if permission is None or permission.can_access_all_users:
        return True
    payload_user = payload.get("user_id", payload.get("metadata", {}).get("user_id", ""))
    return payload_user == user_id


def handle_status(handler, server) -> None:
    if server is None:
        handler._send_json(500, {"error": "server not initialized"})
        return
    state, state_load_error = _read_state_report(server.paths.state)
    counts = gateway_request_counts(server.paths, include_archives=False)
    response = {
        "status": state.get("status", "unknown"),
        "pid": state.get("pid"),
        "uptime": time.time() - state.get("started_at", time.time()),
        "requests": counts,
    }
    if state_load_error:
        response["state_load_error"] = state_load_error
    if isinstance(state.get("server_error"), dict):
        response["server_error"] = state["server_error"]
    handler._send_json(200, response)


def _read_state(state_path) -> dict:
    state, _ = _read_state_report(state_path)
    return state


def _read_state_report(state_path) -> tuple[dict, dict[str, Any] | None]:
    if not state_path.exists():
        return {}, None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return {}, _state_load_error(state_path, exc)
    if isinstance(payload, dict):
        return payload, None
    return {}, _state_load_error(
        state_path,
        ValueError(f"gateway state JSON root is {type(payload).__name__}, expected object"),
    )


def _state_load_error(state_path, exc: BaseException) -> dict[str, Any]:
    report = runtime_error_report(exc, context="gateway.http_state.read")
    report["path"] = str(state_path)
    return report


def handle_result(handler, server) -> None:
    request_id = handler.path[len("/result/"):]
    if server is None:
        handler._send_json(500, {"error": "server not initialized"})
        return
    user_id, permission = _request_identity(handler)
    access = _ResultAccessContext(request_id, user_id, permission)
    response_path = server.paths.responses / f"{request_id}.json"
    if response_path.exists():
        _send_finished_result(handler, server.paths, response_path, access)
        return
    if _send_pending_state(handler, server.paths.processing, "processing", access):
        return
    if _send_pending_state(handler, server.paths.inbox, "queued", access):
        return
    handler._send_json(404, {"error": "not found", "request_id": request_id})


# LLM: progress 读取沿用 result 的请求 owner 鉴权；chunk 正文和 response 都不能自证身份。
# 函数用途: 按行游标返回当前 request 已启用的 typed 工具进度。
def handle_progress(handler, server) -> None:
    parsed = urlsplit(handler.path)
    request_id = parsed.path[len("/progress/") :]
    if server is None:
        handler._send_json(500, {"error": "server not initialized"})
        return
    if not request_id or request_id != request_id.replace("/", "").replace("\\", ""):
        handler._send_json(400, {"error": "invalid request id"})
        return
    user_id, permission = _request_identity(handler)
    access = _ResultAccessContext(request_id, user_id, permission)
    if not _can_read_finished_request(server.paths, access):
        handler._send_json(403, {"error": "forbidden", "request_id": request_id})
        return
    raw_since = parse_qs(parsed.query).get("since", ["0"])[0]
    try:
        since = max(0, int(raw_since))
    except (TypeError, ValueError):
        handler._send_json(400, {"error": "since must be a non-negative integer"})
        return
    chunk_path = gateway_chunk_path(server.paths, request_id)
    actual_path = next((path for path in gateway_chunk_path_candidates(chunk_path) if path.exists()), None)
    if actual_path is None:
        handler._send_json(
            200,
            {"request_id": request_id, "events": [], "next": since},
        )
        return
    events, next_cursor = _read_public_progress_events(actual_path, since)
    handler._send_json(
        200,
        {"request_id": request_id, "events": events, "next": next_cursor},
    )


def _read_public_progress_events(path, since: int) -> tuple[list[dict[str, object]], int]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return [], since
    events: list[dict[str, object]] = []
    for line in lines[since : since + 200]:
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(row, dict):
            continue
        if row.get("kind") == "assistant_commentary":
            # Model commentary is safe at every verbose level only after the
            # same user-facing projection used by final channel delivery.
            text = redact_host_absolute_paths(
                project_user_reply(row.get("text", "")).content
            ).strip()
            if text:
                events.append({"kind": "assistant_commentary", "text": text})
            continue
        if row.get("kind") != "tool_progress":
            continue
        level = str(row.get("verbose_level") or "off")
        progress = row.get("progress")
        if level not in {"on", "full"} or not isinstance(progress, dict):
            continue
        events.append({"level": level, **progress})
    return events, min(len(lines), since + 200)


def _send_pending_state(handler, folder, status: str, access: _ResultAccessContext) -> bool:
    request_path = folder / f"{access.request_id}.json"
    if not request_path.exists():
        return False
    payload, request_load_error = _read_payload_report(request_path, context="gateway.http_pending.read")
    if request_load_error and not _all_user_access(access):
        # 请求记录损坏时无法证明 owner；普通用户必须 fail-closed，不能顺带拿到
        # load report 中的服务器路径。管理员仍保留完整诊断视图。
        handler._send_json(403, {"error": "forbidden", "request_id": access.request_id})
        return True
    if payload and not _can_read_payload(payload, access.user_id, access.permission):
        handler._send_json(403, {"error": "forbidden", "request_id": access.request_id})
        return True
    response = {"status": status, "request_id": access.request_id}
    if request_load_error:
        response["request_load_error"] = request_load_error
    handler._send_json(202, response)
    return True


def _send_finished_result(handler, paths, response_path, access: _ResultAccessContext) -> None:
    if not _can_read_finished_request(paths, access):
        handler._send_json(403, {"error": "forbidden", "request_id": access.request_id})
        return
    result, result_load_error = _read_payload_report(response_path, context="gateway.http_result.read")
    if result_load_error:
        response = {
            "error": "failed to read result",
            "request_id": access.request_id,
        }
        if _all_user_access(access):
            response["result_load_error"] = result_load_error
        handler._send_json(
            500,
            response,
        )
        return
    handler._send_json(200, result if _all_user_access(access) else _public_result(result))


def _all_user_access(access: _ResultAccessContext) -> bool:
    return access.permission is None or access.permission.can_access_all_users


_PUBLIC_RESULT_FIELDS = (
    "id",
    "request_id",
    "kind",
    "ok",
    "status",
    "created_at",
    "started_at",
    "ended_at",
    "duration_seconds",
    "response",
    "error_code",
    "backend",
    "used_memories",
    "tool_rounds",
    "attempts",
    "current_context_token_estimate",
    "prompt_token_estimate",
    "runtime_injection_token_estimate",
    "turn_token_estimate",
    "cumulative_token_estimate",
    "memory_resume_context_injected",
    "memory_resume_context_matches",
    "memory_resume_context_token_estimate",
    "conversation_persist_degraded",
)


# LLM: USER 的 HTTP result 是对内部 response record 的白名单投影；新增运行字段默认不公开。
# 函数用途: 保留客户端需要的状态/正文/计数，同时移除路径、lease、prompt 和内部错误细节。
def _public_result(result: dict) -> dict[str, object]:
    public = {key: result[key] for key in _PUBLIC_RESULT_FIELDS if key in result}
    delivery = result.get("channel_delivery")
    if isinstance(delivery, dict):
        public["channel_delivery"] = {
            key: delivery[key]
            for key in ("content", "artifact_names", "internal_signal", "projection_status")
            if key in delivery
        }
    if result.get("ok") is False and result.get("error_code"):
        public["error"] = "任务处理失败，请稍后重试。"
    return public


def _can_read_finished_request(paths, access: _ResultAccessContext) -> bool:
    """Authorize a finished response from its request record, never its output body.

    Request identity stays authoritative while the record moves from the hot queue
    into done/failed. Missing, unreadable, or conflicting copies therefore deny a
    USER by default; trusted admin callers keep their existing all-user access.
    """
    if _all_user_access(access):
        return True
    found_request = False
    for folder in (paths.processing, paths.inbox, paths.done, paths.failed):
        request_path = folder / f"{access.request_id}.json"
        if not request_path.exists():
            continue
        found_request = True
        payload, load_error = _read_payload_report(
            request_path,
            context="gateway.http_result_request.read",
        )
        if load_error or not _can_read_payload(payload, access.user_id, access.permission):
            return False
    return found_request


def _read_payload(path) -> dict:
    payload, _ = _read_payload_report(path, context="gateway.http_payload.read")
    return payload


def _read_payload_report(path, *, context: str) -> tuple[dict, dict[str, Any] | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return {}, _payload_load_error(path, exc, context)
    if isinstance(payload, dict):
        return payload, None
    return {}, _payload_load_error(
        path,
        ValueError(f"gateway payload JSON root is {type(payload).__name__}, expected object"),
        context,
    )


def _payload_load_error(path, exc: BaseException, context: str) -> dict[str, Any]:
    report = runtime_error_report(exc, context=context)
    report["path"] = str(path)
    return report


def handle_ask(handler, server, request_id_factory: Callable[[], str]) -> None:
    if require_trusted_source(handler):
        return  # 不可信来源(远程无 token)拒绝派工:已发 403(回环本机/单机放行,渠道用户经适配器可提交)
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
    user_id, channel = _request_channel(handler)
    active_turn = _route_ask_to_active_turn(
        server,
        body=body,
        goal=str(goal),
        user_id=user_id,
        channel=channel,
    )
    if active_turn is not None and active_turn.ok:
        handler._send_json(
            202,
            {
                "request_id": active_turn.request_id,
                "status": "steered",
                "disposition": "active_turn_input",
            },
        )
        return
    request_id = request_id_factory()
    request_data = _build_ask_request(_AskRequestContext(body, goal, request_id, user_id, channel))
    pending_path = server.paths.inbox / f"{request_id}.json"
    try:
        pending_path.write_text(json.dumps(request_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        handler._send_json(500, {"error": f"failed to write request: {exc}"})
        return
    from ..observability.concurrency_metrics import gateway_request_enqueued

    gateway_request_enqueued()  # §6-A 进队计数:/ask 直写 inbox 不经 write_gateway_request,单独补点
    handler._send_json(202, {"request_id": request_id, "status": "queued"})


# LLM: The busy-turn decision is based only on authenticated scope plus durable runtime state;
# the prompt body is never inspected for task/chat intent.
# 函数用途：把同一 owner/thread 运行期间的新普通输入交给当前轮；无活跃轮时保持普通入队。
def _route_ask_to_active_turn(
    server,
    *,
    body: dict,
    goal: str,
    user_id: str,
    channel: str,
):
    agent = getattr(server, "agent", None)
    if agent is None:
        return None
    metadata = body.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    return steer_active_conversation_if_running(
        agent,
        server.paths,
        message=goal,
        scope=GatewayControlScope(
            user_id=user_id,
            channel=channel,
            conversation_id=_http_conversation_id(body),
            metadata=metadata,
        ),
    )


# LLM: /control authenticates the caller before selecting a structured owner conversation.
# 函数用途：让 IM/CLI 立即查询、纠偏或停止自己的当前任务，不进入普通 /ask 队列。
def handle_control(handler, server) -> None:
    if require_trusted_source(handler):
        return
    if server is None or server.agent is None:
        handler._send_json(500, {"error": "server control service not initialized"})
        return
    try:
        body = handler._read_json()
    except json.JSONDecodeError as exc:
        handler._send_json(400, {"error": f"invalid JSON: {exc}"})
        return
    command = parse_conversation_control(body.get("command", body.get("prompt", "")))
    if command is None:
        handler._send_json(400, {"error": "unsupported conversation control"})
        return
    user_id, channel = _request_channel(handler)
    permission_user_id, permission = _request_identity(handler)
    if getattr(handler, "_auth_middleware", None) is None:
        user_id = str(body.get("user_id") or user_id).strip()
        channel = str(body.get("channel") or channel).strip()
    conversation_id = _http_conversation_id(body)
    if not conversation_id:
        handler._send_json(400, {"error": "conversation_id is required"})
        return
    metadata = body.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    result = execute_gateway_conversation_control(
        server.agent,
        server.paths,
        command,
        GatewayControlScope(
            user_id=user_id,
            channel=channel,
            conversation_id=conversation_id,
            metadata=dict(metadata),
            all_user_access=(
                permission is None
                or bool(getattr(permission, "can_access_all_users", False))
            )
            and permission_user_id == user_id,
        ),
    )
    handler._send_json(200, result.to_dict())


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
        "priority": "interactive",
        "source": f"http:{context.channel}",
        "submitted_at": time.time(),
        # 进队时刻(§6-A 排队等待直方图的起点):/ask 直写 inbox 不经 write_gateway_request,
        # 曾缺此字段致 queue_wait 探针在 HTTP 主路径恒 0 样本(压测实锤)。
        "created_at": time.time(),
        "user_id": context.user_id,
        "conversation": _http_conversation_payload(context),
    }


def _http_conversation_payload(context: _AskRequestContext) -> dict:
    conversation_id = _http_conversation_id(context.body)
    return {
        "channel": str(context.channel or "http"),
        "channel_conversation_id": conversation_id,
        "channel_user_id": str(context.user_id or "anonymous"),
        "canonical_user_id": str(context.user_id or "anonymous"),
    }


def _http_conversation_id(body: dict) -> str:
    if value := _first_conversation_id(body):
        return value
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        return _first_conversation_id(metadata) or "default"
    return "default"


def _first_conversation_id(payload: dict) -> str:
    for key in ("conversation_id", "session_id", "thread_id", "channel_conversation_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def handle_stop(handler, server) -> None:
    if require_admin_handler(handler):
        return  # 停网关需管理员:已发 403(鉴权未接线时返回 False,回环本机请求放行)
    if server is None:
        handler._send_json(500, {"error": "server not initialized"})
        return
    server.paths.root.mkdir(parents=True, exist_ok=True)
    server.paths.stop_request.write_text(
        json.dumps({"requested_at": time.time(), "reason": "http stop request"}, ensure_ascii=False),
        encoding="utf-8",
    )
    handler._send_json(200, {"status": "stopping"})


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


def _read_bind_body(handler) -> dict | None:
    try:
        return handler._read_json()
    except json.JSONDecodeError as exc:
        handler._send_json(400, {"error": f"invalid JSON: {exc}"})
        return None


def handle_admin_summary(handler, server) -> None:
    from ..auth.middleware import require_admin_handler

    if require_admin_handler(handler):
        return
    if server is None or server.admin_query is None:
        handler._send_json(500, {"error": "admin query not initialized"})
        return
    summary = server.admin_query.format_admin_summary("admin")
    handler._send_json(200, {"summary": summary})
