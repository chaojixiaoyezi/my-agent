# LLM: Runner candidate policy module; keep explicit run-id, input, workflow, and recovery gates together.
# 模块用途: 统一计算 dispatch_subagents 要启动哪些 runner，避免批处理执行模块继续变胖。

from __future__ import annotations

from .dispatch_params import DispatchContext
from .dispatch_runner_selection import requested_include_ids
from .runner_dispatch import (
    _dispatch_runner_candidates,
    _is_dispatch_runner_candidate,
    _runner_retry_reason,
)
from .runner_input_dependencies import input_dependency_ready_candidates
from .runner_workflow_dependencies import workflow_dependency_ready_candidates


# LLM: _runner_candidates_for_context lets explicit packet recovery rerun the original blocked child.
# 函数用途: 普通 dispatch 仍走候选过滤；只有显式 run_id 加恢复指令时，才允许 BLOCKED/FAILED 原 run 续跑。
def _runner_candidates_for_context(
    tasks: list,
    ctx: DispatchContext,
    runner_max_attempts: int,
    *,
    dependency_tasks: list | None = None,
) -> list:
    if requested_include_ids(ctx):
        candidates = _included_normal_runner_tasks(tasks, ctx, runner_max_attempts, dependency_tasks or tasks)
        if candidates or not _is_explicit_recovery_dispatch(ctx):
            return candidates
        return _included_recovery_runner_tasks(tasks, ctx)
    return _dispatch_runner_candidates(
        tasks,
        ctx.max_runners,
        runner_max_attempts=runner_max_attempts,
    )


# LLM: _included_normal_runner_tasks treats explicit run_ids as exact ordered targets.
# 函数用途: 模型或父级已经给定 run_ids 时，不再用阶段闸门静默丢掉 worker/coordinator 混合批次。
def _included_normal_runner_tasks(
    tasks: list,
    ctx: DispatchContext,
    runner_max_attempts: int,
    dependency_tasks: list,
) -> list:
    selected = _requested_candidate_tasks(tasks, ctx, runner_max_attempts)
    selected = input_dependency_ready_candidates(selected, dependency_tasks=dependency_tasks)
    selected = workflow_dependency_ready_candidates(selected, dependency_tasks)
    return selected[: max(0, int(ctx.max_runners or 0))]


# LLM: _requested_candidate_tasks keeps explicit include_run_ids exact and ordered.
# 函数用途: 按父级指定的 run_id 顺序挑选可启动任务，避免无关候选混入同一轮 dispatch。
def _requested_candidate_tasks(tasks: list, ctx: DispatchContext, runner_max_attempts: int) -> list:
    requested = requested_include_ids(ctx)
    by_id = {str(getattr(task, "id", "") or ""): task for task in tasks}
    return [
        task
        for run_id in requested
        if (
            (task := by_id.get(run_id)) is not None
            and _is_dispatch_runner_candidate(task, runner_max_attempts=runner_max_attempts)
        )
    ]


# LLM: _is_explicit_recovery_dispatch keeps forced reruns tied to control-plane recovery refs.
# 函数用途: 只有模型复制了 packet/checkpoint 恢复建议且指定 run_ids 时，才绕过普通 blocked 候选规则。
def _is_explicit_recovery_dispatch(ctx: DispatchContext) -> bool:
    if not requested_include_ids(ctx):
        return False
    instruction = str(ctx.runner_instruction or "").lower()
    return "latest_continue_packet" in instruction or "checkpoint" in instruction


# LLM: _included_recovery_runner_tasks preserves requested order while avoiding closed or unsafe runs.
# 函数用途: 从显式 include_run_ids 中挑出可恢复的 BLOCKED/FAILED 任务，防止 DONE/TAKEN_OVER 被误重跑。
def _included_recovery_runner_tasks(tasks: list, ctx: DispatchContext) -> list:
    requested = requested_include_ids(ctx)
    by_id = {str(getattr(task, "id", "") or ""): task for task in tasks}
    selected: list = []
    for run_id in requested:
        task = by_id.get(run_id)
        if task is not None and _can_rerun_from_recovery_instruction(task):
            selected.append(task)
    return selected[: max(0, int(ctx.max_runners or 0))]


# LLM: _can_rerun_from_recovery_instruction is narrower than normal retry policy on purpose.
# 函数用途: packet/checkpoint 续跑只允许未完成且通道可用的任务，避免把验收完成或被接管的任务重新跑一遍。
def _can_rerun_from_recovery_instruction(task: object) -> bool:
    status = str(getattr(task, "status", "") or "").upper()
    verification = str(getattr(task, "verification_status", "") or "").upper()
    if status not in {"BLOCKED", "FAILED"} or verification == "VERIFIED":
        return False
    if _terminal_recovery_blocker(task):
        return False
    if str(getattr(task, "channel_status", "") or "").upper() == "BROKEN":
        return False
    if any(getattr(item, "status", "") == "OPEN" for item in getattr(task, "capability_requests", []) or []):
        return False
    if any(getattr(item, "status", "") == "OPEN" for item in getattr(task, "capability_gaps", []) or []):
        return False
    return True


# LLM: terminal recovery blockers must report upward instead of burning another runner attempt.
# 函数用途: 判断当前 BLOCKED 是否已经到达接管链路熔断等终局状态；命中时禁止显式 packet 恢复重跑。
def _terminal_recovery_blocker(task: object) -> bool:
    failure_type = str(getattr(task, "failure_type", "") or "").strip().lower()
    current_step = str(getattr(task, "current_step", "") or "").strip().lower()
    result = str(getattr(task, "result", "") or "").strip().lower()
    markers = (
        "takeover_chain_exhausted",
        "takeover chain exhausted",
        "no_progress_fuse",
        "no-progress fuse",
    )
    haystack = " ".join(item for item in [failure_type, current_step, result] if item)
    return any(marker in haystack for marker in markers)
