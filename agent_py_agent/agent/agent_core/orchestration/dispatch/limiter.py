
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RunnerJobLimitResult:
    allowed_jobs: list[Any]
    blocked_count: int
    reason: str
    role_counts: dict[str, int] = field(default_factory=dict)


def limit_runner_jobs(
    jobs: list[Any],
    *,
    runner_start_rate: int = 0,
    active_count: int = 0,
    role_limits: dict[str, int] | None = None,
) -> RunnerJobLimitResult:
    original_jobs = list(jobs)
    jobs = _apply_start_rate(original_jobs, runner_start_rate, active_count)
    start_rate_limited = len(jobs) < len(original_jobs)
    if role_limits:
        jobs, role_counts = _apply_role_limits(jobs, role_limits)
    else:
        role_counts = _role_counts(jobs)
    reason = _limit_reason(start_rate_limited, len(jobs) < len(original_jobs), bool(role_limits))
    return RunnerJobLimitResult(
        allowed_jobs=jobs,
        blocked_count=max(0, len(original_jobs) - len(jobs)),
        reason=reason,
        role_counts=role_counts,
    )


def _limit_reason(start_rate_limited: bool, any_limited: bool, has_role_limits: bool) -> str:
    if start_rate_limited and has_role_limits and any_limited:
        return "limited_by_start_rate_and_role_budget"
    if start_rate_limited:
        return "limited_by_start_rate"
    if any_limited and has_role_limits:
        return "limited_by_role_budget"
    return "not_limited"


def _apply_start_rate(jobs: list[Any], runner_start_rate: int, active_count: int) -> list[Any]:
    if runner_start_rate <= 0:
        return list(jobs)
    capacity = max(0, runner_start_rate - max(0, active_count))
    return list(jobs[:capacity])


def _apply_role_limits(jobs: list[Any], role_limits: dict[str, int]) -> tuple[list[Any], dict[str, int]]:
    allowed: list[Any] = []
    counts: dict[str, int] = {}
    normalized_limits = {str(role): max(0, int(limit)) for role, limit in role_limits.items()}
    for job in jobs:
        role = _job_role(job)
        current = counts.get(role, 0)
        limit = normalized_limits.get(role)
        if limit is not None and current >= limit:
            continue
        allowed.append(job)
        counts[role] = current + 1
    return allowed, counts


def _role_counts(jobs: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for job in jobs:
        role = _job_role(job)
        counts[role] = counts.get(role, 0) + 1
    return counts


def _job_role(job: Any) -> str:
    task = job[1] if isinstance(job, tuple) and len(job) > 1 else None
    return str(getattr(task, "role", "") or "general")
