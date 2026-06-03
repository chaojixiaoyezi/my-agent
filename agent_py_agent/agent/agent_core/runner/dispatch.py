
from __future__ import annotations

"""selects runner candidates, handles retry policy, creates dispatch records, and runs worker agents.

dispatch 阶段不应该把"谁能跑、能不能重试、并发 worker 怎么启动"都塞在一个大函数里。
这个文件专门处理 runner 相关的规则和小工具。
"""

from typing import TYPE_CHECKING

from ...settings.defaults import default_config_int
from ...settings.runtime_guard_config import runtime_guard_int
from ...subagent import SubAgentTask
from .candidate_policy import (
    RunnerCandidatePolicy,
    candidate_policy,
    runner_launch_in_progress,
)
from .dispatch_record import RunnerDispatchRecordParams
from .dispatch_record import runner_dispatch_record as _runner_dispatch_record
from .patch_review import _dispatch_patch_review_run_ids, _task_has_runner_patches
from .worker import RunSubagentWorkerParams, _run_subagent_worker

if TYPE_CHECKING:
    from ..core import SimpleAgent


RETRYABLE_RUNNER_FAILURE_TYPES = {
    "runner_error",
    "structured_output_parse_error",
    "tool_result_missing",
    "model_error",
    "api_error",
    "transient_error",
    "provider_timeout",
    "runner_timeout",
}

CAPABILITY_GRANTED_BLOCKER_FAILURE_TYPES = {
    "capability_request",
    "permission_blocked",
    "missing_capability",
    "write_permission_blocked",
}

def _runner_max_attempts(policy: str, *, runtime_policy: object = None) -> int:

    if policy is None or str(policy).strip().lower() in {"", "auto"}:
        return runtime_guard_int("runner_failure_retry_limit", 2, policy=runtime_policy)
    value = str(policy).strip().lower()
    if value in {"", "auto"}:
        return runtime_guard_int("runner_failure_retry_limit", 2, policy=runtime_policy)
    if value in {"off", "none", "disabled", "false", "no"}:
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        return runtime_guard_int("runner_failure_retry_limit", 2, policy=runtime_policy)


def _same_run_redispatch_limit(value: object = None, *, runtime_policy: object = None) -> int:
    if value is None:
        value = runtime_guard_int("same_run_redispatch_limit", 1, policy=runtime_policy)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return runtime_guard_int("same_run_redispatch_limit", 1, policy=runtime_policy)


def _runner_failure_type(task: SubAgentTask) -> str:

    return str(task.failure_type or "").strip().lower()


def _runner_retry_reason(task: SubAgentTask, runner_max_attempts: int) -> str:

    if runner_max_attempts == 1:
        return ""
    if task.status not in {"BLOCKED", "FAILED", "TIMEOUT"}:
        return ""
    failure_type = _runner_failure_type(task)
    if failure_type not in RETRYABLE_RUNNER_FAILURE_TYPES:
        return ""
    attempts = max(0, int(task.runner_attempts or 0))
    if runner_max_attempts > 0 and _retry_count_after_initial_attempt(attempts) >= runner_max_attempts:
        return ""
    max_attempts_label = "unlimited" if runner_max_attempts <= 0 else f"+{runner_max_attempts}"
    return f"failure_type={failure_type}; retry={_retry_count_after_initial_attempt(attempts) + 1}/{max_attempts_label}"


def _retry_count_after_initial_attempt(attempts: int) -> int:
    return max(0, int(attempts) - 1)


def _resolve_runner_concurrency(value: object, job_count: int, *, auto_limit: object = None) -> int:

    if job_count <= 0:
        return 0
    configured_auto_limit = _runner_auto_concurrency_limit(auto_limit, job_count)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "auto"}:
            return configured_auto_limit
        try:
            parsed = int(normalized)
        except ValueError:
            return configured_auto_limit
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return configured_auto_limit
    return max(1, min(parsed, job_count))


