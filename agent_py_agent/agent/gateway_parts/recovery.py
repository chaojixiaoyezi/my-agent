
from __future__ import annotations

"""detects, requeues, fails, and archives stale gateway processing requests.

gateway 如果崩在半路，请求会留在 processing 目录。
这个文件专门处理这种'卡住的请求'：能重试就退回 pending，重试太多就写失败响应并归档。
processing liveness 直接读取 lease_service，不再经过 runtime 聚合层。
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .io import (
    append_gateway_history,
    gateway_response_path,
    read_json_file_report,
    write_json_file_atomic,
)
from .logging import _report_gateway_side_effect_error, log_gateway_payload
from .paths import GatewayPaths

if TYPE_CHECKING:
    from ..core import SimpleAgent


@dataclass(frozen=True)
class _RecoveryContext:
    now: float
    max_attempts: int
    timeout_seconds: int
    startup: bool
    agent: SimpleAgent | None
    lease_stale_seconds: int | None


@dataclass(frozen=True)
class GatewayProcessingRecoveryReport:
    summary: dict[str, int]
    load_errors: list[dict]


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
        items.append(_stale_processing_item(path, payload, age, payload_report.load_error))
    return items


def _stale_processing_item(path: Path, payload: dict, age: float, load_error: dict | None) -> dict:
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


def recover_gateway_processing_requests(
    paths: GatewayPaths,
    *,
    params: _RecoveryContext | None = None,
    max_attempts: int = 2,
    timeout_seconds: int = 900,
    startup: bool = False,
    agent: SimpleAgent | None = None,
    lease_stale_seconds: int | None = None,
) -> dict[str, int]:
    return recover_gateway_processing_requests_report(
        paths,
        params=params,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        startup=startup,
        agent=agent,
        lease_stale_seconds=lease_stale_seconds,
    ).summary


def recover_gateway_processing_requests_report(
    paths: GatewayPaths,
    *,
    params: _RecoveryContext | None = None,
    max_attempts: int = 2,
    timeout_seconds: int = 900,
    startup: bool = False,
    agent: SimpleAgent | None = None,
    lease_stale_seconds: int | None = None,
) -> GatewayProcessingRecoveryReport:

    _ensure_recovery_dirs(paths)
    summary = {"requeued": 0, "failed": 0, "checked": 0, "archived": 0}
    load_errors: list[dict] = []
    now = time.time()
    max_attempts = _non_negative_int(max_attempts, default=1)
    timeout_seconds = max(1, int(timeout_seconds or 1))
    context = params or _RecoveryContext(now, max_attempts, timeout_seconds, startup, agent, lease_stale_seconds)
    for request_path in sorted(paths.processing.glob("*.json")):
        summary["checked"] += 1
        payload_report = read_json_file_report(request_path, context="gateway.recovery.processing.read")
        if payload_report.load_error is not None:
            load_errors.append(payload_report.load_error)
        action = _recover_one_processing_request(paths, request_path, context, payload_report)
        if action in summary:
            summary[action] += 1
    return GatewayProcessingRecoveryReport(summary, load_errors)


def _ensure_recovery_dirs(paths: GatewayPaths) -> None:
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        folder.mkdir(parents=True, exist_ok=True)


def _recover_one_processing_request(
    paths: GatewayPaths,
    request_path: Path,
    context: _RecoveryContext,
    payload_report,
) -> str:
    if payload_report.load_error is not None:
        return _fail_unreadable_processing(paths, request_path, payload_report.load_error, context)
    payload = payload_report.payload or {"id": request_path.stem, "kind": "unknown", "created_at": 0}
    request_id = str(payload.get("id") or request_path.stem)
    if gateway_response_path(paths, request_id).exists():
        return _archive_completed_processing(paths, request_path, request_id)
    if not _processing_request_stale(payload, request_path, context):
        return ""
    attempts = _gateway_request_attempts(payload)
    if context.max_attempts > 0 and attempts >= context.max_attempts:
        return _fail_stale_processing(
            {
                "paths": paths,
                "request_path": request_path,
                "payload": payload,
                "timeout_seconds": context.timeout_seconds,
                "attempts": attempts,
                "agent": context.agent,
            }
        )
    return _requeue_stale_processing(paths, request_path, payload, context)


def _fail_unreadable_processing(
    paths: GatewayPaths,
    request_path: Path,
    load_error: dict,
    context: _RecoveryContext,
) -> str:
    payload = {"id": request_path.stem, "kind": "unknown", "created_at": 0, "attempts": 0}
    _write_gateway_failure_response(
        paths,
        {
            "request_path": request_path,
            "payload": payload,
            "status": "failed",
            "error_code": "GATEWAY_REQUEST_LOAD_ERROR",
            "error": str(load_error.get("message") or "gateway processing request file could not be read"),
            "event_type": "gateway_request_processing_failed",
            "agent": context.agent,
            "request_load_error": load_error,
        },
    )
    try:
        _archive_gateway_request(request_path, paths.failed)
    except OSError as exc:
        _report_gateway_side_effect_error("archive_gateway_unreadable_request", request_path.stem, exc)
        return ""
    return "failed"


def _non_negative_int(value: object, *, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _archive_completed_processing(paths: GatewayPaths, request_path: Path, request_id: str) -> str:
    try:
        _archive_gateway_request(request_path, paths.done)
    except OSError as exc:
        _report_gateway_side_effect_error("archive_duplicate_gateway_processing_request", request_id, exc)
        return ""
    return "archived"


def _processing_request_stale(
    payload: dict,
    request_path: Path,
    context: _RecoveryContext,
) -> bool:
    from .lease_service import is_heartbeat_alive_for_request

    request_id = str(payload.get("id") or request_path.stem)
    lease_at = gateway_processing_lease_at(payload, request_path)
    effective_timeout = context.lease_stale_seconds if context.lease_stale_seconds is not None else context.timeout_seconds
    return (
        context.startup
        or not lease_at
        or (context.now - lease_at >= effective_timeout and not is_heartbeat_alive_for_request(request_id))
    )


def _fail_stale_processing(context: dict) -> str:
    paths = context["paths"]
    request_path = context["request_path"]
    _write_gateway_failure_response(
        paths,
        {
            "request_path": request_path,
            "payload": context["payload"],
            "status": "failed",
            "error_code": "GATEWAY_PROCESSING_TIMEOUT",
            "error": f"gateway processing timeout after {context['timeout_seconds']}s; attempts={context['attempts']}",
            "event_type": "gateway_request_processing_failed",
            "agent": context["agent"],
        },
    )
    try:
        _archive_gateway_request(request_path, paths.failed)
    except OSError as exc:
        _report_gateway_side_effect_error("archive_gateway_failed_request", request_path.stem, exc)
        return ""
    return "failed"


def _requeue_stale_processing(
    paths: GatewayPaths,
    request_path: Path,
    payload: dict,
    context: _RecoveryContext,
) -> str:
    if context.startup:
        payload["not_before_at"] = context.now + 10
    payload.update(
        {
            "status": "pending",
            "priority": "recovery",
            "source": str(payload.get("source") or "gateway_recovery"),
            "requeued_at": context.now,
            "last_error": (
                "gateway restarted before request completed"
                if context.startup
                else f"gateway processing timeout after {context.timeout_seconds}s"
            ),
        }
    )
    try:
        write_json_file_atomic(request_path, payload)
        request_path.replace(paths.inbox / request_path.name)
    except OSError as exc:
        _report_gateway_side_effect_error("requeue_gateway_request", request_path.stem, exc)
        return ""
    return "requeued"


def requeue_gateway_processing_requests(paths: GatewayPaths) -> int:

    return recover_gateway_processing_requests(paths, startup=True)["requeued"]


def _gateway_request_attempts(payload: dict) -> int:
    return gateway_request_attempts(payload)


def _gateway_processing_started_at(payload: dict, request_path: Path) -> float:
    return gateway_processing_started_at(payload, request_path)


def _write_gateway_failure_response(
    paths: GatewayPaths,
    context: dict,
) -> dict:

    request_path = context["request_path"]
    payload = context["payload"]
    request_id = str(payload.get("id") or request_path.stem)
    now = time.time()
    started_at = _gateway_processing_started_at(payload, request_path) or now
    response = {
        "id": request_id,
        "kind": str(payload.get("kind") or "unknown"),
        "ok": False,
        "status": context["status"],
        "created_at": payload.get("created_at", 0),
        "started_at": started_at,
        "ended_at": now,
        "duration_seconds": round(now - started_at, 3),
        "response": "",
        "error_code": context["error_code"],
        "error": context["error"],
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(request_path),
        "attempts": _gateway_request_attempts(payload),
    }
    if context.get("request_load_error") is not None:
        response["request_load_error"] = context["request_load_error"]
    response_path = gateway_response_path(paths, request_id)
    if not response_path.exists():
        write_json_file_atomic(response_path, response)
    append_gateway_history(paths, response)
    agent = context.get("agent")
    if agent:
        log_gateway_payload(
            agent,
            {**response, "prompt": payload.get("prompt", "")},
            event_type=context["event_type"],
            request_path=request_path,
            response_path=response_path,
        )
    return response


def _archive_gateway_request(path: Path, target_dir: Path) -> Path:

    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    if target.exists():
        target = target_dir / f"{path.stem}-{int(time.time())}{path.suffix}"
    path.replace(target)
    return target
