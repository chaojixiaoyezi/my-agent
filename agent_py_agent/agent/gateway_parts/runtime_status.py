from __future__ import annotations

"""Persisted runtime status for gateway daemon diagnostics."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .daemon_metadata import (
    _get_process_start_time,
    _read_json_file,
    _utc_now_iso,
    _write_json_file,
)


@dataclass(frozen=True)
class WriteRuntimeStatusParams:
    """Bundle of write_runtime_status parameters."""

    status_path: Path
    gateway_state: Any = None
    exit_reason: Any = None
    restart_requested: bool = False
    active_agents: int = 0
    platform: str | None = None
    platform_state: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    extra: dict[str, Any] | None = None


def _base_runtime_payload(status_path: Path) -> dict:
    return _read_json_file(status_path) or {
        "kind": "my-agent-gateway",
        "pid": os.getpid(),
        "start_time": _get_process_start_time(os.getpid()),
        "gateway_state": "unknown",
        "exit_reason": None,
        "restart_requested": False,
        "active_agents": 0,
        "platforms": {},
        "updated_at": _utc_now_iso(),
    }


def _merge_runtime_fields(payload: dict, params: WriteRuntimeStatusParams) -> None:
    payload.setdefault("platforms", {})
    payload["pid"] = os.getpid()
    payload["start_time"] = _get_process_start_time(os.getpid())
    payload["updated_at"] = _utc_now_iso()
    if params.gateway_state is not None:
        payload["gateway_state"] = params.gateway_state
    if params.exit_reason is not None:
        payload["exit_reason"] = params.exit_reason
    payload["restart_requested"] = params.restart_requested
    payload["active_agents"] = max(0, int(params.active_agents))
    if params.extra:
        payload.update({key: value for key, value in params.extra.items() if value is not None})


def _merge_platform_status(payload: dict, params: WriteRuntimeStatusParams) -> None:
    if params.platform is None:
        return
    platform_payload = payload["platforms"].get(params.platform, {})
    if params.platform_state is not None:
        platform_payload["state"] = params.platform_state
    if params.error_code is not None:
        platform_payload["error_code"] = params.error_code
    if params.error_message is not None:
        platform_payload["error_message"] = params.error_message
    platform_payload["updated_at"] = _utc_now_iso()
    payload["platforms"][params.platform] = platform_payload


def write_runtime_status(params: WriteRuntimeStatusParams) -> None:
    """Persist gateway runtime health information for diagnostics/status."""
    payload = _base_runtime_payload(params.status_path)
    _merge_runtime_fields(payload, params)
    _merge_platform_status(payload, params)
    _write_json_file(params.status_path, payload)


def read_runtime_status(status_path: Path) -> dict | None:
    """Read the persisted gateway runtime health/status information."""
    return _read_json_file(status_path)
