from __future__ import annotations

"""Gateway lease management for request processing.

This module provides lease lifecycle management for gateway requests
including heartbeat threads and lease state updates.
"""

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .audit_service import audit_heartbeat_abandoned
from .io import read_json_file, write_json_file_atomic
from .logging import _report_gateway_side_effect_error

if TYPE_CHECKING:
    from ...core import SimpleAgent


# Tracks active heartbeat threads per request ID
_active_heartbeat_request_ids: set[str] = set()


@dataclass(frozen=True)
class _HeartbeatLoopContext:

    agent: SimpleAgent
    request_path: Path
    request_id: str
    worker_id: str
    stop_event: threading.Event
    interval: float


def get_active_heartbeat_request_ids() -> set[str]:
    return _active_heartbeat_request_ids.copy()


def is_heartbeat_alive_for_request(request_id: str) -> bool:
    return request_id in _active_heartbeat_request_ids


def refresh_processing_lease(
    request_path: Path,
    *,
    request_id: str,
    worker_id: str = "",
) -> bool:
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
    stop_event = threading.Event()
    interval = _lease_interval(agent)
    _active_heartbeat_request_ids.add(request_id)
    thread = threading.Thread(
        target=_heartbeat_loop,
        args=(_HeartbeatLoopContext(agent, request_path, request_id, worker_id, stop_event, interval),),
        name=f"gateway-lease-{request_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _heartbeat_loop(context: _HeartbeatLoopContext) -> None:
    try:
        _run_heartbeat_loop_body(context)
    finally:
        _active_heartbeat_request_ids.discard(context.request_id)


def _run_heartbeat_loop_body(context: _HeartbeatLoopContext) -> None:
    consecutive_failures = 0
    while not context.stop_event.wait(context.interval):
        ok, consecutive_failures = _refresh_or_count_failure(
            context.request_path, context.request_id, context.worker_id, consecutive_failures
        )
        if ok or not _should_stop_heartbeat(
            context.agent,
            context.request_path,
            context.request_id,
            consecutive_failures,
        ):
            continue
        return


def _should_stop_heartbeat(
    agent: SimpleAgent,
    request_path: Path,
    request_id: str,
    consecutive_failures: int,
) -> bool:
    if consecutive_failures <= 0:
        return True
    if consecutive_failures < 3:
        return False
    audit_heartbeat_abandoned(agent, request_id, consecutive_failures, request_path)
    return True


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
