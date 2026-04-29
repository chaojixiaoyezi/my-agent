from __future__ import annotations

"""LLM: detects, requeues, fails, and archives stale gateway processing requests.

给人看的解释：
gateway 如果崩在半路，请求会留在 processing 目录。
这个文件专门处理这种“卡住的请求”：能重试就退回 pending，重试太多就写失败响应并归档。
"""

import time
from pathlib import Path
from typing import TYPE_CHECKING

from .io import append_gateway_history, gateway_response_path, read_json_file, write_json_file
from .logging import log_gateway_payload, _report_gateway_side_effect_error
from .paths import GatewayPaths

if TYPE_CHECKING:
    from ..core import SimpleAgent


def gateway_stale_processing(paths: GatewayPaths, timeout_seconds: int) -> list[dict]:
    """LLM contract: list processing requests older than the configured lease timeout.

    Human version:
    这给 local-doctor 用。它会找出那些在 `processing` 里待太久的请求，帮助我们判断 gateway 是不是卡住了。
    """

    items: list[dict] = []
    now = time.time()
    timeout_seconds = max(1, int(timeout_seconds or 1))
    for path in sorted(paths.processing.glob("*.json")):
        payload = read_json_file(path)
        request_id = str(payload.get("id") or path.stem)
        if gateway_response_path(paths, request_id).exists():
            continue
        lease_at = _gateway_processing_lease_at(payload, path)
        age = now - lease_at if lease_at else 0
        if lease_at and age < timeout_seconds:
            continue
        items.append(
            {
                "request_id": request_id,
                "path": str(path),
                "age_seconds": round(age, 1) if lease_at else 0,
                "attempts": _gateway_request_attempts(payload),
                "lease_owner": str(payload.get("lease_owner") or ""),
                "lease_heartbeat_at": payload.get("lease_heartbeat_at", 0),
                "lease_started_at": payload.get("lease_started_at", 0),
            }
        )
    return items


def recover_gateway_processing_requests(
    paths: GatewayPaths,
    *,
    max_attempts: int = 2,
    timeout_seconds: int = 900,
    startup: bool = False,
    agent: SimpleAgent | None = None,
) -> dict[str, int]:
    """LLM contract: requeue or fail stale gateway processing requests.

    Human version:
    如果 gateway 崩在半路，请求会留在 `processing`。启动时我们把旧请求退回 pending；
    运行中只处理超过超时时间的请求。重试次数太多的请求会归档到 failed，并写失败响应。
    """

    paths.inbox.mkdir(parents=True, exist_ok=True)
    paths.processing.mkdir(parents=True, exist_ok=True)
    paths.done.mkdir(parents=True, exist_ok=True)
    paths.failed.mkdir(parents=True, exist_ok=True)
    paths.responses.mkdir(parents=True, exist_ok=True)
    summary = {"requeued": 0, "failed": 0, "checked": 0, "archived": 0}
    now = time.time()
    max_attempts = max(1, int(max_attempts or 1))
    timeout_seconds = max(1, int(timeout_seconds or 1))
    for request_path in sorted(paths.processing.glob("*.json")):
        payload = read_json_file(request_path)
        if not payload:
            payload = {"id": request_path.stem, "kind": "unknown", "created_at": 0}
        summary["checked"] += 1
        request_id = str(payload.get("id") or request_path.stem)
        if gateway_response_path(paths, request_id).exists():
            try:
                _archive_gateway_request(request_path, paths.done)
            except OSError as exc:
                _report_gateway_side_effect_error("archive_duplicate_gateway_processing_request", request_id, exc)
                continue
            summary["archived"] += 1
            continue
        lease_at = _gateway_processing_lease_at(payload, request_path)
        stale = startup or not lease_at or now - lease_at >= timeout_seconds
        if not stale:
            continue
        attempts = _gateway_request_attempts(payload)
        if attempts >= max_attempts:
            _write_gateway_failure_response(
                paths,
                request_path,
                payload,
                status="failed",
                error_code="GATEWAY_PROCESSING_TIMEOUT",
                error=f"gateway processing timeout after {timeout_seconds}s; attempts={attempts}",
                event_type="gateway_request_processing_failed",
                agent=agent,
            )
            try:
                _archive_gateway_request(request_path, paths.failed)
            except OSError as exc:
                _report_gateway_side_effect_error("archive_gateway_failed_request", request_path.stem, exc)
                continue
            summary["failed"] += 1
            continue
        payload.update(
            {
                "status": "pending",
                "requeued_at": now,
                "last_error": (
                    "gateway restarted before request completed"
                    if startup
                    else f"gateway processing timeout after {timeout_seconds}s"
                ),
            }
        )
        try:
            write_json_file(request_path, payload)
            request_path.replace(paths.inbox / request_path.name)
        except OSError as exc:
            _report_gateway_side_effect_error("requeue_gateway_request", request_path.stem, exc)
            continue
        summary["requeued"] += 1
    return summary


