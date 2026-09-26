# LLM: Reconcile exact request leases and canonical terminal receipts; dispatch generations
# never imply task failure. Startup resume preserves effects/history, expired processing leases
# consume their own persisted budget. Terminal commit releases exact request-affine claims; failed
# cleanup stays retryable without re-execution. Check heartbeat/terminal/owner tests on changes.
# 中断展示事件直接使用 paths/stream_writer，不通过请求执行器反向导入，也不改变终态写账顺序。
# 模块用途: 恢复 Gateway 中断的请求，分清服务重启和真正卡死，并保证同一回合只有一个终态和执行者。
from __future__ import annotations

"""detects, requeues, fails, and archives stale gateway processing requests.

gateway 如果崩在半路，请求会留在 processing 目录。
服务重启按原身份退回 pending；真实 processing 租约失效才累计失败，上限用尽写失败响应并归档。
processing liveness 直接读取 lease_service，不再经过 runtime 聚合层。
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..runtime_errors import DataCorruptionError, runtime_error_report
from .io import (
    GATEWAY_REQUEST_FINGERPRINT_SCHEMA,
    append_gateway_history_once,
    gateway_request_fingerprint,
    gateway_response_path,
    gateway_turn_transition,
    read_json_file_report,
    validated_gateway_request_fingerprint,
    write_json_file_atomic,
)
from .logging import _report_gateway_side_effect_error, log_gateway_payload
from .paths import GatewayPaths, gateway_chunk_path

if TYPE_CHECKING:
    from ..core import SimpleAgent


_TERMINAL_PROJECTION_MARKER_SCHEMA = "gateway_terminal_projection_complete.v1"
_ACTIVE_TURN_RECOVERY_SCHEMA = "gateway_active_turn_recovery.v1"
GATEWAY_SAFE_RESTART_CAUSE = "gateway_safe_restart"


@dataclass(frozen=True)
class _RecoveryContext:
    now: float
    max_attempts: int
    timeout_seconds: int
    startup: bool
    agent: SimpleAgent | None
    lease_stale_seconds: int | None
    # 安全重启排空后的接班启动：旧进程已确认退出，续跑不需要防崩溃循环的等待延迟。
    planned_restart: bool = False


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
        lease_at = gateway_processing_lease_at(payload, path)
        age = now - lease_at if lease_at else 0
        if lease_at and age < timeout_seconds:
            continue
        items.append(_stale_processing_item(path, payload, age, payload_report.load_error))
    return items


def _stale_processing_item(path: Path, payload: dict, age: float, load_error: dict | None) -> dict:
    item = {
        "request_id": path.stem,
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
    planned_restart: bool = False,
) -> dict[str, int]:
    return recover_gateway_processing_requests_report(
        paths,
        params=params,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        startup=startup,
        agent=agent,
        lease_stale_seconds=lease_stale_seconds,
        planned_restart=planned_restart,
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
    planned_restart: bool = False,
) -> GatewayProcessingRecoveryReport:

    _ensure_recovery_dirs(paths)
    summary = {
        "requeued": 0,
        "failed": 0,
        "checked": 0,
        "archived": 0,
        "projected": 0,
    }
    load_errors: list[dict] = []
    now = time.time()
    max_attempts = _non_negative_int(max_attempts, default=1)
    timeout_seconds = max(1, int(timeout_seconds or 1))
    context = params or _RecoveryContext(
        now, max_attempts, timeout_seconds, startup, agent, lease_stale_seconds,
        planned_restart=bool(startup and planned_restart),
    )
    for request_path in sorted(paths.processing.glob("*.json")):
        summary["checked"] += 1
        payload_report = read_json_file_report(request_path, context="gateway.recovery.processing.read")
        if payload_report.load_error is not None:
            load_errors.append(payload_report.load_error)
        try:
            action = _recover_one_processing_request(paths, request_path, context, payload_report)
        except Exception as exc:
            report = runtime_error_report(exc, context="gateway.recovery.request")
            report["path"] = str(request_path)
            load_errors.append(report)
            continue
        if action in summary:
            summary[action] += 1
    return GatewayProcessingRecoveryReport(summary, load_errors)


def _ensure_recovery_dirs(paths: GatewayPaths) -> None:
    for folder in (
        paths.inbox,
        paths.processing,
        paths.done,
        paths.failed,
        paths.terminal,
        paths.responses,
    ):
        folder.mkdir(parents=True, exist_ok=True)


# LLM: Dispatch attempts fence executions but are not failure counts. Startup restores the
# same turn; only expired live-service processing leases consume the recovery failure budget.
# Keep terminal/CAS/unknown-operation fences unchanged and never derive cause from last_error prose.
# 函数用途: 区分服务重启续接和真正卡死，避免一个健康长任务因部署多次就被错误终止。
def _recover_one_processing_request(
    paths: GatewayPaths,
    request_path: Path,
    context: _RecoveryContext,
    payload_report,
) -> str:
    if payload_report.load_error is not None:
        return _fail_unreadable_processing(paths, request_path, payload_report.load_error, context)
    payload = payload_report.payload or {"id": request_path.stem, "kind": "unknown", "created_at": 0}
    request_id = request_path.stem
    declared_id = str(payload.get("id") or "").strip()
    if declared_id != request_id:
        raise DataCorruptionError(
            f"gateway processing filename and payload id conflict: {request_id}"
        )
    if (
        (paths.terminal / f"{request_id}.json").exists()
        or payload.get("schema_version") == "gateway_terminal_request.v1"
    ):
        return _recover_committed_terminal_processing(
            paths,
            request_path,
            request_id,
            payload=payload,
            agent=context.agent,
        )
    if not _processing_request_stale(payload, request_path, context):
        return ""
    failures = _gateway_processing_failure_count(payload) + (0 if context.startup else 1)
    if not context.startup and context.max_attempts > 0 and failures >= context.max_attempts:
        return _fail_stale_processing(
            {
                "paths": paths,
                "request_path": request_path,
                "payload": payload,
                "timeout_seconds": context.lease_stale_seconds or context.timeout_seconds,
                "attempts": _gateway_request_attempts(payload),
                "processing_failure_count": failures,
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
    failure_context = {
        "request_path": request_path,
        "payload": payload,
        "status": "failed",
        "error_code": "GATEWAY_REQUEST_LOAD_ERROR",
        "error": str(load_error.get("message") or "gateway processing request file could not be read"),
        "event_type": "gateway_request_processing_failed",
        "agent": context.agent,
        "request_load_error": load_error,
    }
    response = _build_gateway_failure_response(failure_context)
    try:
        terminalize_gateway_request_file(
            paths,
            request_path,
            paths.failed,
            request_path.stem,
            terminal_response=response,
        )
    except OSError as exc:
        _report_gateway_side_effect_error("archive_gateway_unreadable_request", request_path.stem, exc)
        return ""
    _publish_gateway_failure_response(paths, failure_context, response)
    return "failed"


def _non_negative_int(value: object, *, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


# LLM: 插话持久操作经 guidance 领域组件； A canonical archive always wins over a leftover hot record. Legacy sealed processing rows
# are committed without re-running the provider. Exact request-affine claim cleanup precedes
# the same canonical commit, so a cleanup I/O failure remains a commit retry, not a model retry.
# 函数用途: 补交已封口的答复和执行权释放，不重跑模型；随后修复用户可见投影。
def _recover_committed_terminal_processing(
    paths: GatewayPaths,
    request_path: Path,
    request_id: str,
    *,
    payload: dict,
    agent: SimpleAgent | None,
) -> str:
    conversation_store = None
    with gateway_turn_transition(paths, request_id):
        canonical = paths.terminal / f"{request_id}.json"
        canonical_committed = canonical.exists()
        if canonical_committed:
            report = read_json_file_report(
                canonical,
                context="gateway.recovery.terminal.read",
            )
            if report.load_error is not None or not report.payload:
                raise DataCorruptionError(
                    f"canonical gateway terminal archive is unreadable: {request_id}"
                )
            terminal_payload = report.payload
        else:
            terminal_payload = dict(payload)
        terminal_response = terminal_payload.get("terminal_response")
        if (
            terminal_payload.get("schema_version") != "gateway_terminal_request.v1"
            or str(terminal_payload.get("id") or "").strip() != request_id
            or not isinstance(terminal_response, dict)
            or not terminal_response
        ):
            raise DataCorruptionError(
                f"gateway terminal archive is incomplete: {request_id}"
            )
        conversation_store = _recovery_conversation_store(agent, terminal_payload)
        if conversation_store is not None:
            try:
                mailbox_summary = conversation_store.guidance.recovery.reject_pending(
                    request_id,
                    reject_reserved=True,
                )
                if mailbox_summary.get("errors") and not canonical_committed:
                    terminal_response["input_settlement_errors"] = int(
                        mailbox_summary.get("errors") or 0
                    )
                    terminal_payload = _terminal_gateway_request_payload(
                        request_id,
                        terminal_payload,
                        terminal_response,
                    )
            except Exception as exc:
                if canonical_committed:
                    _report_gateway_side_effect_error(
                        "gateway_recovery_guidance_settlement",
                        request_id,
                        exc,
                    )
                else:
                    terminal_response["input_settlement_error"] = runtime_error_report(
                        exc,
                        context="gateway.recovery.guidance_settlement",
                    )
                    terminal_payload = _terminal_gateway_request_payload(
                        request_id,
                        terminal_payload,
                        terminal_response,
                    )
        if not canonical_committed:
            terminal_payload = _terminal_gateway_request_payload(
                request_id,
                terminal_payload,
                terminal_response,
            )
        target = gateway_terminal_projection_folder(paths, terminal_response)
        _finish_gateway_conversation_claim(terminal_payload, request_id, conversation_store)
        _commit_gateway_terminal_request(
            paths,
            request_path,
            target,
            request_id,
            terminal_payload,
        )

    from .input_delivery_service import settle_gateway_inputs_for_turn

    settle_gateway_inputs_for_turn(
        paths,
        target_turn_id=request_id,
        conversation_store=conversation_store,
    )
    repair_gateway_chunk_projection(paths, request_id, terminal_response)
    write_json_file_atomic(gateway_response_path(paths, request_id), terminal_response)
    append_gateway_history_once(paths, terminal_response)
    mark_gateway_terminal_projection_complete(paths, request_id, terminal_response)
    return "archived"


# LLM: The canonical terminal archive remains the sole completion authority. This marker is only
# a rebuildable acknowledgement that every derived projection was checked once for the exact
# canonical file version; changing or deleting a projection invalidates the fast path.
# 函数用途: 记录某个终态请求的展示目录和 response 已完整落盘，避免后台永久重复修复。
def mark_gateway_terminal_projection_complete(
    paths: GatewayPaths,
    request_id: str,
    terminal_response: dict,
) -> Path:
    selected = str(request_id or "").strip()
    if not selected:
        raise ValueError("gateway terminal projection marker requires request_id")
    canonical = paths.terminal / f"{selected}.json"
    canonical_report = read_json_file_report(
        canonical,
        context="gateway.recovery.terminal_marker.canonical",
    )
    if canonical_report.load_error is not None or not canonical_report.payload:
        raise DataCorruptionError(
            f"canonical gateway terminal archive is unreadable: {selected}"
        )
    if canonical_report.payload.get("terminal_response") != terminal_response:
        raise DataCorruptionError(
            f"gateway terminal response conflicts with canonical archive: {selected}"
        )
    target = gateway_terminal_projection_folder(paths, terminal_response)
    projection = target / canonical.name
    projection_report = read_json_file_report(
        projection,
        context="gateway.recovery.terminal_marker.projection",
    )
    if (
        projection_report.load_error is not None
        or not projection_report.payload
        or not _same_gateway_terminal_outcome(
            canonical_report.payload,
            projection_report.payload,
        )
    ):
        raise DataCorruptionError(
            f"gateway terminal outcome projection is incomplete: {selected}"
        )
    response_path = gateway_response_path(paths, selected)
    response_report = read_json_file_report(
        response_path,
        context="gateway.recovery.terminal_marker.response",
    )
    if response_report.load_error is not None or response_report.payload != terminal_response:
        raise DataCorruptionError(
            f"gateway terminal response projection is incomplete: {selected}"
        )
    marker = _terminal_projection_marker_path(paths, selected)
    write_json_file_atomic(
        marker,
        {
            "schema_version": _TERMINAL_PROJECTION_MARKER_SCHEMA,
            "request_id": selected,
            "projection_folder": target.name,
            "canonical_stamp": _terminal_projection_file_stamp(canonical),
            "projection_stamp": _terminal_projection_file_stamp(projection),
            "response_stamp": _terminal_projection_file_stamp(response_path),
            "updated_at": time.time(),
        },
    )
    return marker


# LLM: A marker may skip expensive conversation/history reconciliation only while all three exact
# files still match the versions observed after the successful projection transaction.
# 函数用途: 快速判断终态投影是否仍完整；任何损坏、缺失或版本变化都回到正常修复路径。
def _terminal_projection_marker_matches(paths: GatewayPaths, canonical: Path) -> bool:
    marker_report = read_json_file_report(
        _terminal_projection_marker_path(paths, canonical.stem),
        context="gateway.recovery.terminal_marker.read",
    )
    marker = marker_report.payload
    if marker_report.load_error is not None or not marker:
        return False
    folder_name = str(marker.get("projection_folder") or "").strip()
    target = (
        paths.done
        if folder_name == paths.done.name
        else paths.failed
        if folder_name == paths.failed.name
        else None
    )
    if target is None:
        return False
    projection = target / canonical.name
    response_path = gateway_response_path(paths, canonical.stem)
    return bool(
        marker.get("schema_version") == _TERMINAL_PROJECTION_MARKER_SCHEMA
        and str(marker.get("request_id") or "").strip() == canonical.stem
        and marker.get("canonical_stamp")
        == _terminal_projection_file_stamp(canonical)
        and marker.get("projection_stamp")
        == _terminal_projection_file_stamp(projection)
        and marker.get("response_stamp")
        == _terminal_projection_file_stamp(response_path)
    )


# LLM: File stamps are projection freshness hints, never task-completion authority. Missing files
# use an explicit zero stamp so marker validation fails closed without raising in the hot loop.
# 函数用途: 返回投影文件的大小与纳秒修改时间，用于低成本发现丢失或外部改写。
def _terminal_projection_file_stamp(path: Path) -> dict[str, int]:
    try:
        stat = path.stat()
    except OSError:
        return {"size": 0, "mtime_ns": 0}
    return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


# LLM: Marker filenames reuse the already validated canonical request filename and live only under
# projection_state; they are indexes and may be deleted to force a complete repair pass.
# 函数用途: 返回单个终态请求的可重建投影完成标记路径。
def _terminal_projection_marker_path(paths: GatewayPaths, request_id: str) -> Path:
    return paths.root / "projection_state" / "terminal_complete" / f"{request_id}.json"


# LLM: The live dispatcher walks canonical terminal authorities in bounded rotating pages. Repair
# never delays Gateway readiness, and one corrupt archive is reported without blocking others.
# 函数用途: 后台分批补齐终态请求的完成目录、response、history 和输入回执投影。
def repair_gateway_terminal_projections(
    paths: GatewayPaths,
    *,
    agent: SimpleAgent | None,
    limit: int = 16,
) -> tuple[int, list[dict]]:
    projected = 0
    errors: list[dict] = []
    candidates = sorted(paths.terminal.glob("*.json"), key=lambda item: item.name)
    if not candidates:
        return projected, errors
    cursor_path = paths.root / "projection_state" / "terminal_cursor.json"
    cursor_report = read_json_file_report(
        cursor_path,
        context="gateway.recovery.terminal_projector.cursor",
    )
    previous = (
        str(cursor_report.payload.get("last_name") or "")
        if cursor_report.load_error is None
        else ""
    )
    start = next(
        (index for index, path in enumerate(candidates) if path.name > previous),
        0,
    )
    ordered = candidates[start:] + candidates[:start]
    selected = ordered[: max(1, int(limit or 1))]
    for canonical in selected:
        if _terminal_projection_marker_matches(paths, canonical):
            continue
        report = read_json_file_report(
            canonical,
            context="gateway.recovery.terminal_projector.read",
        )
        request_id = str(report.payload.get("id") or canonical.stem).strip()
        if report.load_error is not None or not report.payload or request_id != canonical.stem:
            error = DataCorruptionError(
                f"canonical gateway terminal archive is invalid: {canonical.stem}"
            )
            failure = runtime_error_report(
                error,
                context="gateway.recovery.terminal_projector",
            )
            failure["path"] = str(canonical)
            errors.append(failure)
            continue
        try:
            _recover_committed_terminal_processing(
                paths,
                canonical,
                request_id,
                payload=report.payload,
                agent=agent,
            )
        except Exception as exc:
            failure = runtime_error_report(
                exc,
                context="gateway.recovery.terminal_projector",
            )
            failure["path"] = str(canonical)
            errors.append(failure)
            continue
        projected += 1
    write_json_file_atomic(
        cursor_path,
        {
            "schema_version": "gateway_terminal_projection_cursor.v1",
            "last_name": selected[-1].name,
            "updated_at": time.time(),
        },
    )
    return projected, errors


# LLM: All normal and recovery terminal paths use the same typed outcome-to-projection rule. A
# response cannot land in done merely because a projection file happened to exist first.
# 函数用途: 根据最终答复的 ok 事实选择完成或失败展示目录。
def gateway_terminal_projection_folder(paths: GatewayPaths, response: dict) -> Path:
    return paths.done if bool(response.get("ok")) else paths.failed


def _processing_request_stale(
    payload: dict,
    request_path: Path,
    context: _RecoveryContext,
) -> bool:
    from .lease_service import is_heartbeat_alive_for_request

    request_id = request_path.stem
    lease_at = gateway_processing_lease_at(payload, request_path)
    effective_timeout = context.lease_stale_seconds if context.lease_stale_seconds is not None else context.timeout_seconds
    from .daemon_metadata import process_identity_is_live

    process_identity = payload.get("lease_process_identity")
    process_live = process_identity_is_live(process_identity)
    if context.startup and process_live is True:
        return False
    if context.startup and process_live is False:
        return True
    if context.startup and not isinstance(process_identity, dict):
        return True
    return (
        not lease_at
        or (
            context.now - lease_at >= effective_timeout
            and not is_heartbeat_alive_for_request(
                request_id,
                execution_attempt_id=str(
                    payload.get("execution_attempt_id") or ""
                ).strip(),
                lease_epoch=_gateway_lease_epoch(payload),
            )
        )
    )


# LLM: Recovery decisions made before T are only hints. The winner must re-read the hot record
# under T and compare the immutable attempt, epoch, and last observed heartbeat before mutation.
# 函数用途: 在回合锁内确认过期快照仍指向同一个未恢复执行，避免误收口新 worker。
def _fresh_matching_stale_payload(
    request_path: Path,
    snapshot: dict,
    context: _RecoveryContext,
) -> dict | None:
    report = read_json_file_report(
        request_path,
        context="gateway.recovery.processing.recheck",
    )
    if report.load_error is not None:
        raise DataCorruptionError(
            f"gateway processing request became unreadable: {request_path.stem}"
        )
    fresh = report.payload
    if (
        not fresh
        or str(fresh.get("id") or "").strip() != request_path.stem
        or str(snapshot.get("id") or "").strip() != request_path.stem
    ):
        return None
    expected_attempt = str(snapshot.get("execution_attempt_id") or "").strip()
    if expected_attempt and str(fresh.get("execution_attempt_id") or "").strip() != expected_attempt:
        return None
    expected_epoch = _gateway_lease_epoch(snapshot)
    if expected_epoch and _gateway_lease_epoch(fresh) != expected_epoch:
        return None
    if _gateway_lease_heartbeat(snapshot) != _gateway_lease_heartbeat(fresh):
        return None
    if str(fresh.get("status") or "") != "processing":
        return None
    return fresh if _processing_request_stale(fresh, request_path, context) else None


# LLM: Lease epoch is the monotonic half of the execution fence and malformed persisted values
# must not accidentally match a live attempt.
# 函数用途: 从请求记录读取非负租约代次，坏值统一视为零。
def _gateway_lease_epoch(payload: dict) -> int:
    try:
        return max(0, int(payload.get("lease_epoch") or 0))
    except (TypeError, ValueError):
        return 0


# LLM: Recovery compares the exact heartbeat value it used for the stale decision. A concurrent
# heartbeat changes this value before the recovery winner may mutate the file.
# 函数用途: 读取用于过期裁决的心跳时间，坏值统一视为零。
def _gateway_lease_heartbeat(payload: dict) -> float:
    try:
        return float(payload.get("lease_heartbeat_at") or 0)
    except (TypeError, ValueError):
        return 0.0


# LLM: This is only an exhausted processing-lease failure budget, never startup recovery.
# The terminal response records failures separately from dispatch attempts; exact lease CAS
# remains the authority before any archive or visible response is written.
# 函数用途: 真正卡死次数用尽时提交失败及准确诊断，不把进程恢复代次说成任务超时次数。
def _fail_stale_processing(context: dict) -> str:
    paths = context["paths"]
    request_path = context["request_path"]
    failure_context = {
        "request_path": request_path,
        "payload": context["payload"],
        "status": "failed",
        "error_code": "GATEWAY_PROCESSING_TIMEOUT",
        "error": (
            f"gateway processing lease expired; threshold={context['timeout_seconds']}s; "
            f"failures={context['processing_failure_count']}; attempts={context['attempts']}"
        ),
        "event_type": "gateway_request_processing_failed",
        "agent": context["agent"],
    }
    response = _build_gateway_failure_response(failure_context)
    response["processing_failure_count"] = context["processing_failure_count"]
    request_id = request_path.stem
    conversation_store = _recovery_conversation_store(
        context.get("agent"),
        context["payload"],
    )
    try:
        terminalize_gateway_request_file(
            paths,
            request_path,
            paths.failed,
            request_id,
            conversation_store=conversation_store,
            terminal_response=response,
            expected_execution_attempt_id=str(
                context["payload"].get("execution_attempt_id") or ""
            ).strip(),
            expected_lease_epoch=_gateway_lease_epoch(context["payload"]),
            expected_lease_heartbeat_at=_gateway_lease_heartbeat(
                context["payload"]
            ),
            expected_request_status="processing",
            expected_turn_phase=str(
                context["payload"].get("turn_phase") or "open"
            ),
            expected_cancel_requested=(
                context["payload"].get("cancel_requested") is True
            ),
        )
    except OSError as exc:
        _report_gateway_side_effect_error("archive_gateway_failed_request", request_path.stem, exc)
        return ""
    _publish_gateway_failure_response(paths, failure_context, response)
    return "failed"


# LLM: cause 是续跑排序与诊断读取的结构化事实：安全重启接班、普通重启、服务内租约过期三者互斥。
# 函数用途: 给重排的回合标注这次续跑的原因。
def _requeue_cause(context: _RecoveryContext) -> str:
    if context.planned_restart:
        return GATEWAY_SAFE_RESTART_CAUSE
    return "gateway_restart" if context.startup else "processing_lease_expired"


# LLM: Preserve request/run identity and committed effects while replacing only the dead lease.
# Record observed processing failures under the existing turn lock; startup resumes do not
# consume that budget or reset earlier failures. This does not start/restart the Gateway itself.
# 函数用途: 精确重排原回合；服务重启沿用原任务，卡死单独计数，保留启动退避及旧副作用保护。
def _requeue_stale_processing(
    paths: GatewayPaths,
    request_path: Path,
    payload: dict,
    context: _RecoveryContext,
) -> str:
    try:
        request_id = request_path.stem
        with gateway_turn_transition(paths, request_id):
            fresh = _fresh_matching_stale_payload(request_path, payload, context)
            if fresh is None:
                return ""
            dead_attempt_id = str(fresh.get("execution_attempt_id") or "").strip()
            _release_dead_attempt_guidance(
                _recovery_conversation_store(context.agent, fresh),
                request_id,
                dead_attempt_id=dead_attempt_id,
            )
            requeued = dict(fresh)
            if context.startup and not context.planned_restart:
                requeued["not_before_at"] = context.now + 10
            requeued.update(
                {
                    "status": "pending",
                    "priority": "recovery",
                    "source": str(fresh.get("source") or "gateway_recovery"),
                    "requeued_at": context.now,
                    "processing_failure_count": (
                        _gateway_processing_failure_count(fresh) + (0 if context.startup else 1)
                    ),
                    "active_turn_recovery": {
                        "schema_version": _ACTIVE_TURN_RECOVERY_SCHEMA,
                        "request_id": request_id,
                        "dead_execution_attempt_id": dead_attempt_id,
                        "requeued_at": context.now,
                        "cause": _requeue_cause(context),
                    },
                    "last_error": (
                        "gateway restarted before request completed"
                        if context.startup
                        else f"gateway processing timeout after {context.timeout_seconds}s"
                    ),
                }
            )
            for key in (
                "execution_attempt_id",
                "lease_owner",
                "lease_started_at",
                "lease_heartbeat_at",
                "lease_process_identity",
            ):
                requeued.pop(key, None)
            write_json_file_atomic(request_path, requeued)
            request_path.replace(paths.inbox / request_path.name)
    except OSError as exc:
        _report_gateway_side_effect_error("requeue_gateway_request", request_path.stem, exc)
        return ""
    return "requeued"


def requeue_gateway_processing_requests(paths: GatewayPaths) -> int:

    return recover_gateway_processing_requests(paths, startup=True)["requeued"]


def _gateway_request_attempts(payload: dict) -> int:
    return gateway_request_attempts(payload)


# LLM: This new host-owned counter is independent from legacy total dispatch attempts.
# Missing old records start at zero observed lease failures; history prose cannot reconstruct
# a cause. Malformed persisted counters must fail closed rather than reset a consumed budget.
# 函数用途: 读取已记录的真正卡死次数；旧记录无法证明的重启/失败不瞎猜，坏计数交给数据诊断。
def _gateway_processing_failure_count(payload: dict) -> int:
    value = payload.get("processing_failure_count", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DataCorruptionError("invalid gateway processing_failure_count")
    return value


def _gateway_processing_started_at(payload: dict, request_path: Path) -> float:
    return gateway_processing_started_at(payload, request_path)


# LLM: 插话持久操作经 guidance 领域组件； Recovery calls this only after the request lease proves the owning process/attempt dead.
# Reserved input is then safe to release; submitted input remains unknown inside ConversationStore.
# 函数用途: 在过期请求重排或失败前释放尚未开始模型提交的补充消息。
def _release_dead_attempt_guidance(
    conversation_store: object | None,
    request_id: str,
    *,
    dead_attempt_id: str,
) -> None:
    release = getattr(getattr(getattr(conversation_store, 'guidance', None), 'recovery', None), 'release_reserved', None)
    if callable(release):
        release(request_id, dead_attempt_id=dead_attempt_id)


# LLM: Failure construction is pure so terminalization can persist the complete response under the
# exact-turn lock before any client-visible response or history projection is written.
# 函数用途: 根据恢复失败事实构造最终答复，不写文件也不发送日志。
def _build_gateway_failure_response(context: dict) -> dict:

    request_path = context["request_path"]
    payload = context["payload"]
    request_id = request_path.stem
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
    return response


# LLM: This projection runs only after the terminal archive contains the full response. A crash
# before or during projection can therefore be repaired from that archive without rerunning a model.
# 流终态经 stream_writer 直接投影，不加载请求执行器；显示失败不改变已归档终态。
# 函数用途: 在终态归档成功后写 response、history 和诊断日志。
def _publish_gateway_failure_response(
    paths: GatewayPaths,
    context: dict,
    response: dict,
) -> None:
    request_path = context["request_path"]
    payload = context["payload"]
    request_id = request_path.stem
    response_path = gateway_response_path(paths, request_id)
    if not response_path.exists():
        write_json_file_atomic(response_path, response)
    append_gateway_history_once(paths, response)
    # #4: recovery 失败也向 chunks 流写终态事件(对齐 会话运行时 TurnAborted)——纯 chunks
    # 消费者(rich TUI 等)收到即复位, 不依赖 response 文件轮询。
    try:
        from .paths import claimed_request_chunk_path
        from .stream_writer import write_chunk_event

        chunk_path = claimed_request_chunk_path(request_path, request_id)
        if chunk_path.exists() or chunk_path.parent.exists():
            write_chunk_event(
                chunk_path,
                {
                    "kind": "request_aborted",
                    "error_code": str(context.get("error_code") or "GATEWAY_REQUEST_ABORTED"),
                    "status": "failed",
                },
            )
    except Exception:  # noqa: BLE001 - 终态事件是增强, 失败不影响 recovery 本身
        pass
    agent = context.get("agent")
    if agent:
        log_gateway_payload(
            agent,
            {**response, "prompt": payload.get("prompt", "")},
            event_type=context["event_type"],
            request_path=request_path,
            response_path=response_path,
        )


def _archive_gateway_request(path: Path, target_dir: Path) -> Path:

    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    if target.exists():
        target = target_dir / f"{path.stem}-{int(time.time())}{path.suffix}"
    path.replace(target)
    return target


# LLM: A Gateway request has exactly one canonical terminal file named by request id. Replays may
# retire an identical processing copy, but conflicting terminal outcomes fail closed without suffixes.
# 函数用途: 原子提交唯一终态归档，并修复 done/failed 展示投影。
def _commit_gateway_terminal_request(
    paths: GatewayPaths,
    path: Path,
    target_dir: Path,
    request_id: str,
    terminal_payload: dict,
) -> Path:
    paths.terminal.mkdir(parents=True, exist_ok=True)
    canonical = paths.terminal / f"{request_id}.json"
    if canonical.exists():
        existing_report = read_json_file_report(
            canonical,
            context="gateway.terminal_archive.read",
        )
        if (
            existing_report.load_error is not None
            or not _same_gateway_terminal_outcome(existing_report.payload, terminal_payload)
        ):
            raise RuntimeError(f"conflicting gateway terminal outcome: {request_id}")
        if path.exists() and path != canonical:
            hot_report = read_json_file_report(
                path,
                context="gateway.terminal_archive.hot_request.read",
            )
            if (
                hot_report.load_error is not None
                or not _same_gateway_request_identity(
                    existing_report.payload,
                    hot_report.payload,
                    request_id=request_id,
                )
            ):
                raise RuntimeError(
                    f"gateway hot request conflicts with canonical terminal: {request_id}"
                )
            path.unlink()
    else:
        # Commit the complete terminal authority before retiring the mutable
        # processing record. A crash can leave both files, but never leaves a
        # completed answer only inside a record that recovery may requeue.
        write_json_file_atomic(canonical, terminal_payload)
        if path.exists() and path != canonical:
            path.unlink()

    other_folder = paths.failed if target_dir == paths.done else paths.done
    other_projection = other_folder / f"{request_id}.json"
    if other_projection.exists():
        other_report = read_json_file_report(
            other_projection,
            context="gateway.terminal_projection.read",
        )
        if (
            other_report.load_error is not None
            or not _same_gateway_terminal_outcome(other_report.payload, terminal_payload)
        ):
            raise RuntimeError(f"conflicting gateway terminal projection: {request_id}")
        other_projection.unlink()

    target_dir.mkdir(parents=True, exist_ok=True)
    projection = target_dir / f"{request_id}.json"
    if projection.exists():
        projection_report = read_json_file_report(
            projection,
            context="gateway.terminal_projection.read",
        )
        if (
            projection_report.load_error is not None
            or not _same_gateway_terminal_outcome(projection_report.payload, terminal_payload)
        ):
            raise RuntimeError(f"conflicting gateway terminal projection: {request_id}")
    else:
        write_json_file_atomic(projection, terminal_payload)
    return canonical


# LLM: Idempotent terminal replay requires both the immutable request fingerprint and complete
# terminal response to match. Equal prose alone never proves that two request executions are one.
# 函数用途: 判断两份终态记录是否属于同一请求内容且有完全相同的答复。
def _same_gateway_terminal_outcome(left: dict, right: dict) -> bool:
    left_response = left.get("terminal_response") if isinstance(left, dict) else None
    right_response = right.get("terminal_response") if isinstance(right, dict) else None
    if isinstance(left_response, dict) and isinstance(right_response, dict):
        request_id = str(left.get("id") or "").strip()
        return (
            bool(request_id)
            and request_id == str(right.get("id") or "").strip()
            and _same_gateway_request_identity(
                left,
                right,
                request_id=request_id,
            )
            and left_response == right_response
        )
    return left == right


# LLM: Canonical/hot retirement compares a freshly recomputed immutable fingerprint on both rows;
# a copied or stale digest cannot hide changed owner, prompt, task, or execution options.
# 函数用途: 校验终态归档和待清理热请求确实是同一份请求。
def _same_gateway_request_identity(
    left: dict,
    right: dict,
    *,
    request_id: str,
) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if (
        str(left.get("id") or "").strip() != request_id
        or str(right.get("id") or "").strip() != request_id
    ):
        return False
    try:
        left_fingerprint = validated_gateway_request_fingerprint(left, request_id)
        right_fingerprint = validated_gateway_request_fingerprint(right, request_id)
    except DataCorruptionError:
        return False
    return left_fingerprint == right_fingerprint


# LLM: Projection repair trusts only the exact canonical terminal archive. It never fabricates
# owner/request facts from a standalone response after the processing record disappeared.
# 函数用途: 校验唯一终态归档与完整答复一致，并幂等修复完成或失败目录投影。
def repair_gateway_terminal_request(
    paths: GatewayPaths,
    target_dir: Path,
    request_id: str,
    response: dict,
) -> Path:
    turn_id = str(request_id or "").strip()
    if not turn_id:
        raise ValueError("gateway terminal repair requires request_id")
    with gateway_turn_transition(paths, turn_id):
        canonical = paths.terminal / f"{turn_id}.json"
        report = read_json_file_report(
            canonical,
            context="gateway.terminal_repair.read",
        )
        if report.load_error is not None or not report.payload:
            raise DataCorruptionError(
                f"canonical gateway terminal outcome is unavailable: {turn_id}"
            )
        terminal_response = report.payload.get("terminal_response")
        if not isinstance(terminal_response, dict) or terminal_response != response:
            raise DataCorruptionError(
                f"gateway terminal response conflicts with canonical outcome: {turn_id}"
            )
        return _commit_gateway_terminal_request(
            paths,
            canonical,
            target_dir,
            turn_id,
            report.payload,
        )


# LLM: Release only the host-bound request's running recovery claim, using the already scoped
# store and atomic task selector. No model calls, path guessing, or replacement-attempt recovery.
# 函数用途: 结束原请求遗留的执行权；旧清理不能释放其他请求、线程或普通后台的租约。
def _finish_gateway_conversation_claim(
    payload: dict, request_id: str, conversation_store: object | None,
) -> None:
    binding = payload.get("conversation_claim")
    if binding is None:
        return
    if (
        not isinstance(binding, dict)
        or binding.get("schema_version") != "gateway_conversation_claim.v1"
        or binding.get("request_id") != request_id
        or binding.get("task_id") != f"gateway:{request_id}"
        or not isinstance(binding.get("thread_id"), str)
        or not binding["thread_id"].strip()
    ):
        raise DataCorruptionError("Gateway 执行车道引用与原请求不一致")
    if conversation_store is None:
        raise RuntimeError("Gateway 执行车道收尾缺少原用户会话存储")
    conversation_store.claims.finish({
        "thread_id": binding["thread_id"],
        "expected_task_id": binding["task_id"],
        "recover_same_task_only": True,
        "status": {"failed": "failed", "cancelled": "cancelled", "interrupted": "cancelled"}.get(
            payload.get("status"), "finished",
        ),
        "runtime_facts": {"execution_source": "gateway", "request_id": request_id},
    })


# LLM: 插话持久操作经 guidance 领域组件； Sole terminal transition seals the response before exact claim/guidance cleanup. Failed
# cleanup leaves a sealed hot record for startup commit repair, never another model invocation.
# 函数用途: 保存完整答复，释放原请求执行权、收口补充消息并移动到终态目录；失败可幂等补交。
def terminalize_gateway_request_file(
    paths: GatewayPaths,
    path: Path,
    target_dir: Path,
    request_id: str,
    *,
    conversation_store: object | None = None,
    terminal_response: dict | None = None,
    expected_execution_attempt_id: str = "",
    expected_lease_epoch: int = 0,
    expected_lease_heartbeat_at: float | None = None,
    expected_request_status: str = "",
    expected_turn_phase: str = "",
    expected_cancel_requested: bool | None = None,
) -> Path:
    turn_id = str(request_id or path.stem).strip()
    canonical_path = paths.terminal / f"{turn_id}.json"
    if (
        not turn_id
        or path.stem != turn_id
        or (path != canonical_path and path.parent != paths.processing)
    ):
        raise DataCorruptionError(
            "gateway terminal transition path conflicts with exact request id"
        )
    terminal_payload: dict = {}
    with gateway_turn_transition(paths, turn_id):
        canonical = canonical_path
        path_exists = path.is_file()
        request_report = (
            read_json_file_report(
                path,
                context="gateway.terminalize.request.read",
            )
            if path_exists
            else None
        )
        if request_report is not None:
            _require_expected_gateway_execution_attempt(
                request_report.payload,
                turn_id,
                expected_execution_attempt_id=expected_execution_attempt_id,
                expected_lease_epoch=expected_lease_epoch,
                expected_lease_heartbeat_at=expected_lease_heartbeat_at,
                expected_request_status=expected_request_status,
                expected_turn_phase=expected_turn_phase,
                expected_cancel_requested=expected_cancel_requested,
            )
        if not path_exists:
            canonical_report = read_json_file_report(
                canonical,
                context="gateway.terminalize.missing_processing.read",
            )
            canonical_response = canonical_report.payload.get("terminal_response")
            if (
                canonical_report.load_error is not None
                or canonical_report.payload.get("schema_version")
                != "gateway_terminal_request.v1"
                or str(canonical_report.payload.get("id") or "").strip() != turn_id
                or not isinstance(canonical_response, dict)
                or not canonical_response
            ):
                raise FileNotFoundError(
                    f"gateway processing and canonical terminal request are missing: {turn_id}"
                )
            if terminal_response is not None and canonical_response != terminal_response:
                raise DataCorruptionError(
                    f"gateway terminal replay conflicts with canonical outcome: {turn_id}"
                )
            terminal_payload = dict(canonical_report.payload)
        elif terminal_response is not None:
            _prepare_gateway_chunk_projection(paths, target_dir, turn_id, terminal_response)
            assert request_report is not None
            if request_report.load_error is not None:
                terminal_response.setdefault(
                    "final_request_load_error",
                    request_report.load_error,
                )
            terminal_payload = _terminal_gateway_request_payload(
                turn_id,
                request_report.payload,
                terminal_response,
            )
            # Seal the full provider result in the hot record before any
            # cross-ledger settlement. Startup recovery recognizes this schema
            # and can finish the commit without invoking the model again.
            write_json_file_atomic(path, terminal_payload)
        else:
            assert request_report is not None
            if request_report.load_error is not None or not request_report.payload:
                raise RuntimeError(f"gateway terminal request is unreadable: {turn_id}")
            terminal_payload = dict(request_report.payload)
        if conversation_store is not None:
            try:
                mailbox_summary = conversation_store.guidance.recovery.reject_pending(
                    turn_id,
                    reject_reserved=True,
                )
                if (
                    terminal_response is not None
                    and path_exists
                    and mailbox_summary.get("errors")
                ):
                    terminal_response["input_settlement_errors"] = int(
                        mailbox_summary.get("errors") or 0
                    )
                    terminal_payload = _terminal_gateway_request_payload(
                        turn_id,
                        terminal_payload,
                        terminal_response,
                    )
            except Exception as exc:
                if terminal_response is not None and path_exists:
                    terminal_response["input_settlement_error"] = runtime_error_report(
                        exc,
                        context="gateway.terminalize.guidance_settlement",
                    )
                    terminal_payload = _terminal_gateway_request_payload(
                        turn_id,
                        terminal_payload,
                        terminal_response,
                    )
        if terminal_response is not None and path.is_file():
            write_json_file_atomic(path, terminal_payload)
        _finish_gateway_conversation_claim(terminal_payload, turn_id, conversation_store)
        archived = _commit_gateway_terminal_request(
            paths,
            path if path_exists else canonical,
            target_dir,
            turn_id,
            terminal_payload,
        )
    from .input_delivery_service import settle_gateway_inputs_for_turn

    try:
        settle_gateway_inputs_for_turn(
            paths,
            target_turn_id=turn_id,
            conversation_store=conversation_store,
        )
    except Exception:
        # The archived terminal response is the canonical completion fact. An
        # ingress reconciliation failure remains retryable and must not hide it.
        pass
    return archived


# LLM: request_id is reused across recovery attempts, so terminal commit must compare the
# immutable attempt id and monotonic epoch observed by the worker that produced the response.
# 函数用途: 在封存答复前确认当前 processing 文件仍属于产出该答复的执行代次。
def _require_expected_gateway_execution_attempt(
    payload: dict,
    request_id: str,
    *,
    expected_execution_attempt_id: str,
    expected_lease_epoch: int,
    expected_lease_heartbeat_at: float | None,
    expected_request_status: str,
    expected_turn_phase: str,
    expected_cancel_requested: bool | None,
) -> None:
    expected_attempt = str(expected_execution_attempt_id or "").strip()
    if expected_attempt and str(payload.get("execution_attempt_id") or "").strip() != expected_attempt:
        raise InterruptedError(
            f"stale gateway execution attempt cannot terminalize request: {request_id}"
        )
    expected_epoch = max(0, int(expected_lease_epoch or 0))
    if expected_epoch and _gateway_lease_epoch(payload) != expected_epoch:
        raise InterruptedError(
            f"stale gateway lease epoch cannot terminalize request: {request_id}"
        )
    if (
        expected_lease_heartbeat_at is not None
        and _gateway_lease_heartbeat(payload) != float(expected_lease_heartbeat_at)
    ):
        raise InterruptedError(
            f"gateway lease changed before terminal recovery: {request_id}"
        )
    expected_status = str(expected_request_status or "").strip()
    if expected_status and str(payload.get("status") or "").strip() != expected_status:
        raise InterruptedError(
            f"gateway request state changed before terminal recovery: {request_id}"
        )
    expected_phase = str(expected_turn_phase or "").strip()
    if expected_phase and str(payload.get("turn_phase") or "open").strip() != expected_phase:
        raise InterruptedError(
            f"gateway turn phase changed before terminal recovery: {request_id}"
        )
    if (
        expected_cancel_requested is not None
        and (payload.get("cancel_requested") is True) is not expected_cancel_requested
    ):
        raise InterruptedError(
            f"gateway cancellation changed before terminal recovery: {request_id}"
        )


# LLM: Chunk data is a repairable terminal projection. The intended stable destination is embedded
# before the canonical terminal commit, while the actual rename happens only after that commit.
# 函数用途: 在封存最终答复前登记流式记录的稳定归档位置，但不提前移动文件。
def _prepare_gateway_chunk_projection(
    paths: GatewayPaths,
    target_dir: Path,
    request_id: str,
    terminal_response: dict,
) -> None:
    source = gateway_chunk_path(paths, request_id)
    target = target_dir / source.name
    if source.exists() or target.exists():
        terminal_response["chunk_stream_path"] = str(target)


# LLM: Canonical terminal data owns the planned chunk destination. Projector/recovery may repeat
# this rename after any crash; conflicting destinations fail without changing the authority.
# 函数用途: 根据终态答复把遗留流式记录幂等移动到完成或失败目录。
def repair_gateway_chunk_projection(
    paths: GatewayPaths,
    request_id: str,
    terminal_response: dict,
) -> Path | None:
    raw_target = str(terminal_response.get("chunk_stream_path") or "").strip()
    if not raw_target:
        return None
    source = gateway_chunk_path(paths, request_id)
    target = Path(raw_target)
    allowed_roots = {paths.done.resolve(), paths.failed.resolve()}
    if target.parent.resolve() not in allowed_roots or target.name != source.name:
        raise DataCorruptionError("gateway chunk projection target is outside terminal folders")
    if target.exists():
        if source.exists() and source.read_bytes() != target.read_bytes():
            raise DataCorruptionError("gateway chunk projection conflicts with source stream")
        if source.exists():
            source.unlink()
        return target
    if not source.exists():
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    source.replace(target)
    return target


# LLM: This helper is the sole schema builder for a terminal request archive. The complete response
# is embedded as authority; /responses and history are repairable projections after a crash.
# Observed lease failures and total dispatch attempts stay separate in terminal projections.
# 函数用途: 把锁内读取到的最新请求状态和完整最终答复合并成可恢复的终态归档。
def _terminal_gateway_request_payload(
    request_id: str,
    current_payload: dict,
    terminal_response: dict,
) -> dict:
    payload = dict(current_payload) if isinstance(current_payload, dict) else {}
    request_fingerprint = validated_gateway_request_fingerprint(payload, request_id)
    response_id = str(terminal_response.get("id") or "").strip()
    if response_id != request_id:
        raise DataCorruptionError(
            f"gateway terminal response identity conflicts with request: {request_id}"
        )
    if not str(payload.get("request_fingerprint") or "").strip():
        payload["request_fingerprint_migration"] = {
            "schema_version": "gateway_request_fingerprint_migration.v1",
            "decision": "materialized_from_canonical_request_fields",
        }
    status = str(
        terminal_response.get("status")
        or ("done" if terminal_response.get("ok") else "failed")
    )
    payload.update(
        {
            "schema_version": "gateway_terminal_request.v1",
            "id": request_id,
            "request_fingerprint_schema": GATEWAY_REQUEST_FINGERPRINT_SCHEMA,
            "request_fingerprint": request_fingerprint,
            "status": status,
            "turn_phase": "closed",
            "attempts": terminal_response.get("attempts", payload.get("attempts", 0)),
            "processing_failure_count": terminal_response.get(
                "processing_failure_count", _gateway_processing_failure_count(payload)
            ),
            "lease_owner": terminal_response.get(
                "lease_owner", payload.get("lease_owner", "")
            ),
            "lease_started_at": terminal_response.get(
                "lease_started_at", payload.get("lease_started_at", 0)
            ),
            "lease_heartbeat_at": terminal_response.get(
                "lease_heartbeat_at", payload.get("lease_heartbeat_at", 0)
            ),
            "completed_at": terminal_response.get("ended_at", time.time()),
            "ok": bool(terminal_response.get("ok")),
            "error_code": str(terminal_response.get("error_code") or ""),
            "error": str(terminal_response.get("error") or ""),
            "terminal_response": dict(terminal_response),
        }
    )
    if "request_id" in payload:
        payload["request_id"] = request_id
    return payload


# LLM: Recovery must resolve the same owner store as normal execution before terminalizing. A
# missing/corrupt owner scope returns None and never falls back to another user's store.
# 函数用途: 为恢复收口查找请求所属会话存储；解析失败时保持权限边界并跳过回执扫描。
def _recovery_conversation_store(agent: SimpleAgent | None, payload: dict) -> object | None:
    if agent is None:
        return None
    try:
        from .request_worker import _resolve_request_agent

        return _resolve_request_agent(agent, payload).conversation_store
    except Exception:
        return None
