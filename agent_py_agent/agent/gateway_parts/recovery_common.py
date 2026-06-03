from __future__ import annotations

"""Shared request recovery counters and timestamps."""

from pathlib import Path


def gateway_request_attempts(payload: dict) -> int:
    try:
        return int(payload.get("attempts", 0) or 0)
    except (TypeError, ValueError):
        return 0


def gateway_processing_started_at(payload: dict, request_path: Path) -> float:
    return gateway_processing_timestamp(
        payload,
        request_path,
        ("lease_started_at", "started_at", "updated_at", "created_at"),
    )


def gateway_processing_lease_at(payload: dict, request_path: Path) -> float:
    return gateway_processing_timestamp(
        payload,
        request_path,
        ("lease_heartbeat_at", "lease_started_at", "started_at", "updated_at", "created_at"),
    )


def gateway_processing_timestamp(payload: dict, request_path: Path, keys: tuple[str, ...]) -> float:
    for key in keys:
        try:
            value = float(payload.get(key, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    try:
        return request_path.stat().st_mtime
    except OSError:
        return 0
