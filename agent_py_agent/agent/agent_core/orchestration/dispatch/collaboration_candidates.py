from __future__ import annotations

from dataclasses import dataclass

from ....runtime_errors import runtime_error_report
from ....subagents.models import TaskStatus, task_status_in


@dataclass(frozen=True)
class CollaborationCandidateReport:
    candidates: list
    load_errors: list[dict[str, object]]


def collaboration_request_runner_candidates(agent, tasks: list) -> list:
    return collaboration_request_runner_candidates_report(agent, tasks).candidates


def collaboration_request_runner_candidates_report(agent, tasks: list) -> CollaborationCandidateReport:
    store = getattr(agent, "collaboration_store", None)
    if store is None:
        return CollaborationCandidateReport([], [])
    candidates = []
    load_errors: list[dict[str, object]] = []
    for task in tasks:
        has_request, error = _has_pending_request_for_task(store, task)
        if error:
            load_errors.append(error)
        if has_request:
            candidates.append(task)
    return CollaborationCandidateReport(candidates, load_errors)


def _has_pending_request_for_task(store: object, task: object) -> tuple[bool, dict[str, object] | None]:
    if not _can_run_for_collaboration_request(task):
        return False, None
    try:
        requests, load_errors = _pending_requests_for_task(store, task)
    except Exception as exc:
        return False, _candidate_load_error(task, exc)
    if load_errors:
        return bool(requests), _candidate_reported_load_error(task, load_errors)
    return bool(requests), None


def _pending_requests_for_task(store: object, task: object) -> tuple[list, list[dict[str, object]]]:
    kwargs = {
        "agent_id": str(getattr(task, "id", "") or ""),
        "agent_name": str(getattr(task, "agent_name", "") or ""),
        "agent_role": str(getattr(task, "role", "") or ""),
        "limit": 1,
    }
    if hasattr(store, "pending_requests_for_agent_report"):
        return store.pending_requests_for_agent_report(**kwargs)
    if not hasattr(store, "pending_requests_for_agent"):
        return [], []
    return store.pending_requests_for_agent(**kwargs), []


def _candidate_load_error(task: object, exc: BaseException) -> dict[str, object]:
    return {
        "run_id": str(getattr(task, "id", "") or ""),
        **runtime_error_report(exc, context="dispatch.collaboration_candidates.pending_requests"),
    }


def _candidate_reported_load_error(task: object, load_errors: list[dict[str, object]]) -> dict[str, object]:
    return {
        "run_id": str(getattr(task, "id", "") or ""),
        "context": "dispatch.collaboration_candidates.pending_requests",
        "category": "ledger_read",
        "model_message": "协作候选扫描读取到坏账本；这不是该子代理没有待响应协作请求。",
        "load_errors": load_errors,
    }


def _can_run_for_collaboration_request(task: object) -> bool:
    unavailable_statuses = frozenset({
        TaskStatus.RUNNING.value,
        TaskStatus.TAKEN_OVER.value,
        TaskStatus.ABANDONED.value,
        TaskStatus.CANCELLED.value,
        TaskStatus.CHANNEL_ERROR.value,
        TaskStatus.TIMEOUT.value,
    })
    if task_status_in(getattr(task, "status", ""), unavailable_statuses):
        return False
    if str(getattr(task, "channel_status", "") or "") == "BROKEN":
        return False
    if any(getattr(item, "status", "") == "OPEN" for item in getattr(task, "capability_requests", []) or []):
        return False
    if any(getattr(item, "status", "") == "OPEN" for item in getattr(task, "capability_gaps", []) or []):
        return False
    return True


def collaboration_candidate_limit(agent, ctx, candidates: list, *, has_explicit_run_ids: bool) -> int:
    limit = max(0, int(getattr(ctx, "max_runners", 0) or 0))
    if not _should_expand_collaboration_limit(ctx, candidates, limit, has_explicit_run_ids):
        return limit
    cap = _collaboration_auto_dispatch_limit(getattr(agent, "config", None))
    if cap <= 0:
        return limit
    return max(limit, min(len(candidates), cap))


def _should_expand_collaboration_limit(ctx, candidates: list, limit: int, has_explicit_run_ids: bool) -> bool:
    return bool(
        getattr(ctx, "should_start_runners", False)
        and not has_explicit_run_ids
        and candidates
        and limit > 0
    )


def _collaboration_auto_dispatch_limit(config: object | None) -> int:
    raw = getattr(config, "collaboration_auto_dispatch_max_runners", 8)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 8