def _runner_auto_concurrency_limit(value: object, job_count: int) -> int:
    if value is None:
        value = default_config_int("runner_auto_concurrency")
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = job_count
    if limit <= 0:
        return job_count
    return min(job_count, limit)


def _resolve_runner_start_rate(value: object, job_count: int) -> int:

    if job_count <= 0:
        return 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "auto"}:
            return job_count
        try:
            parsed = int(normalized)
        except ValueError:
            return job_count
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return job_count
    return max(0, min(parsed, job_count))


def _resolve_runner_timeout_seconds(value: object) -> float:

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "auto", "off", "none", "disabled", "false", "no"}:
            return 0.0
        try:
            parsed = float(normalized)
        except ValueError:
            return 0.0
    else:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.0
    return max(0.0, parsed)


def _dispatch_runner_candidates(
    tasks: list[SubAgentTask],
    max_runners: int,
    *,
    policy: RunnerCandidatePolicy | None = None,
) -> list[SubAgentTask]:

    if max_runners <= 0:
        return []
    effective_policy = candidate_policy(policy)
    candidates: list[SubAgentTask] = []
    for task in tasks:
        if not _is_dispatch_runner_candidate(task, policy=effective_policy):
            continue
        candidates.append(task)
    return candidates[:max_runners]


def _limit_items(items: list, limit: int) -> list:

    if limit <= 0:
        return list(items)
    return list(items)[:limit]


def _is_dispatch_runner_candidate(
    task: SubAgentTask,
    *,
    policy: RunnerCandidatePolicy | None = None,
) -> bool:

    effective_policy = candidate_policy(policy)
    if runner_launch_in_progress(task, effective_policy):
        return False
    if task.status == "RUNNING":
        return False
    if task.status in {
        "DONE",
        "CHANNEL_ERROR",
        "TAKEN_OVER",
    }:
        return False
    if task.verification_status == "VERIFIED":
        return False
    if task.channel_status == "BROKEN":
        return False
    if any(item.status == "OPEN" for item in task.capability_requests):
        return False
    if any(item.status == "OPEN" for item in task.capability_gaps):
        return False
    if task.status == "BLOCKED":
        if _blocked_after_capability_grant(task):
            return True
        return _can_retry_same_run(
            task,
            effective_policy.runner_max_attempts,
            effective_policy.same_run_redispatch_limit,
        )
    if task.status in {"FAILED", "TIMEOUT"}:
        return _can_retry_same_run(
            task,
            effective_policy.runner_max_attempts,
            effective_policy.same_run_redispatch_limit,
        )
    return task.status == "PLANNING"


def _can_retry_same_run(
    task: SubAgentTask,
    runner_max_attempts: int,
    same_run_redispatch_limit: int | None,
) -> bool:
    reason = _runner_retry_reason(task, runner_max_attempts)
    if not reason:
        return False
    limit = _same_run_redispatch_limit(same_run_redispatch_limit)
    if limit <= 0:
        return True
    attempts = max(0, int(getattr(task, "runner_attempts", 0) or 0))
    return _retry_count_after_initial_attempt(attempts) < limit


def _blocked_after_capability_grant(task: SubAgentTask) -> bool:
    if not getattr(task, "capability_grants", None):
        return False
    if any(item.status == "OPEN" for item in getattr(task, "capability_requests", []) or []):
        return False
    if any(item.status == "OPEN" for item in getattr(task, "capability_gaps", []) or []):
        return False
    if _has_fresh_capability_grant(task):
        return True
    return _runner_failure_type(task) in CAPABILITY_GRANTED_BLOCKER_FAILURE_TYPES


def _has_fresh_capability_grant(task: SubAgentTask) -> bool:
    last_attempt = _float_attr(task, "runner_last_attempt_at")
    if last_attempt <= 0:
        return False
    for grant in getattr(task, "capability_grants", []) or []:
        if _float_attr(grant, "created_at") > last_attempt:
            return True
    return False


def _float_attr(value: object, name: str) -> float:
    try:
        return float(getattr(value, name, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
