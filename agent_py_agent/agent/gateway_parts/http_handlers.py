
from __future__ import annotations

"""Endpoint handlers used by the gateway HTTP server."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..runtime_errors import runtime_error_report
from .io import gateway_request_counts


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
    user_id, _ = mw.extract_identity(dict(handler.headers))
    return user_id, mw.get_permission(dict(handler.headers))


def _request_channel(handler) -> tuple[str, str]:
    mw = getattr(handler, "_auth_middleware", None)
    if mw is None:
        return "admin", "chat"
    return mw.extract_identity(dict(handler.headers))


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
        _send_finished_result(handler, response_path, access)
        return
    if _send_pending_state(handler, server.paths.processing, "processing", access):
        return
    if _send_pending_state(handler, server.paths.inbox, "queued", access):
        return
    handler._send_json(404, {"error": "not found", "request_id": request_id})


def _send_pending_state(handler, folder, status: str, access: _ResultAccessContext) -> bool:
    request_path = folder / f"{access.request_id}.json"
    if not request_path.exists():
        return False
    payload, request_load_error = _read_payload_report(request_path, context="gateway.http_pending.read")
    if payload and not _can_read_payload(payload, access.user_id, access.permission):
        handler._send_json(403, {"error": "forbidden", "request_id": access.request_id})
        return True
    response = {"status": status, "request_id": access.request_id}
    if request_load_error:
        response["request_load_error"] = request_load_error
    handler._send_json(202, response)
    return True


def _send_finished_result(handler, response_path, access: _ResultAccessContext) -> None:
    result, result_load_error = _read_payload_report(response_path, context="gateway.http_result.read")
    if result_load_error:
        handler._send_json(
            500,
            {
                "error": "failed to read result",
                "request_id": access.request_id,
                "result_load_error": result_load_error,
            },
        )
        return
    if not _can_read_payload(result, access.user_id, access.permission):
        handler._send_json(403, {"error": "forbidden", "request_id": access.request_id})
        return
    handler._send_json(200, result)


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
    for key in ("conversation_id", "session_id", "thread_id", "channel_conversation_id"):
        value = str(body.get(key) or "").strip()
        if value:
            return value
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        for key in ("conversation_id", "session_id", "thread_id", "channel_conversation_id"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return value
    return "default"


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
