
from __future__ import annotations

"""Gateway lease management for request processing.

This module provides lease lifecycle management for gateway requests
including heartbeat threads and lease state updates.
"""

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..settings.defaults import default_config_float
from .audit_service import audit_heartbeat_abandoned
from .io import read_json_file_report, update_json_file_atomic
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


@dataclass(frozen=True)
class LeaseRefreshReport:
    ok: bool
    load_error: dict | None = None


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
    return refresh_processing_lease_report(
        request_path,
        request_id=request_id,
        worker_id=worker_id,
    ).ok


def refresh_processing_lease_report(
    request_path: Path,
    *,
    request_id: str,
    worker_id: str = "",
) -> LeaseRefreshReport:
    read_report = read_json_file_report(request_path, context="gateway.lease.request.read")
    if read_report.load_error is not None:
        return LeaseRefreshReport(False, read_report.load_error)
    if not _lease_payload_matches_request(read_report.payload, request_path, request_id):
        return LeaseRefreshReport(False)
    matched_ref = [False]

    def refresh(payload: dict) -> dict:
        if not _lease_payload_matches_request(payload, request_path, request_id):
            return payload
        matched_ref[0] = True
        _update_lease_payload(payload, request_path, worker_id)
        return payload

    try:
        update_json_file_atomic(request_path, refresh, require_existing=True)
    except OSError as exc:
        _report_gateway_side_effect_error("gateway_lease_heartbeat", request_id, exc)
        return LeaseRefreshReport(False)
    return LeaseRefreshReport(matched_ref[0])


def _lease_payload_matches_request(payload: dict, request_path: Path, request_id: str) -> bool:
    if not payload:
        return False
    return str(payload.get("id") or request_path.stem) == request_id


def _update_lease_payload(payload: dict, request_path: Path, worker_id: str) -> None:
    payload_id = str(payload.get("id") or request_path.stem)
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


def _lease_interval(agent: SimpleAgent) -> float:
    gateway_interval = _config_float(agent, "gateway_heartbeat_interval")
    processing_timeout = _config_float(agent, "gateway_processing_timeout_seconds")
    if processing_timeout > 0:
        gateway_interval = min(gateway_interval, max(0.2, processing_timeout / 3.0))
    return max(0.2, gateway_interval)


def _config_float(agent: SimpleAgent, key: str) -> float:
    try:
        value = float(getattr(agent.config, key))
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return default_config_float(key)


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
        report = refresh_processing_lease_report(request_path, request_id=request_id, worker_id=worker_id)
        if not report.ok:
            _report_lease_load_error(request_id, report.load_error)
            return False, 0
        return True, 0
    except Exception as exc:
        _report_gateway_side_effect_error("heartbeat", request_id, exc)
        return False, consecutive_failures + 1


def _report_lease_load_error(request_id: str, load_error: dict | None) -> None:
    if load_error is None:
        return
    print(
        "[gateway-lease-load-error] "
        f"request_id={request_id} "
        f"context={load_error.get('context', '')} "
        f"path={load_error.get('path', '')} "
        f"error={load_error.get('message', '')}",
        file=sys.stderr,
    )
