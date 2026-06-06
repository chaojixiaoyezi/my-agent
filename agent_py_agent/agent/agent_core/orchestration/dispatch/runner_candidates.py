
from __future__ import annotations

from ....subagents.services.recovery.modes import is_rerun_mode
from ...runner.candidate_policy import RunnerCandidatePolicy
from ...runner.dispatch import (
    _dispatch_runner_candidates,
    _is_dispatch_runner_candidate,
    _runner_retry_reason,
)
from .params import DispatchContext
from .runner_selection import requested_include_ids


def _runner_candidates_for_context(
    tasks: list,
    ctx: DispatchContext,
    runner_max_attempts: int,
    *,
    same_run_redispatch_limit: int | None = None,
) -> list:
    policy = RunnerCandidatePolicy(
        runner_max_attempts=runner_max_attempts,
        same_run_redispatch_limit=same_run_redispatch_limit,
        background_launch_id=ctx.background_launch_id,
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


def _included_normal_runner_tasks(
    tasks: list,
    ctx: DispatchContext,
    policy: RunnerCandidatePolicy,
) -> list:
    selected = _requested_candidate_tasks(tasks, ctx, policy)
    return selected[: max(0, int(ctx.max_runners or 0))]


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


def _is_explicit_recovery_dispatch(ctx: DispatchContext) -> bool:
    if not requested_include_ids(ctx):
        return False
    return is_rerun_mode(str(getattr(ctx, "recovery_mode", "") or ""))


def _included_recovery_runner_tasks(tasks: list, ctx: DispatchContext) -> list:
    requested = requested_include_ids(ctx)
    by_id = {str(getattr(task, "id", "") or ""): task for task in tasks}
    selected: list = []
    for run_id in requested:
        task = by_id.get(run_id)
        if task is not None and _can_rerun_from_recovery_instruction(task):
            selected.append(task)
    return selected[: max(0, int(ctx.max_runners or 0))]


def _can_rerun_from_recovery_instruction(task: object) -> bool:
    status = str(getattr(task, "status", "") or "").upper()
    verification = str(getattr(task, "verification_status", "") or "").upper()
    if status not in {"BLOCKED", "FAILED"} or verification == "VERIFIED":
        return False
    if _terminal_recovery_code_present(task):
        return False
    if str(getattr(task, "channel_status", "") or "").upper() == "BROKEN":
        return False
    if any(getattr(item, "status", "") == "OPEN" for item in getattr(task, "capability_requests", []) or []):
        return False
    if any(getattr(item, "status", "") == "OPEN" for item in getattr(task, "capability_gaps", []) or []):
        return False
    return True


def _terminal_recovery_code_present(task: object) -> bool:
    failure_type = str(getattr(task, "failure_type", "") or "").strip().lower()
    terminal_codes = {
        "takeover_chain_exhausted",
        "no_progress_fuse",
    }
    return failure_type in terminal_codes
