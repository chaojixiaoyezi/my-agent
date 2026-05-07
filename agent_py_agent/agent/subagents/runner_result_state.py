from __future__ import annotations

"""LLM: task state mutation rules for runner result recording.

给人看的解释：
runner 写回状态的分支比较多，单独放这里，manager mixin 只负责串起读写流程。
"""

from dataclasses import dataclass

from .policies import _status_from_structured_output, _verification_from_runner_status


@dataclass(frozen=True)
class RunnerResultFieldParams:
    """LLM: bundle runner result mutation inputs."""

    task: object
    result_meta: dict
    status_context: dict
    parsed: object
    now: float


@dataclass(frozen=True)
class RunnerAttemptParams:
    """LLM: bundle runner attempt counters and last-error state."""

    task: object
    dry_run: bool
    ok: bool
    message: str
    now: float


def apply_runner_result_fields(params: RunnerResultFieldParams) -> None:
    """Apply parsed runner status and raw fallback status to a task in place."""
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

    if parsed.found and not parsed_ok:
        ok = False
        message = f"{message} / structured output parse failed: {parsed.parse_error}"
        task.result = response or message
    elif not parsed.found:
        _apply_unstructured_failure(task, ok, status_context["failure_type"])
        task.result = response or message or task.result

    if response and not (parsed.found and not parsed_ok):
        task.result = response
    elif message:
        task.result = message
    _apply_runner_timestamps(task, params.now)
    _apply_runner_attempt_fields(RunnerAttemptParams(task, dry_run, ok, message, params.now))
    result_meta["ok"] = ok
    result_meta["message"] = message


def _apply_status_fields(task, status_context, parsed) -> None:
    status = status_context["status"]
    verification_status = status_context["verification_status"]
    failure_type = status_context["failure_type"]
    if parsed.found and parsed.ok:
        task.status = (status or _status_from_structured_output(parsed)).upper()
        task.verification_status = (verification_status or _verification_from_runner_status(task.status)).upper()
        task.failure_type = failure_type or parsed.failure_type or task.failure_type
        if task.capability_requests and not failure_type:
            task.failure_type = "capability_request"
        return
    if parsed.found and not parsed.ok:
        task.status = status.upper() if status else "BLOCKED"
        task.verification_status = verification_status.upper() if verification_status else "UNVERIFIED"
        task.failure_type = failure_type or "structured_output_parse_error"
        return
    if status:
        task.status = status.upper()
    if verification_status:
        task.verification_status = verification_status.upper()
    if failure_type:
        task.failure_type = failure_type


def _apply_unstructured_failure(task, ok, failure_type: str) -> None:
    if failure_type:
        task.failure_type = failure_type
    elif not ok:
        task.failure_type = task.failure_type or "runner_error"


def _apply_runner_timestamps(task, now: float) -> None:
    if task.status in {"DONE", "FAILED", "BLOCKED", "CHANNEL_ERROR", "TIMEOUT"}:
        task.ended_at = now
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
