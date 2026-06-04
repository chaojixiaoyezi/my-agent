from __future__ import annotations

"""Shared gateway request error response builders."""

import time
from pathlib import Path


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
