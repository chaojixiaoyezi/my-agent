from __future__ import annotations

"""Structured same-run recovery eligibility for user-stopped subagents."""

from .model_capabilities import capability_request_requires_parent_resolution
from .models import (
    FailureType,
    TaskStatus,
    task_status_in,
)

_USER_STOP_REASON = "conversation_user_stop"
_RECOVERABLE_PREVIOUS_STATUSES = frozenset(
    {
        TaskStatus.PLANNING.value,
        TaskStatus.PENDING.value,
        TaskStatus.RUNNING.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.FAILED.value,
        TaskStatus.TIMEOUT.value,
    }
)


def user_stopped_resume_eligibility(task: object) -> dict[str, object]:
    """Return a typed recovery decision; free-form task text never participates."""

    cancel = _cancel_record(task)
    run_id = str(getattr(task, "id", "") or "").strip()
    previous_status = str(cancel.get("previous_status") or "").strip().upper()
    blockers: list[str] = []
    if not task_status_in(getattr(task, "status", ""), {TaskStatus.CANCELLED.value}):
        blockers.append("status_not_cancelled")
    if str(getattr(task, "failure_type", "") or "").strip() != FailureType.CANCELLED.value:
        blockers.append("failure_type_not_cancelled")
    if str(cancel.get("reason") or "").strip() != _USER_STOP_REASON:
        blockers.append("not_conversation_user_stop")
    if previous_status not in _RECOVERABLE_PREVIOUS_STATUSES:
        blockers.append("previous_status_not_recoverable")
    if str(getattr(task, "channel_status", "") or "").strip() == "BROKEN":
        blockers.append("channel_broken")
    if _has_open_capability_request(task):
        blockers.append("open_capability_request")
    if _has_open_capability_gap(task):
        blockers.append("open_capability_gap")
    eligible = bool(run_id and not blockers)
    return {
        "eligible": eligible,
        "reason_code": _USER_STOP_REASON if eligible else "",
        "run_id": run_id,
        "previous_status": previous_status,
        "required_recovery_mode": "rerun_from_checkpoint",
        "requires_explicit_run_id": True,
        "same_run_only": True,
        "blockers": blockers,
    }


def user_stopped_run_is_resumable(task: object) -> bool:
    return bool(user_stopped_resume_eligibility(task)["eligible"])


def _cancel_record(task: object) -> dict[str, object]:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return {}
    value = attrs.get("cancel_subagents")
    return dict(value) if isinstance(value, dict) else {}


def _has_open_capability_request(task: object) -> bool:
    return any(
        capability_request_requires_parent_resolution(getattr(item, "status", "OPEN"))
        for item in getattr(task, "capability_requests", []) or []
    )


def _has_open_capability_gap(task: object) -> bool:
    return any(
        str(getattr(item, "status", "") or "").strip() == "OPEN"
        for item in getattr(task, "capability_gaps", []) or []
    )


__all__ = [
    "user_stopped_resume_eligibility",
    "user_stopped_run_is_resumable",
]
