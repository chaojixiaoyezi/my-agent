# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""selects runner candidates, handles retry policy, creates dispatch records, and runs worker agents.

dispatch 阶段不应该把"谁能跑、能不能重试、并发 worker 怎么启动"都塞在一个大函数里。
这个文件专门处理 runner 相关的规则和小工具。
"""

from typing import TYPE_CHECKING

from ..settings.runtime_guard_config import runtime_guard_int
from ..subagent import SubAgentTask
from .runner_candidate_policy import (
    RunnerCandidatePolicy,
    candidate_policy,
    runner_launch_in_progress,
)
from .runner_dispatch_record import RunnerDispatchRecordParams
from .runner_dispatch_record import runner_dispatch_record as _runner_dispatch_record
from .runner_patch_review import _dispatch_patch_review_run_ids, _task_has_runner_patches
from .runner_worker import RunSubagentWorkerParams, _run_subagent_worker

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

# LLM: _runner_max_attempts returns the configured retry budget after the first failed attempt.
# 函数用途: 解析 runner 失败后的补跑次数；0 表示不限制，旧 off/auto 字符串只做兼容入口。
def _runner_max_attempts(policy: str) -> int:

    if policy is None or str(policy).strip().lower() in {"", "auto"}:
        return runtime_guard_int("runner_failure_retry_limit", 2)
    value = str(policy).strip().lower()
    if value in {"", "auto"}:
        return runtime_guard_int("runner_failure_retry_limit", 2)
    if value in {"off", "none", "disabled", "false", "no"}:
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        return runtime_guard_int("runner_failure_retry_limit", 2)


# LLM: _same_run_redispatch_limit reads the same-run retry cap from shared runtime guard config.
# 函数用途: 限制同一个 run_id 失败后被反复派发的次数；0 表示不限制。
def _same_run_redispatch_limit(value: object = None) -> int:
    if value is None:
        value = runtime_guard_int("same_run_redispatch_limit", 1)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return runtime_guard_int("same_run_redispatch_limit", 1)


# LLM: _runner_failure_type 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器失败type的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _runner_failure_type(task: SubAgentTask) -> str:

    return str(task.failure_type or "").strip().lower()


# LLM: _runner_retry_reason 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器retryreason的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
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


# LLM: _retry_count_after_initial_attempt makes retry limits count re-runs, not total runner attempts.
# 函数用途: runner_attempts 包含首次执行；补跑预算只统计首次失败后的重新派发次数。
def _retry_count_after_initial_attempt(attempts: int) -> int:
    return max(0, int(attempts) - 1)


# LLM: _resolve_runner_concurrency 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 读取或查询执行器concurrency需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
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


# LLM: _runner_auto_concurrency_limit resolves "auto" runner width from AgentConfig.
# 函数用途: 读取 runner_auto_concurrency；0 表示按本批 job 数执行，不在调度层另藏默认并发。
def _runner_auto_concurrency_limit(value: object, job_count: int) -> int:
    if value is None:
        from ..settings.config import AgentConfig

        value = AgentConfig().runner_auto_concurrency
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = job_count
    if limit <= 0:
        return job_count
    return min(job_count, limit)


# LLM: _resolve_runner_start_rate 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 读取或查询执行器startrate需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
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


# LLM: _resolve_runner_timeout_seconds 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 读取或查询执行器超时seconds需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
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


# LLM: _dispatch_runner_candidates 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器candidates的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
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


# LLM: _limit_items 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 计算限制条目的预算、数量或限制，影响后续调度节奏；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _limit_items(items: list, limit: int) -> list:

    if limit <= 0:
        return list(items)
    return list(items)[:limit]


# LLM: _is_dispatch_runner_candidate 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 判断执行器candidate条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
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


# LLM: _can_retry_same_run applies global retry and same-run redispatch budgets as soft parent-facing caps.
# 函数用途: 达到同 run 重派限制时不再自动选择该 run，交给父代理换策略或显式恢复。
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


# LLM: granted capability blockers should rerun once the parent has routed the request.
# 函数用途: 能力申请已被父级授权且没有未处理 OPEN 请求时，允许 blocked runner 再跑一轮，避免“纸面授权但任务卡死”。
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


# LLM: _has_fresh_capability_grant makes rerun eligibility a state contract, not a failure-word allowlist.
# 函数用途: 父级在上次 runner 后新增授权时，允许同一个 BLOCKED run 续跑一次；续跑后不再凭旧 grant 无限重跑。
def _has_fresh_capability_grant(task: SubAgentTask) -> bool:
    last_attempt = _float_attr(task, "runner_last_attempt_at")
    if last_attempt <= 0:
        return False
    for grant in getattr(task, "capability_grants", []) or []:
        if _float_attr(grant, "created_at") > last_attempt:
            return True
    return False


# LLM: _float_attr keeps timestamp comparisons tolerant of old task/grant records.
# 函数用途: 读取旧记录中可能为空或字符串的时间戳；坏值按 0 处理，不让调度崩溃。
def _float_attr(value: object, name: str) -> float:
    try:
        return float(getattr(value, name, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
