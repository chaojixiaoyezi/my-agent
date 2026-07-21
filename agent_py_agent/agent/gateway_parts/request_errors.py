from __future__ import annotations

"""Shared gateway request error response builders."""

import time
from pathlib import Path

from ..runtime_errors import runtime_error_report


class ConversationPersistenceError(RuntimeError):
    """结构化会话无法可靠读取或写入时，阻止无上下文继续回答。"""

    error_code = "CONVERSATION_PERSISTENCE_UNAVAILABLE"


class UserReplyUnavailableError(RuntimeError):
    """The model reply phase ended without any text safe for user delivery."""

    error_code = "USER_REPLY_UNAVAILABLE"


def gateway_request_load_error_response(
    request_path: Path,
    load_error: dict,
    *,
    started_at: float | None = None,
) -> dict:
    request_id = str(request_path.stem)
    started_at = time.time() if started_at is None else started_at
    return {
        "id": request_id,
        "kind": "unknown",
        "ok": False,
        "status": "failed",
        "created_at": 0,
        "started_at": started_at,
        "ended_at": 0,
        "duration_seconds": 0,
        "response": "",
        "error_code": "GATEWAY_REQUEST_LOAD_ERROR",
        "error": str(load_error.get("message") or "gateway request file could not be read"),
        "request_load_error": load_error,
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(request_path),
        "attempts": 0,
        "lease_owner": "",
        "lease_started_at": 0,
        "lease_heartbeat_at": 0,
    }


def gateway_response_load_error_response(
    response_path: Path,
    load_error: dict,
    *,
    request_id: str | None = None,
) -> dict:
    return {
        "id": request_id or response_path.stem,
        "kind": "unknown",
        "ok": False,
        "status": "failed",
        "created_at": 0,
        "started_at": 0,
        "ended_at": time.time(),
        "duration_seconds": 0,
        "response": "",
        "error_code": "GATEWAY_RESPONSE_LOAD_ERROR",
        "error": str(load_error.get("message") or "gateway response file could not be read"),
        "response_load_error": load_error,
        "response_file": str(response_path),
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
    }


def gateway_request_processing_state_error_response(
    request_path: Path,
    processing_error: dict,
    *,
    request_id: str,
    started_at: float | None = None,
) -> dict:
    started_at = time.time() if started_at is None else started_at
    return {
        "id": request_id,
        "kind": "unknown",
        "ok": False,
        "status": "failed",
        "created_at": 0,
        "started_at": started_at,
        "ended_at": time.time(),
        "duration_seconds": 0,
        "response": "",
        "error_code": "GATEWAY_REQUEST_PROCESSING_STATE_WRITE_ERROR",
        "error": str(processing_error.get("message") or "gateway request processing state could not be written"),
        "processing_state_error": processing_error,
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(request_path),
        "attempts": 0,
        "lease_owner": "",
        "lease_started_at": 0,
        "lease_heartbeat_at": 0,
    }


def gateway_owner_scope_error_response(
    request_path: Path,
    request: dict,
    error: BaseException,
    *,
    request_id: str,
) -> dict:
    """远程请求无法建立隔离 owner 时的终态响应；不得留 processing 重试或共享回退。"""
    now = time.time()
    return {
        "id": request_id,
        "kind": str(request.get("kind") or "ask"),
        "ok": False,
        "status": "failed",
        "created_at": request.get("created_at", 0),
        "started_at": now,
        "ended_at": now,
        "duration_seconds": 0,
        "response": "",
        "error_code": str(getattr(error, "error_code", "") or "OWNER_SCOPE_UNAVAILABLE"),
        "error": f"{type(error).__name__}: {error}",
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(request_path),
        "attempts": int(request.get("attempts") or 0),
        "lease_owner": "",
        "lease_started_at": 0,
        "lease_heartbeat_at": 0,
    }


def gateway_unhandled_worker_error_response(
    request_path: Path,
    request: dict,
    error: BaseException,
    *,
    request_id: str,
) -> dict:
    """Build a terminal response when a worker fails outside the turn handler.

    Normal model/tool failures are handled by ``_handle_gateway_request``.  This
    boundary covers earlier failures such as thread-local agent construction;
    leaving those requests in ``processing`` would make status and recovery lie
    until the lease timeout expires.
    """

    now = time.time()
    try:
        started_at = float(request.get("lease_started_at") or now)
    except (TypeError, ValueError):
        started_at = now
    report = runtime_error_report(error, context="gateway.worker.unhandled")
    return {
        "id": request_id,
        "kind": str(request.get("kind") or "ask"),
        "ok": False,
        "status": "failed",
        "created_at": request.get("created_at", 0),
        "started_at": started_at,
        "ended_at": now,
        "duration_seconds": round(max(0.0, now - started_at), 3),
        "response": "",
        "error_code": str(
            getattr(error, "error_code", "") or "GATEWAY_WORKER_UNHANDLED_ERROR"
        ),
        "error": f"{type(error).__name__}: {error}",
        "worker_error": report,
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(request_path),
        "attempts": int(request.get("attempts") or 0),
        "lease_owner": str(request.get("lease_owner") or ""),
        "lease_started_at": request.get("lease_started_at", 0),
        "lease_heartbeat_at": request.get("lease_heartbeat_at", 0),
    }
