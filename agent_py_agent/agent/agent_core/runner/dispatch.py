# LLM: 统一 runner 候选门禁和重试计数，并提供并发、启动速率、超时的解析辅助；部署重派上限消费传入的策略快照。
#   候选判断只读取任务、心跳及必要的来源状态；本模块同时导出委托 worker 和派工记录的入口，实际启动可能有副作用。
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
from ...settings.runtime_guard_config import RuntimeGuardPolicy, runtime_guard_int
from ...subagents import SubAgentTask
from ...subagents.model_capabilities import capability_request_requires_parent_resolution
from ...subagents.models import (
    CAPABILITY_GRANTED_BLOCKER_FAILURE_TYPES,
    PROVIDER_SUPPLY_FAILURE_TYPES,
    RETRYABLE_RUNNER_FAILURE_TYPES,
    SUBAGENT_RETRYABLE_FAILURE_STATUSES,
    TaskStatus,
    known_failure_type,
    task_has_status,
    task_status_in,
)
from ...subagents.recovery_eligibility import user_stopped_run_is_resumable
from ...subagents.runner_session_liveness import has_fresh_runner_session
from ...subagents.services.recovery.strategy import (
    SubagentRecoveryStrategyRequest,
    _no_progress_fuse,
    recovery_count,
)
from .dispatch_record import RunnerDispatchRecordParams
from .dispatch_record import runner_dispatch_record as _runner_dispatch_record
from .worker import RunSubagentWorkerParams, _run_subagent_worker

if TYPE_CHECKING:
    from ..core import SimpleAgent


# LLM: runner_retry_limit 是普通失败后还能自动重跑几次（0 = 不重跑）；只有调度入口按配置
#   runner_failure_retry_limit传入；no_progress_attempt_limit由生产调用方传能力配置，0关闭，None仅兼容纯函数策略默认值。
# 类用途: 保存一次候选挑选的普通重试上限、后台启动标识、部署守卫策略快照与无进展上限。
@dataclass(frozen=True)
class RunnerCandidatePolicy:
    runner_retry_limit: int = 0
    background_launch_id: str = ""
    runtime_guard_policy: RuntimeGuardPolicy | None = None
    no_progress_attempt_limit: int | None = None


# LLM: 已传入的策略原样复用；未传入时构造不允许普通失败重跑的默认策略，不读取任务或启动 runner。
# 函数用途: 为所有候选门禁调用提供非空的类型化重试策略。
def candidate_policy(policy: RunnerCandidatePolicy | None = None) -> RunnerCandidatePolicy:
    return policy or RunnerCandidatePolicy()


# LLM: 非空活动执行轮立即视为已占用；否则检查排除本批 launch 的后台启动记录，防止重复派工。
# 函数用途: 为统一候选门禁判断任务是否已有执行轮或后台启动在途。
def runner_launch_in_progress(task: object, policy: RunnerCandidatePolicy) -> bool:
    if _runner_active_attempt_id(task):
        return True
    return _background_start_active(task, background_launch_id=policy.background_launch_id)


# LLM: 仅接受字符串类型的活动执行轮 ID 并剥离首尾空白；缺失或异常类型视为空，不修改任务。
# 函数用途: 为启动占用检查读取标准化的活动执行轮标识。
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


# LLM: 使用模型层权威归一化规则解释 failure_type；不从错误文案猜测失败类别，也不回写任务。
# 函数用途: 为自动重跑和能力授权后恢复判断提供标准失败类型。
def _runner_failure_type(task: SubAgentTask) -> str:

    return known_failure_type(getattr(task, "failure_type", ""))


# 临时供应类失败(模型 429 限流/断供/请求超时):环境故障
#   不是任务失败,不烧任务失败重试预算。真机实锤:只按普通失败的重跑次数算时,
#   几分钟的额度断供把任务永久卡死 BLOCKED,额度恢复也不复活。
# LLM: 非供应类失败直接返回零，避免热路径读取部署额度；供应失败从传入策略解析独立同 run 重派上限。
# 函数用途: 为重试计数门禁补充临时供应故障的独立额度。
def _provider_supply_retry_limit(failure_type: str, *, runtime_policy: object = None) -> int:
    """供应类失败的独立同 run 重派上限(0=关闭特权,回归普通失败同闸)。只对供应类
    failure_type 解析配置——候选判定是每任务热路径,非供应任务零额外 IO。"""
    if failure_type not in PROVIDER_SUPPLY_FAILURE_TYPES:
        return 0
    return runtime_guard_int("provider_transient_redispatch_limit", 8, policy=runtime_policy)


# LLM: 读取统一验证后的计数，坏账即拒绝，干净旧记录保守迁移；正常切片不耗失败额度，0仅关闭无进展熔断。
# 函数用途: 判断一个失败的run还有没有真实重试额度。
def _runner_retry_reason(
    task: SubAgentTask,
    runner_retry_limit: int,
    *,
    runtime_policy: RuntimeGuardPolicy | None = None,
    no_progress_attempt_limit: int | None = None,
) -> str:

    if user_stopped_run_is_resumable(task):
        return "reason_code=conversation_user_stop; mode=same_run_resume"
    failure_type = _runner_failure_type(task)
    if (recovery_count(task, "recovery_history_unknown_count") or 0) > 0:
        return ""
    effective_max = max(
        max(0, int(runner_retry_limit or 0)),
        _provider_supply_retry_limit(failure_type, runtime_policy=runtime_policy),
    )
    limit = (SubagentRecoveryStrategyRequest(task).no_progress_attempt_limit
             if no_progress_attempt_limit is None else no_progress_attempt_limit)
    if _no_progress_fuse(task, limit):
        return ""
    if not task_status_in(task.status, SUBAGENT_RETRYABLE_FAILURE_STATUSES):
        return ""
    if failure_type == "host_shutdown_interrupted" and getattr(task, "_extra_fields", {}).get("recovery_safe_resume") is True:
        return "reason_code=host_shutdown_interrupted; mode=same_run_resume"
    if failure_type not in RETRYABLE_RUNNER_FAILURE_TYPES:
        return ""
    counter = "provider_retry_count" if failure_type in PROVIDER_SUPPLY_FAILURE_TYPES else "failure_retry_count"
    retries = recovery_count(task, counter)
    if retries is None or (recovery_count(task, "recovery_history_unknown_count") or 0) > 0:
        return ""
    if retries >= effective_max:
        return ""
    return f"failure_type={failure_type}; retry={retries + 1}/+{effective_max}"


