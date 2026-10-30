# LLM: 普通候选沿runner/dispatch唯一闸，显式恢复回退同样校验canonical计数；用户停止不覆盖坏账，读取不授实际启动权。只读。
# 模块用途: 按派工上下文（显式 include、恢复指令或全部任务）挑出可以启动或重跑的子代理 run。
from __future__ import annotations

from ....subagents.models import (
    FailureType,
    TaskStatus,
    known_failure_type,
    task_status_in,
)
from ....subagents.recovery_eligibility import user_stopped_run_is_resumable
from ....subagents.services.recovery.modes import is_rerun_mode, recovery_mode_from_protocol_value
from ....subagents.services.recovery.strategy import recovery_count
from ...runner.dispatch import (
    RunnerCandidatePolicy,
    _dispatch_runner_candidates,
    _is_dispatch_runner_candidate,
    _runner_retry_reason,
)
from .params import DispatchContext
from .runner_selection import requested_include_ids


# LLM: runner_retry_limit 来自配置 runner_failure_retry_limit（普通失败后最多重跑几次，0 = 不重跑）；
#   无进展阈值由生产调用方传能力配置，0关闭；显式include恢复另走原判定，不启动runner。
# 函数用途: 按本次派工上下文挑出可以启动或重跑的子代理 run。
def _runner_candidates_for_context(
    tasks: list,
    ctx: DispatchContext,
    runner_retry_limit: int,
    *,
    runtime_policy=None,
    no_progress_attempt_limit: int | None = None,
) -> list:
    policy = RunnerCandidatePolicy(
        runner_retry_limit=runner_retry_limit,
        background_launch_id=ctx.background_launch_id,
        runtime_guard_policy=runtime_policy,
        no_progress_attempt_limit=no_progress_attempt_limit,
    )
    if requested_include_ids(ctx):
        candidates = _included_normal_runner_tasks(
            tasks,
            ctx,
            policy,
        )
        if candidates or not _is_explicit_recovery_dispatch(ctx):
            return candidates
        return _included_recovery_runner_tasks(tasks, ctx)
    return _dispatch_runner_candidates(
        tasks,
        ctx.max_runners,
        policy=policy,
    )


# LLM: 只从显式指定且通过统一候选门禁的任务中按请求顺序截取；非正 max_runners 不产生候选。
# 函数用途: 为指定 run ID 的普通派工应用本次 runner 数量上限。
def _included_normal_runner_tasks(
    tasks: list,
    ctx: DispatchContext,
    policy: RunnerCandidatePolicy,
) -> list:
    selected = _requested_candidate_tasks(tasks, ctx, policy)
    return selected[: max(0, int(ctx.max_runners or 0))]


# LLM: 以现有任务建立 ID 索引，按显式请求顺序读取并调用统一候选门禁；缺失任务忽略，不改任务状态。
# 函数用途: 返回派工请求指定且满足普通启动或重试策略的任务。
def _requested_candidate_tasks(
    tasks: list,
    ctx: DispatchContext,
    policy: RunnerCandidatePolicy,
) -> list:
    requested = requested_include_ids(ctx)
    by_id = {str(getattr(task, "id", "") or ""): task for task in tasks}
    return [
        task
        for run_id in requested
        if (
            (task := by_id.get(run_id)) is not None
            and _is_dispatch_runner_candidate(task, policy=policy)
        )
    ]


# LLM: 必须同时存在显式 run ID 和协议解析后的重跑模式；普通指令文本不能触发恢复路径。
# 函数用途: 让候选接纳阶段识别用户明确指定的恢复派工。
def _is_explicit_recovery_dispatch(ctx: DispatchContext) -> bool:
    if not requested_include_ids(ctx):
        return False
    return is_rerun_mode(recovery_mode_from_protocol_value(getattr(ctx, "recovery_mode", "")))


# LLM: 按请求顺序筛选可由恢复指令重跑的现存任务，并截取数量上限；只生成候选，不重置或启动任务。
# 函数用途: 在显式恢复派工没有普通候选时提供专用恢复候选列表。
def _included_recovery_runner_tasks(tasks: list, ctx: DispatchContext) -> list:
    requested = requested_include_ids(ctx)
    by_id = {str(getattr(task, "id", "") or ""): task for task in tasks}
    selected: list = []
    for run_id in requested:
        task = by_id.get(run_id)
        if task is not None and _can_rerun_from_recovery_instruction(task):
            selected.append(task)
    return selected[: max(0, int(ctx.max_runners or 0))]


# LLM: 显式include/typed rerun不是坏账覆盖授权；先核同源计数，再保留用户停止和原能力/终态门，不写状态。
# 函数用途: 判断指定run能否显式恢复；不能把已识别的坏历史作为普通恢复候选。
def _can_rerun_from_recovery_instruction(task: object) -> bool:
    if recovery_count(task, "recovery_history_unknown_count"):
        return False
    if user_stopped_run_is_resumable(task):
        return True
    recoverable_statuses = frozenset({TaskStatus.BLOCKED.value, TaskStatus.FAILED.value})
    if not task_status_in(getattr(task, "status", ""), recoverable_statuses):
        return False
    if _terminal_recovery_code_present(task):
        return False
    if str(getattr(task, "channel_status", "") or "") == "BROKEN":
        return False
    if any(getattr(item, "status", "") == "OPEN" for item in getattr(task, "capability_requests", []) or []):
        return False
    if any(getattr(item, "status", "") == "OPEN" for item in getattr(task, "capability_gaps", []) or []):
        return False
    return True


# LLM: 先按模型枚举归一化失败类型，再识别接管链耗尽和无进展熔断；不根据自由文本推断终局状态。
# 函数用途: 阻止显式恢复路径绕过任务的终局恢复熔断标记。
def _terminal_recovery_code_present(task: object) -> bool:
    failure_type = known_failure_type(getattr(task, "failure_type", ""))
    terminal_codes = {
        FailureType.TAKEOVER_CHAIN_EXHAUSTED.value,
        FailureType.NO_PROGRESS_FUSE.value,
    }
    return failure_type in terminal_codes
