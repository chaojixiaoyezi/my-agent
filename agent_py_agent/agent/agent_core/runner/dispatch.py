
from __future__ import annotations

"""selects runner candidates, handles retry policy, creates dispatch records, and runs worker agents.

dispatch 阶段不应该把"谁能跑、能不能重试、并发 worker 怎么启动"都塞在一个大函数里。
这个文件专门处理 runner 相关的规则和小工具。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ...settings.defaults import default_config_int
from ...settings.runtime_guard_config import runtime_guard_int
from ...subagents import SubAgentTask
from ...subagents.model_capabilities import capability_request_requires_parent_resolution
from ...subagents.models import (
    CAPABILITY_GRANTED_BLOCKER_FAILURE_TYPES,
    PROVIDER_SUPPLY_FAILURE_TYPES,
    RETRYABLE_RUNNER_FAILURE_TYPES,
    TaskStatus,
    VerificationStatus,
    known_failure_type,
    normalize_verification_status,
    task_has_status,
    task_status_in,
)
from ...subagents.recovery_eligibility import user_stopped_run_is_resumable
from ...subagents.runner_session_liveness import has_fresh_runner_session
from .dispatch_record import RunnerDispatchRecordParams
from .dispatch_record import runner_dispatch_record as _runner_dispatch_record
from .worker import RunSubagentWorkerParams, _run_subagent_worker

if TYPE_CHECKING:
    from ..core import SimpleAgent


@dataclass(frozen=True)
class RunnerCandidatePolicy:
    runner_max_attempts: int = 1
    same_run_redispatch_limit: int | None = None
    background_launch_id: str = ""


def candidate_policy(policy: RunnerCandidatePolicy | None = None) -> RunnerCandidatePolicy:
    return policy or RunnerCandidatePolicy()


def runner_launch_in_progress(task: object, policy: RunnerCandidatePolicy) -> bool:
    if _runner_active_attempt_id(task):
        return True
    return _background_start_active(task, background_launch_id=policy.background_launch_id)


def _runner_active_attempt_id(task: object) -> str:
    value = getattr(task, "runner_active_attempt_id", "")
    if not isinstance(value, str):
        return ""
    return value.strip()


# launching→running 是秒级过渡;记录冻在 launching(或 running 而线程宿主已死)超过
# 这个窗即视为宿主硬死亡残留,不再挡续派。比 supervision 周期(60s)宽,防误判慢启动。
_BACKGROUND_START_STALE_SECONDS = 180.0


def _background_start_active(task: object, *, background_launch_id: str = "") -> bool:
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return False
    background = attrs.get("background_start")
    if not isinstance(background, dict):
        return False
    status = str(background.get("status") or "").strip()
    if background_launch_id and str(background.get("launch_id") or "").strip() == str(background_launch_id).strip():
        return False
    if status not in {"launching", "running"}:
        return False
    return not _background_start_record_stale(task, background)


def _background_start_record_stale(task: object, background: dict) -> bool:
    """launching/running 记录是否为宿主硬死亡残留(P2 真机实锤:网关被杀时状态冻在
    launching/running,重启后该 run 被本判定永久排除——supervision 复活扫描也救不回,
    PENDING 卡死)。判据全结构化:pid 存活 > runner 会话心跳 > 记录时效,按证据强度取用;
    误判"在启"最多延迟一个 stale 窗被复活,误判"已死"由候选判定的 fresh-session 闸兜住双跑。"""
    try:
        pid = int(background.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 0:
        from ...subagents.process_control import is_pid_alive

        return not is_pid_alive(pid)
    if has_fresh_runner_session(task):
        return False
    try:
        updated_at = float(background.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        updated_at = 0.0
    if updated_at <= 0:
        return True  # 极老残留(权威构造一直写 updated_at):无从判活,按残留放行续派
    import time as _time

    return (_time.time() - updated_at) > _BACKGROUND_START_STALE_SECONDS


def _runner_max_attempts(policy: str, *, runtime_policy: object = None) -> int:

    if policy is None or str(policy).strip().lower() in {"", "auto"}:
        return runtime_guard_int("runner_failure_retry_limit", 2, policy=runtime_policy)
    value = str(policy).strip().lower()
    if value in {"", "auto"}:
        return runtime_guard_int("runner_failure_retry_limit", 2, policy=runtime_policy)
    if value in {"off", "0"}:
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

    return known_failure_type(getattr(task, "failure_type", ""))


# 临时供应类失败(模型 429 限流/断供/请求超时):环境故障
#   不是任务失败,不烧任务失败重试预算。真机实锤:默认闸(runner_failure_retry_limit=2 +
#   same_run_redispatch_limit=1)下,几分钟的额度断供把任务永久卡死 BLOCKED,额度恢复也不复活。
def _provider_supply_retry_limit(failure_type: str, *, runtime_policy: object = None) -> int:
    """供应类失败的独立同 run 重派上限(0=关闭特权,回归普通失败同闸)。只对供应类
    failure_type 解析配置——候选判定是每任务热路径,非供应任务零额外 IO。"""
    if failure_type not in PROVIDER_SUPPLY_FAILURE_TYPES:
        return 0
    return runtime_guard_int("provider_transient_redispatch_limit", 8, policy=runtime_policy)


def _runner_retry_reason(task: SubAgentTask, runner_max_attempts: int) -> str:

    if user_stopped_run_is_resumable(task):
        return "reason_code=conversation_user_stop; mode=same_run_resume"
    failure_type = _runner_failure_type(task)
    supply_limit = _provider_supply_retry_limit(failure_type)
    if runner_max_attempts == 1 and supply_limit <= 0:
        return ""
    retryable_statuses = frozenset({
        TaskStatus.BLOCKED.value,
        TaskStatus.FAILED.value,
        TaskStatus.TIMEOUT.value,
    })
    if not task_status_in(task.status, retryable_statuses):
        return ""
    if failure_type not in RETRYABLE_RUNNER_FAILURE_TYPES:
        return ""
    attempts = max(0, int(task.runner_attempts or 0))
    effective_max = runner_max_attempts
    if supply_limit > 0 and runner_max_attempts > 0:
        effective_max = max(runner_max_attempts, supply_limit)
    if effective_max > 0 and _retry_count_after_initial_attempt(attempts) >= effective_max:
        return ""
    max_attempts_label = "unlimited" if effective_max <= 0 else f"+{effective_max}"
    return f"failure_type={failure_type}; retry={_retry_count_after_initial_attempt(attempts) + 1}/{max_attempts_label}"


def _retry_count_after_initial_attempt(attempts: int) -> int:
    return max(0, int(attempts) - 1)


def _dispatch_patch_review_run_ids(tasks: list[SubAgentTask]) -> list[str]:
    run_ids: list[str] = []
    for task in tasks:
        if not task_has_status(task, TaskStatus.DONE):
            continue
        if _task_has_runner_patches(task):
            run_ids.append(task.id)
    return run_ids


def _task_has_runner_patches(task: SubAgentTask) -> bool:
    try:
        payload = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    return isinstance(payload.get("patches"), list) and bool(payload.get("patches"))


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
        if normalized in {"auto", "off"}:
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
    if not _source_worker_dispatch_allowed(task):
        return False
    if runner_launch_in_progress(task, effective_policy):
        return False
    if task_has_status(task, TaskStatus.RUNNING):
        return False
    # 耐久判活防双跑:run 若还有心跳新鲜的 runner 会话(可能在别的进程/agent 实例上跑,
    #   或被误 requeue 的"幽灵"runner 还没收尾),绝不再派第二个 runner 写同一工作区。
    if has_fresh_runner_session(task):
        return False
    if task_status_in(task.status, {TaskStatus.DONE.value, TaskStatus.CHANNEL_ERROR.value, TaskStatus.TAKEN_OVER.value}):
        return False
    if _task_verification_status(task) == VerificationStatus.VERIFIED.value:
        return False
    if task.channel_status == "BROKEN":
        return False
    if any(
        capability_request_requires_parent_resolution(getattr(item, "status", "OPEN"))
        for item in task.capability_requests
    ):
        return False
    if any(item.status == "OPEN" for item in task.capability_gaps):
        return False
    if task_has_status(task, TaskStatus.BLOCKED):
        if _blocked_after_capability_grant(task):
            return True
        return _can_retry_same_run(
            task,
            effective_policy.runner_max_attempts,
            effective_policy.same_run_redispatch_limit,
        )
    if task_status_in(task.status, {TaskStatus.FAILED.value, TaskStatus.TIMEOUT.value}):
        return _can_retry_same_run(
            task,
            effective_policy.runner_max_attempts,
            effective_policy.same_run_redispatch_limit,
        )
    # 续派候选兜底：PLANNING/PENDING 同为"待启动 runner"的可派工态(对齐 state_machine.DISPATCHABLE_STATES)。
    # PENDING 是子代理被 background 启动后 runner 进程被回收(orphan)留下的停滞孤儿态——原先只认 PLANNING,
    # 导致这类孤儿永不被续派、多子代理任务死循环卡死。正在启动中的 PENDING 已被本函数开头的
    # runner_launch_in_progress 排除,故这里只会捞起真正停滞的孤儿,不会重复派正在跑的。
    return task_status_in(task.status, {TaskStatus.PLANNING.value, TaskStatus.PENDING.value})


def _source_worker_dispatch_allowed(task: object) -> bool:
    """Fail closed before every runner dispatch when a typed Audit source job
    has no remaining durable work.

    函数用途：在统一 runner 候选入口读取来源岗位的持久生命周期；已清账、已关闭
    或状态不可读时一律不再启动，避免通用孤儿恢复器复活旧 Audit worker。
    """
    from ...common.audit_activation import (
        structured_audit_source_worker_attributes,
    )

    attrs = getattr(task, "attributes", {}) or {}
    if not structured_audit_source_worker_attributes(attrs):
        return True
    try:
        from ...ingestion.source_worker import source_worker_lifecycle_state

        return source_worker_lifecycle_state(task) == "active"
    except Exception:
        return False


def _task_verification_status(task: SubAgentTask) -> str:
    try:
        return normalize_verification_status(getattr(task, "verification_status", ""))
    except ValueError:
        return ""


def _can_retry_same_run(
    task: SubAgentTask,
    runner_max_attempts: int,
    same_run_redispatch_limit: int | None,
) -> bool:
    reason = _runner_retry_reason(task, runner_max_attempts)
    if not reason:
        return False
    limit = _same_run_redispatch_limit(same_run_redispatch_limit)
    supply_limit = _provider_supply_retry_limit(_runner_failure_type(task))
    if supply_limit > 0 and limit > 0:
        limit = max(limit, supply_limit)
    if limit <= 0:
        return True
    attempts = max(0, int(getattr(task, "runner_attempts", 0) or 0))
    return _retry_count_after_initial_attempt(attempts) < limit


def _blocked_after_capability_grant(task: SubAgentTask) -> bool:
    if not getattr(task, "capability_grants", None):
        return False
    if any(
        capability_request_requires_parent_resolution(getattr(item, "status", "OPEN"))
        for item in getattr(task, "capability_requests", []) or []
    ):
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