# LLM: 只检查已完成任务的输出补丁，按输入顺序返回 ID；可能读取输出 JSON，但不审阅、应用补丁或改状态。
# 函数用途: 为派工补丁审阅阶段收集有非空补丁列表的已完成子代理。
def _dispatch_patch_review_run_ids(tasks: list[SubAgentTask]) -> list[str]:
    run_ids: list[str] = []
    for task in tasks:
        if not task_has_status(task, TaskStatus.DONE):
            continue
        if _task_has_runner_patches(task):
            run_ids.append(task.id)
    return run_ids


# LLM: 读取任务输出 JSON 并检查 patches 是否为非空列表；文件、解码或路径类型错误返回假，其他结构错误不在此兜底。
# 函数用途: 为审阅候选筛选判断任务输出是否声明了 runner 补丁。
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


# LLM: 无工作时为零，auto、空串或转换失败时允许本批全部；其他数值夹在零与任务数之间，不实际启动工作。
# 函数用途: 将启动速率配置解析为本批可放行的工作数量。
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


# LLM: auto、off 或无法解析的值返回零，其他值转非负浮点数；这里只解释配置，不设置计时器或终止进程。
# 函数用途: 为 runner 执行配置解析超时秒数。
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


# LLM: 非正数量上限返回空列表；其余按输入顺序调用统一门禁筛选后截取，不启动任务或增加重试次数。
# 函数用途: 为正式派工与待跑统计提供相同策略下的 runner 候选列表。
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


# LLM: 始终返回新列表；非正限额保留全部条目，正限额保留前缀，不修改原列表或其中对象。
# 函数用途: 为派工调用方提供保序且不回写输入的通用列表限额处理。
def _limit_items(items: list, limit: int) -> list:

    if limit <= 0:
        return list(items)
    return list(items)[:limit]


# LLM: 唯一候选门先拒坏恢复账，再按显式配置检查无进展熔断；真实等孩子不是孤儿，不读正文/进度猜资格。
# 函数用途: 判断一个子代理 run 能否被启动或恢复，并排除正常等孩子的父级。
def _is_dispatch_runner_candidate(
    task: SubAgentTask,
    *,
    policy: RunnerCandidatePolicy | None = None,
) -> bool:

    effective_policy = candidate_policy(policy)
    if (recovery_count(task, "recovery_history_unknown_count") or 0) > 0:
        return False
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
        return bool(
            _runner_retry_reason(
                task,
                effective_policy.runner_retry_limit,
                runtime_policy=effective_policy.runtime_guard_policy,
                no_progress_attempt_limit=effective_policy.no_progress_attempt_limit,
            )
        )
    if task_status_in(task.status, {TaskStatus.FAILED.value, TaskStatus.TIMEOUT.value}):
        return bool(
            _runner_retry_reason(
                task,
                effective_policy.runner_retry_limit,
                runtime_policy=effective_policy.runtime_guard_policy,
                no_progress_attempt_limit=effective_policy.no_progress_attempt_limit,
            )
        )
    # 续派候选兜底：PLANNING/PENDING 同为"待启动 runner"的可派工态(对齐 state_machine.DISPATCHABLE_STATES)。
    # PENDING 是子代理被 background 启动后 runner 进程被回收(orphan)留下的停滞孤儿态——原先只认 PLANNING,
    # 导致这类孤儿永不被续派、多子代理任务死循环卡死。正在启动中的 PENDING 已被本函数开头的
    # runner_launch_in_progress 排除,故这里只会捞起真正停滞的孤儿,不会重复派正在跑的。
    return task_status_in(task.status, {TaskStatus.PLANNING.value, TaskStatus.PENDING.value})


# LLM: 普通任务直接放行；结构化 Audit 来源岗位必须读到 active 持久生命周期，读取异常也拒绝，不复活已清账工作。
# 函数用途: 在统一候选入口核验来源 worker 是否仍有允许执行的持久工作。
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


# LLM: 至少已有授权且不存在待父级处理的申请或开放缺口时，才按新授权时间或指定能力阻塞失败类型放行。
# 函数用途: 为阻塞任务识别能力授予后可恢复派工的情形。
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


# LLM: 上次尝试时间必须为正，且至少一项授权的创建时间严格晚于它；只比较时间，不消耗授权或修改状态。
# 函数用途: 判断任务上次运行之后是否获得了新的能力授权。
def _has_fresh_capability_grant(task: SubAgentTask) -> bool:
    last_attempt = _float_attr(task, "runner_last_attempt_at")
    if last_attempt <= 0:
        return False
    for grant in getattr(task, "capability_grants", []) or []:
        if _float_attr(grant, "created_at") > last_attempt:
            return True
    return False


# LLM: 缺失或空值按零解析，类型和数值转换错误也返回零；不读取文件，不回写属性。
# 函数用途: 为授权新鲜度判断读取可比较的时间戳属性。
def _float_attr(value: object, name: str) -> float:
    try:
        return float(getattr(value, name, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