def requeue_gateway_processing_requests(paths: GatewayPaths) -> int:
    """LLM contract: compatibility wrapper for startup processing recovery.

    Human version:
    老测试和场景里还会直接调用这个名字。它现在只是恢复逻辑的一个简短入口。
    """

    return recover_gateway_processing_requests(paths, startup=True)["requeued"]


def _gateway_request_attempts(payload: dict) -> int:
    """LLM contract: parse request attempt count safely.

    Human version:
    文件里的 attempts 可能缺失或是奇怪类型。这里统一转成整数，转不了就按 0 次处理。
    """

    try:
        return int(payload.get("attempts", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _gateway_processing_started_at(payload: dict, request_path: Path) -> float:
    """LLM contract: derive processing start timestamp from payload or file mtime.

    Human version:
    有些旧请求没有 lease 字段，那就按 started/updated/created 或文件修改时间兜底。
    """

    return _gateway_processing_timestamp(
        payload,
        request_path,
        ("lease_started_at", "started_at", "updated_at", "created_at"),
    )


def _gateway_processing_lease_at(payload: dict, request_path: Path) -> float:
    """Return the freshness timestamp used for stale processing recovery."""

    return _gateway_processing_timestamp(
        payload,
        request_path,
        ("lease_heartbeat_at", "lease_started_at", "started_at", "updated_at", "created_at"),
    )


def _gateway_processing_timestamp(payload: dict, request_path: Path, keys: tuple[str, ...]) -> float:
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


def _write_gateway_failure_response(
    paths: GatewayPaths,
    request_path: Path,
    payload: dict,
    *,
    status: str,
    error_code: str,
    error: str,
    event_type: str,
    agent: SimpleAgent | None = None,
) -> dict:
    """LLM contract: write a terminal failure response for a queued request.

    Human version:
    当请求重试太多或 processing 超时时，不能只把文件扔进 failed。还要写一个响应 JSON，
    让客户端可以通过 request_id 查到明确失败原因。
    """

    request_id = str(payload.get("id") or request_path.stem)
    now = time.time()
    started_at = _gateway_processing_started_at(payload, request_path) or now
    response = {
        "id": request_id,
        "kind": str(payload.get("kind") or "unknown"),
        "ok": False,
        "status": status,
        "created_at": payload.get("created_at", 0),
        "started_at": started_at,
        "ended_at": now,
        "duration_seconds": round(now - started_at, 3),
        "response": "",
        "error_code": error_code,
        "error": error,
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(request_path),
        "attempts": _gateway_request_attempts(payload),
    }
    response_path = gateway_response_path(paths, request_id)
    if not response_path.exists():
        write_json_file(response_path, response)
    append_gateway_history(paths, response)
    if agent:
        log_gateway_payload(
            agent,
            {**response, "prompt": payload.get("prompt", "")},
            event_type=event_type,
            request_path=request_path,
            response_path=response_path,
        )
    return response


def _archive_gateway_request(path: Path, target_dir: Path) -> Path:
    """LLM contract: move a request file into a terminal archive directory.

    Human version:
    done/failed 目录里可能已经有同名文件，所以必要时会给文件名加时间戳，避免覆盖旧证据。
    """

    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    if target.exists():
        target = target_dir / f"{path.stem}-{int(time.time())}{path.suffix}"
    path.replace(target)
    return target
