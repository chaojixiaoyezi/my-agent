# LLM: runner 候选闸、重跑次数（配置 runner_failure_retry_limit）、并发（runner_concurrency）与启动速率的解析都只在这里；
#   调用方传入数值，不在别处复制默认值。候选判断只读，worker 启动函数有副作用。
# 模块用途: 决定哪些子代理 run 能启动或重跑、同时跑几个，并提供派工记录与 worker 启动入口。
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
    known_failure_type,
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


# LLM: runner_retry_limit 是普通失败后还能自动重跑几次（0 = 不重跑）；只有调度入口按配置
#   runner_failure_retry_limit 传入，规划、能力清扫等默认策略只接续不重跑。临时供应失败另走独立上限。
# 类用途: 一次挑选 runner 候选时用到的重跑上限和本次后台启动标识。
@dataclass(frozen=True)
class RunnerCandidatePolicy:
    runner_retry_limit: int = 0
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


# runner_concurrency 为 auto（或留空）时的并发上限，与单个根会话 8 个子代理槽位一致。
_DEFAULT_RUNNER_CONCURRENCY_COUNT = 8


# LLM: 派工候选的“正在启动”判定：本批自己的 launch 不算；launching/running 记录是否已成宿主残留只问
#   subagents.process_control.background_start_record_stale（与 existing_runner_launch 同一权威）。只读。
# 函数用途: 判断这个任务是不是还有一个活着的后台启动在路上，有就不再重复派工。
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
    # 过期判定的唯一权威在 subagents.process_control（重复投递复用也调它）；运行时取模块属性，便于 harness 替换。
    from ...subagents import process_control

    return not process_control.background_start_record_stale(task, background)


# LLM: 唯一的家是 AgentConfig.runner_failure_retry_limit（原 runner_failure_policy 与守卫文件里的
#   runner_failure_retry_limit / same_run_redispatch_limit 已并入）：正整数 = 最多重跑几次，0 = 不自动重跑；
#   布尔、负数或乱填按 AgentConfig 默认值。只读配置，不改任务。
# 函数用途: 读出子代理 runner 普通失败后最多自动重跑几次，交给调度入口组装候选策略。
def runner_failure_retry_limit(config: object) -> int:
    default = default_config_int("runner_failure_retry_limit")
    value = getattr(config, "runner_failure_retry_limit", default)
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _runner_failure_type(task: SubAgentTask) -> str:

    return known_failure_type(getattr(task, "failure_type", ""))


# 临时供应类失败(模型 429 限流/断供/请求超时):环境故障
#   不是任务失败,不烧任务失败重试预算。真机实锤:只按普通失败的重跑次数算时,
#   几分钟的额度断供把任务永久卡死 BLOCKED,额度恢复也不复活。
def _provider_supply_retry_limit(failure_type: str, *, runtime_policy: object = None) -> int:
    """供应类失败的独立同 run 重派上限(0=关闭特权,回归普通失败同闸)。只对供应类
    failure_type 解析配置——候选判定是每任务热路径,非供应任务零额外 IO。"""
    if failure_type not in PROVIDER_SUPPLY_FAILURE_TYPES:
        return 0
    return runtime_guard_int("provider_transient_redispatch_limit", 8, policy=runtime_policy)


# LLM: 这是同一 run 自动重跑的唯一计数闸：已重跑次数（runner_attempts - 1）达到上限即停；
#   临时供应失败取 max(普通上限, provider_transient_redispatch_limit)。返回空串表示不重跑，非空串只作记录标签。
# 函数用途: 判断一个失败的 run 还能不能自动再跑一次，能的话给出“第几次重跑”的说明。
def _runner_retry_reason(task: SubAgentTask, runner_retry_limit: int) -> str:

    if user_stopped_run_is_resumable(task):
        return "reason_code=conversation_user_stop; mode=same_run_resume"
    failure_type = _runner_failure_type(task)
    effective_max = max(max(0, int(runner_retry_limit or 0)), _provider_supply_retry_limit(failure_type))
    retryable_statuses = frozenset({
        TaskStatus.BLOCKED.value,
        TaskStatus.FAILED.value,
        TaskStatus.TIMEOUT.value,
    })
    if not task_status_in(task.status, retryable_statuses):
        return ""
    if failure_type not in RETRYABLE_RUNNER_FAILURE_TYPES:
        return ""
    retries = _retry_count_after_initial_attempt(max(0, int(task.runner_attempts or 0)))
    if retries >= effective_max:
        return ""
    return f"failure_type={failure_type}; retry={retries + 1}/+{effective_max}"


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


# LLM: runner_concurrency 是唯一并发旋钮（原 runner_auto_concurrency 已并入）：auto/空 = 默认 8，正整数 = 上限，
#   0 = 不限制（本批全部同时跑）；布尔、负数或乱填按 auto。结果不超过本批任务数，只算数，不启动线程。
# 函数用途: 算出本次调度批次最多同时跑几个 runner。
def _resolve_runner_concurrency(value: object, job_count: int) -> int:

    if job_count <= 0:
        return 0
    limit = _runner_concurrency_setting(value)
    return job_count if limit == 0 else min(limit, job_count)


# LLM: 只解析数值，不看任务；非法值一律回到默认 8，不能把乱填解释成“不限制”。
# 函数用途: 把 runner_concurrency 的配置值换成并发上限，0 表示不限制。
def _runner_concurrency_setting(value: object) -> int:
    text = "" if value is None or isinstance(value, bool) else str(value).strip().lower()
    if text in {"", "auto"}:
        return _DEFAULT_RUNNER_CONCURRENCY_COUNT
    try:
        parsed = int(text)
    except ValueError:
        return _DEFAULT_RUNNER_CONCURRENCY_COUNT
    return parsed if parsed >= 0 else _DEFAULT_RUNNER_CONCURRENCY_COUNT


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


# LLM: This is the single runner candidate gate. A typed direct-child wait is
# an intentional suspension, not an orphan; prose and task-progress never enter.
# 函数用途: 判断一个子代理 run 能否被启动或恢复，并排除正常等孩子的父级。
def _is_dispatch_runner_candidate(
    task: SubAgentTask,
    *,
    policy: RunnerCandidatePolicy | None = None,
) -> bool:

    effective_policy = candidate_policy(policy)
    if not _source_worker_dispatch_allowed(task):
        return False
    from ...subagents.direct_parent_lifecycle import parent_wait_blocks_dispatch

    if parent_wait_blocks_dispatch(task):
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
        return bool(_runner_retry_reason(task, effective_policy.runner_retry_limit))
    if task_status_in(task.status, {TaskStatus.FAILED.value, TaskStatus.TIMEOUT.value}):
        return bool(_runner_retry_reason(task, effective_policy.runner_retry_limit))
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
