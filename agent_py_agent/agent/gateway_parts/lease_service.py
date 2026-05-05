from __future__ import annotations

"""Gateway lease management for request processing.

This module provides lease lifecycle management for gateway requests
including heartbeat threads and lease state updates.
"""

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .audit_service import audit_heartbeat_abandoned
from .io import read_json_file, write_json_file_atomic
from .logging import _report_gateway_side_effect_error

if TYPE_CHECKING:
    from ...core import SimpleAgent


# Tracks active heartbeat threads per request ID
_active_heartbeat_request_ids: set[str] = set()


def get_active_heartbeat_request_ids() -> set[str]:
    """Return the set of request IDs with active heartbeats."""
    return _active_heartbeat_request_ids.copy()


def is_heartbeat_alive_for_request(request_id: str) -> bool:
    """Check whether a heartbeat thread is currently active for the given request."""
    return request_id in _active_heartbeat_request_ids


def refresh_processing_lease(
    request_path: Path,
    *,
    request_id: str,
    worker_id: str = "",
) -> bool:
    """Refresh the lease heartbeat on a processing request file."""
    payload = read_json_file(request_path)
    if not payload:
        return False
    payload_id = str(payload.get("id") or request_path.stem)
    if payload_id != request_id:
        return False
    now = time.time()
    payload["id"] = payload_id
    payload["status"] = "processing"
    if worker_id:
        payload["lease_owner"] = worker_id
    else:
        payload.setdefault("lease_owner", "")
    payload.setdefault("lease_started_at", now)
    payload["lease_heartbeat_at"] = now
    payload["updated_at"] = now
    try:
        write_json_file_atomic(request_path, payload)
    except OSError as exc:
        _report_gateway_side_effect_error("gateway_lease_heartbeat", request_id, exc)
        return False
    return True


def _lease_interval(agent: SimpleAgent) -> float:
    """Return a heartbeat cadence short enough to keep processing leases fresh."""
    try:
        gateway_interval = float(agent.config.gateway_heartbeat_interval or 5)
    except (TypeError, ValueError):
        gateway_interval = 5.0
    try:
        processing_timeout = float(agent.config.gateway_processing_timeout_seconds or 900)
    except (TypeError, ValueError):
        processing_timeout = 900.0
    if processing_timeout > 0:
        gateway_interval = min(gateway_interval, max(0.2, processing_timeout / 3.0))
    return max(0.2, gateway_interval)


def start_lease_heartbeat(
    agent: SimpleAgent,
    request_path: Path,
    *,
    request_id: str,
    worker_id: str = "",
) -> tuple[threading.Event, threading.Thread]:
    """Start a daemon thread that keeps one processing lease alive during agent.run."""
    stop_event = threading.Event()
    interval = _lease_interval(agent)
    _active_heartbeat_request_ids.add(request_id)
    thread = threading.Thread(
        target=_heartbeat_loop,
        args=(agent, request_path, request_id, worker_id, stop_event, interval),
        name=f"gateway-lease-{request_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _heartbeat_loop(
    agent: SimpleAgent,
    request_path: Path,
    request_id: str,
    worker_id: str,
    stop_event: threading.Event,
    interval: float,
) -> None:
    """Refresh one gateway processing lease until stopped or abandoned."""
    consecutive_failures = 0
    try:
        while not stop_event.wait(interval):
            ok, consecutive_failures = _refresh_or_count_failure(
                request_path, request_id, worker_id, consecutive_failures
            )
            if ok:
                continue
            if consecutive_failures <= 0:
                return
            if consecutive_failures >= 3:
                audit_heartbeat_abandoned(agent, request_id, consecutive_failures, request_path)
                return
    finally:
        _active_heartbeat_request_ids.discard(request_id)


def _refresh_or_count_failure(
    request_path: Path,
    request_id: str,
    worker_id: str,
    consecutive_failures: int,
) -> tuple[bool, int]:
    try:
        if not refresh_processing_lease(request_path, request_id=request_id, worker_id=worker_id):
            return False, 0
        return True, 0
    except Exception as exc:
        _report_gateway_side_effect_error("heartbeat", request_id, exc)
        return False, consecutive_failures + 1
