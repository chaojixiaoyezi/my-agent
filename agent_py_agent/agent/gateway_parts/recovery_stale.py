from __future__ import annotations

"""Stale processing request listing for gateway diagnostics."""

import time

from .io import gateway_response_path, read_json_file_report
from .paths import GatewayPaths
from .recovery_common import gateway_processing_lease_at, gateway_request_attempts


def gateway_stale_processing(paths: GatewayPaths, timeout_seconds: int) -> list[dict]:
    items: list[dict] = []
    now = time.time()
    timeout_seconds = max(1, int(timeout_seconds or 1))
    for path in sorted(paths.processing.glob("*.json")):
        payload_report = read_json_file_report(path, context="gateway.stale_processing.read")
        payload = payload_report.payload
        request_id = str(payload.get("id") or path.stem)
        if gateway_response_path(paths, request_id).exists():
            continue
        lease_at = gateway_processing_lease_at(payload, path)
        age = now - lease_at if lease_at else 0
        if lease_at and age < timeout_seconds:
            continue
        item = _stale_processing_item(path, payload, age, payload_report.load_error)
        items.append(item)
    return items


def _stale_processing_item(path, payload: dict, age: float, load_error: dict | None) -> dict:
    item = {
        "request_id": str(payload.get("id") or path.stem),
        "path": str(path),
        "age_seconds": round(age, 1) if age else 0,
        "attempts": gateway_request_attempts(payload),
        "lease_owner": str(payload.get("lease_owner") or ""),
        "lease_heartbeat_at": payload.get("lease_heartbeat_at", 0),
        "lease_started_at": payload.get("lease_started_at", 0),
    }
    if load_error is not None:
        item["request_load_error"] = load_error
    return item
