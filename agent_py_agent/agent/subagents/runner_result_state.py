
from __future__ import annotations

"""task state mutation rules for runner result recording.

runner 写回状态的分支比较多，单独放这里，manager mixin 只负责串起读写流程。
"""

from dataclasses import dataclass

from .capability_status import is_pending_capability_status
from .model_capabilities import capability_request_counts_as_open
from .policies import _status_from_structured_output, _verification_from_runner_status

_RUNNER_FAILURE_STATUSES = {"BLOCKED", "FAILED", "CHANNEL_ERROR", "TIMEOUT"}


@dataclass(frozen=True)
class RunnerResultFieldParams:

    task: object
    result_meta: dict
    status_context: dict
    parsed: object
    now: float


@dataclass(frozen=True)
class RunnerAttemptParams:

    task: object
    dry_run: bool
    ok: bool
    message: str
    now: float


def apply_runner_result_fields(params: RunnerResultFieldParams) -> None:
    """Apply parsed runner status and raw status text to a task in place."""
    task = params.task
    result_meta = params.result_meta
    status_context = params.status_context
    parsed = params.parsed
    ok = result_meta["ok"]
    message = result_meta["message"]
    response = result_meta["response"]
    dry_run = result_meta["dry_run"]
    parsed_ok = parsed.ok
    _apply_status_fields(task, status_context, parsed)

    ok, message = _runner_result_outcome(task, parsed, result_meta, status_context)

    if response and not (parsed.found and not parsed_ok):
        task.result = response
    elif message:
        task.result = message
    _apply_runner_timestamps(task, params.now)
    _apply_runner_attempt_fields(RunnerAttemptParams(task, dry_run, ok, message, params.now))
    result_meta["ok"] = ok
    result_meta["message"] = message


def _runner_result_outcome(task, parsed, result_meta: dict, status_context: dict) -> tuple[bool, str]:
    ok = result_meta["ok"]
    message = result_meta["message"]
    response = result_meta["response"]
    if parsed.found and not parsed.ok:
        message = f"{message} / structured output parse failed: {parsed.parse_error}"
        task.result = response or message
        return False, message
    if parsed.found and task.status in _RUNNER_FAILURE_STATUSES:
        return False, parsed.blocked_reason or message or task.failure_type or task.status.lower()
    if not parsed.found:
        _apply_unstructured_failure(task, ok, status_context["failure_type"])
        task.result = response or message or task.result
    return ok, message


def _apply_status_fields(task, status_context, parsed) -> None:
    status = status_context["status"]
    verification_status = status_context["verification_status"]
    failure_type = status_context["failure_type"]
    if parsed.found and parsed.ok:
        task.status = (status or _status_from_structured_output(parsed)).upper()
        task.verification_status = (verification_status or _verification_from_runner_status(task.status)).upper()
        _apply_structured_failure_state(task, failure_type or parsed.failure_type, parsed)
        return
    if parsed.found and not parsed.ok:
        task.status = status.upper() if status else "BLOCKED"
        task.verification_status = verification_status.upper() if verification_status else "UNVERIFIED"
        if _has_open_capability_requests(task):
            task.failure_type = failure_type or "capability_request"
            _append_open_request_blocker(task)
        else:
            task.failure_type = failure_type or "structured_output_parse_error"
        return
    if status:
        task.status = status.upper()
    if verification_status:
        task.verification_status = verification_status.upper()
    if failure_type:
        task.failure_type = failure_type


def _apply_structured_failure_state(task, current_failure_type: str, parsed) -> None:
    if current_failure_type:
        task.failure_type = current_failure_type
        return
    if is_pending_capability_status(str(getattr(parsed, "status", "") or "")):
        task.failure_type = "capability_request"
        return
    if parsed.capability_requests or parsed.blocked_reason:
        task.failure_type = "capability_request"
        return
    if _should_resolve_stale_capability_requests(task):
        _resolve_stale_capability_requests(task)
    if _has_open_capability_requests(task):
        task.status = "BLOCKED"
        task.verification_status = "UNVERIFIED"
        task.failure_type = "capability_request"
        _append_open_request_blocker(task)
        return
    if task.status in _RUNNER_FAILURE_STATUSES:
        task.failure_type = task.failure_type or task.status.lower()
        return
    task.failure_type = ""
    task.blockers = []
    _resolve_stale_capability_requests(task)


def _should_resolve_stale_capability_requests(task) -> bool:
    return str(getattr(task, "failure_type", "") or "") == "capability_request"


def _has_open_capability_requests(task) -> bool:
    return any(
        capability_request_counts_as_open(getattr(request, "status", "OPEN"))
        for request in getattr(task, "capability_requests", []) or []
    )


def _append_open_request_blocker(task) -> None:
    blocker = "已有 OPEN capability_request，等待父级 route_capability_request。"
    if blocker not in getattr(task, "blockers", []):
        task.blockers.append(blocker)


def _resolve_stale_capability_requests(task) -> None:
    for request in getattr(task, "capability_requests", []) or []:
        if capability_request_counts_as_open(getattr(request, "status", "OPEN")):
            request.status = "CLOSED"


def _apply_unstructured_failure(task, ok, failure_type: str) -> None:
    if failure_type:
        task.failure_type = failure_type
    elif not ok:
        task.failure_type = task.failure_type or "runner_error"


def _apply_runner_timestamps(task, now: float) -> None:
    if task.status in {"DONE", "FAILED", "BLOCKED", "CHANNEL_ERROR", "TIMEOUT"}:
        task.ended_at = now
    if str(getattr(task, "status", "") or "").upper() == "DONE":
        task.progress = 1.0
    elif str(getattr(task, "status", "") or "").upper() == "RUNNING":
        task.progress = max(_safe_progress(getattr(task, "progress", 0.0)), 0.05)
    task.updated_at = now
    task.heartbeat_at = now


def _apply_runner_attempt_fields(params: RunnerAttemptParams) -> None:
    task = params.task
    if params.dry_run:
        return
    task.runner_attempts = max(0, int(task.runner_attempts or 0)) + 1
    task.runner_last_attempt_at = params.now
    if not params.ok or task.status in {"BLOCKED", "FAILED", "CHANNEL_ERROR", "TIMEOUT"}:
        task.runner_last_error = params.message
    else:
        task.runner_last_error = ""
    if str(task.runner_active_attempt_id or "").strip():
        task.runner_active_attempt_id = ""


def _safe_progress(value: object) -> float:
    try:
        progress = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, progress))
