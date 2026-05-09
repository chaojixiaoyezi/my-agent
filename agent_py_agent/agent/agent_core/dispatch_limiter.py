# LLM: Dispatch limiter centralizes runner start budgets before workers are launched.
# 模块用途: 统一计算本轮 runner 可以启动哪些任务，为并发、start-rate 和后续按角色限流提供稳定入口。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# LLM: RunnerJobLimitRequest is the bundle for runner start limiting.
# 类用途: 保存候选 runner jobs、start-rate、当前 active 数和可选 role 限额。
@dataclass(frozen=True)
class RunnerJobLimitRequest:
    jobs: list[Any]
    runner_start_rate: int = 0
    active_count: int = 0
    role_limits: dict[str, int] = field(default_factory=dict)


# LLM: RunnerJobLimitResult explains both allowed jobs and why jobs were held back.
# 类用途: 返回可启动任务、被拦截数量和原因，方便调度报告后续扩展。
@dataclass(frozen=True)
class RunnerJobLimitResult:
    allowed_jobs: list[Any]
    blocked_count: int
    reason: str
    role_counts: dict[str, int] = field(default_factory=dict)


# LLM: limit_runner_jobs applies global start-rate first, then optional role budgets.
# 函数用途: 从 pending runner jobs 中选出本轮可启动的子集；不执行 runner、不修改任务。
def limit_runner_jobs(request: RunnerJobLimitRequest) -> RunnerJobLimitResult:
    jobs = _apply_start_rate(request.jobs, request.runner_start_rate, request.active_count)
    start_rate_limited = len(jobs) < len(request.jobs)
    if request.role_limits:
        jobs, role_counts = _apply_role_limits(jobs, request.role_limits)
    else:
        role_counts = _role_counts(jobs)
    reason = _limit_reason(start_rate_limited, len(jobs) < len(request.jobs), bool(request.role_limits))
    return RunnerJobLimitResult(
        allowed_jobs=jobs,
        blocked_count=max(0, len(request.jobs) - len(jobs)),
        reason=reason,
        role_counts=role_counts,
    )


# LLM: _limit_reason explains whether global start-rate, role budget, both, or neither applied.
# 函数用途: 生成限流结果原因，避免主入口出现深层条件分支。
def _limit_reason(start_rate_limited: bool, any_limited: bool, has_role_limits: bool) -> str:
    if start_rate_limited and has_role_limits and any_limited:
        return "limited_by_start_rate_and_role_budget"
    if start_rate_limited:
        return "limited_by_start_rate"
    if any_limited and has_role_limits:
        return "limited_by_role_budget"
    return "not_limited"


# LLM: _apply_start_rate keeps legacy runner_start_rate semantics but reserves active_count support.
# 函数用途: 根据本轮 start-rate 和已 active 数量截断候选任务。
def _apply_start_rate(jobs: list[Any], runner_start_rate: int, active_count: int) -> list[Any]:
    if runner_start_rate <= 0:
        return list(jobs)
    capacity = max(0, runner_start_rate - max(0, active_count))
    return list(jobs[:capacity])


# LLM: _apply_role_limits filters jobs in stable order by role-specific budgets.
# 函数用途: 按 role 限制每轮启动数量，保留原候选顺序。
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


# LLM: _role_counts summarizes allowed jobs by their task role.
# 函数用途: 生成限制结果中的 role 统计，供后续报告展示。
def _role_counts(jobs: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for job in jobs:
        role = _job_role(job)
        counts[role] = counts.get(role, 0) + 1
    return counts


# LLM: _job_role understands the current (run_id, task, retry_reason) runner job tuple shape.
# 函数用途: 从 runner job 中提取 role；无法识别时归为 general。
def _job_role(job: Any) -> str:
    task = job[1] if isinstance(job, tuple) and len(job) > 1 else None
    return str(getattr(task, "role", "") or "general")
