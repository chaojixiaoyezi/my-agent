
from __future__ import annotations

"""Gateway lease management for request processing.

This module provides lease lifecycle management for gateway requests
including heartbeat threads and lease state updates.
"""

# LLM: This module owns Gateway execution leases. Every refresh must compare the immutable
# execution attempt and monotonic lease epoch so a stale worker cannot keep a replacement alive.
# 模块用途: 管理 Gateway 请求心跳，并用执行代次阻止旧 worker 覆盖新接管者。

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..settings.defaults import default_config_float
from .audit_service import audit_heartbeat_abandoned
from .io import gateway_turn_transition, read_json_file_report, update_json_file_atomic
from .logging import _report_gateway_side_effect_error
from .paths import gateway_paths_from_root

if TYPE_CHECKING:
    from ...core import SimpleAgent


# Tracks active heartbeat threads per request ID
_active_heartbeat_request_ids: set[str] = set()
_active_heartbeat_attempts: set[tuple[str, str, int]] = set()
_active_heartbeat_lock = threading.RLock()


@dataclass(frozen=True)
class _HeartbeatLoopContext:

    agent: SimpleAgent
    request_path: Path
    request_id: str
    worker_id: str
    execution_attempt_id: str
    lease_epoch: int
    stop_event: threading.Event
    interval: float


@dataclass(frozen=True)
class LeaseRefreshReport:
    ok: bool
    load_error: dict | None = None


def get_active_heartbeat_request_ids() -> set[str]:
    with _active_heartbeat_lock:
        return _active_heartbeat_request_ids.copy()


# LLM: Recovery may ask about a specific execution fence; the legacy request-only projection is
# retained only for status/tests and never proves that another attempt is still the owner.
# 函数用途: 判断整条请求或某个精确执行代次的本机心跳线程是否仍存活。
def is_heartbeat_alive_for_request(
    request_id: str,
    *,
    execution_attempt_id: str = "",
    lease_epoch: int = 0,
) -> bool:
    attempt_id = str(execution_attempt_id or "").strip()
    epoch = _normalized_lease_epoch(lease_epoch)
    with _active_heartbeat_lock:
        if attempt_id or epoch:
            return (str(request_id), attempt_id, epoch) in _active_heartbeat_attempts
        return request_id in _active_heartbeat_request_ids


def refresh_processing_lease(
    request_path: Path,
    *,
    request_id: str,
    worker_id: str = "",
    execution_attempt_id: str = "",
    lease_epoch: int = 0,
) -> bool:
    return refresh_processing_lease_report(
        request_path,
        request_id=request_id,
        worker_id=worker_id,
        execution_attempt_id=execution_attempt_id,
        lease_epoch=lease_epoch,
    ).ok


def refresh_processing_lease_report(
    request_path: Path,
    *,
    request_id: str,
    worker_id: str = "",
    execution_attempt_id: str = "",
    lease_epoch: int = 0,
) -> LeaseRefreshReport:
    paths = gateway_paths_from_root(_gateway_root_for_processing_path(request_path))
    with gateway_turn_transition(paths, request_id):
        read_report = read_json_file_report(request_path, context="gateway.lease.request.read")
        if read_report.load_error is not None:
            return LeaseRefreshReport(False, read_report.load_error)
        if not _lease_payload_matches_request(
            read_report.payload,
            request_path,
            request_id,
            worker_id=worker_id,
            execution_attempt_id=execution_attempt_id,
            lease_epoch=lease_epoch,
        ):
            return LeaseRefreshReport(False)
        matched_ref = [False]

        def refresh(payload: dict) -> dict:
            if not _lease_payload_matches_request(
                payload,
                request_path,
                request_id,
                worker_id=worker_id,
                execution_attempt_id=execution_attempt_id,
                lease_epoch=lease_epoch,
            ):
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


# LLM: Production requests live under root/requests/processing; focused tests may use a direct
# root/processing fixture. Both must resolve the same T-lock root as terminal and recovery code.
# 函数用途: 从 processing 文件位置解析 Gateway 根目录，供心跳取得精确回合锁。
def _gateway_root_for_processing_path(request_path: Path) -> Path:
    processing_dir = request_path.parent
    parent = processing_dir.parent
    return parent.parent if parent.name == "requests" else parent


# LLM: The request id alone is not a lease fence because requeue reuses it. Optional legacy
# callers may omit the new fields, while production heartbeat always supplies both.
# 函数用途: 校验请求文件仍由当前 worker 的精确执行代次持有。
def _lease_payload_matches_request(
    payload: dict,
    request_path: Path,
    request_id: str,
    *,
    worker_id: str = "",
    execution_attempt_id: str = "",
    lease_epoch: int = 0,
) -> bool:
    if not payload:
        return False
    if str(payload.get("id") or request_path.stem) != request_id:
        return False
    expected_attempt = str(execution_attempt_id or "").strip()
    if expected_attempt and str(payload.get("execution_attempt_id") or "") != expected_attempt:
        return False
    expected_epoch = _normalized_lease_epoch(lease_epoch)
    if expected_epoch and _normalized_lease_epoch(payload.get("lease_epoch")) != expected_epoch:
        return False
    current_owner = str(payload.get("lease_owner") or "").strip()
    return not (worker_id and current_owner and current_owner != worker_id)


# LLM: Lease epochs are persisted data and malformed values fail closed to zero rather than
# comparing Python truthiness or natural-language status.
# 函数用途: 把租约代次安全转换成非负整数，坏值视为未设置。
def _normalized_lease_epoch(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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
    execution_attempt_id: str = "",
    lease_epoch: int = 0,
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    interval = _lease_interval(agent)
    attempt_id = str(execution_attempt_id or "").strip()
    epoch = _normalized_lease_epoch(lease_epoch)
    with _active_heartbeat_lock:
        _active_heartbeat_request_ids.add(request_id)
        _active_heartbeat_attempts.add((request_id, attempt_id, epoch))
    thread = threading.Thread(
        target=_heartbeat_loop,
        args=(
            _HeartbeatLoopContext(
                agent,
                request_path,
                request_id,
                worker_id,
                attempt_id,
                epoch,
                stop_event,
                interval,
            ),
        ),
        name=f"gateway-lease-{request_id}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _heartbeat_loop(context: _HeartbeatLoopContext) -> None:
    try:
        _run_heartbeat_loop_body(context)
    finally:
        with _active_heartbeat_lock:
            _active_heartbeat_attempts.discard(
                (
                    context.request_id,
                    context.execution_attempt_id,
                    context.lease_epoch,
                )
            )
            if not any(item[0] == context.request_id for item in _active_heartbeat_attempts):
                _active_heartbeat_request_ids.discard(context.request_id)


def _run_heartbeat_loop_body(context: _HeartbeatLoopContext) -> None:
    consecutive_failures = 0
    while not context.stop_event.wait(context.interval):
        ok, consecutive_failures = _refresh_or_count_failure(
            context.request_path,
            context.request_id,
            context.worker_id,
            context.execution_attempt_id,
            context.lease_epoch,
            consecutive_failures,
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
    execution_attempt_id: str,
    lease_epoch: int,
    consecutive_failures: int,
) -> tuple[bool, int]:
    try:
        report = refresh_processing_lease_report(
            request_path,
            request_id=request_id,
            worker_id=worker_id,
            execution_attempt_id=execution_attempt_id,
            lease_epoch=lease_epoch,
        )
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
