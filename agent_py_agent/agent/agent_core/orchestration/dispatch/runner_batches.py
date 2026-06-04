
from __future__ import annotations

"""Runner candidate collection and batch execution for dispatch mixins."""

from ...runner.dispatch import (
    _runner_max_attempts,
    _runner_retry_reason,
    _same_run_redispatch_limit,
)
from ...runner.dispatch_record import RunnerDispatchRecordParams, runner_dispatch_record
from ...runner.gate import (
    ConcurrentRunnerParams,
    RunnerFailureParams,
    SingleRunnerParams,
    get_task_timeout,
    handle_runner_failure,
    resolve_runner_config,
    run_concurrent_runners,
    run_single_runner,
)
from ..run_scope import remembered_orchestration_run_ids
from .collaboration_candidates import (
    collaboration_candidate_limit,
    collaboration_request_runner_candidates,
    collaboration_request_runner_candidates_report,
)
from .limiter import limit_runner_jobs
from .params import DispatchContext, RunnerBatchContext
from .runner_candidates import (
    _runner_candidates_for_context,
)
from .runner_records import (
    collaboration_candidate_load_error_record,
    dry_runner_record,
    multi_runner_instruction_record,
)
from .runner_selection import (
    invalid_include_run_ids_record,
    requested_include_ids,
    scoped_current_turn_runner_tasks,
    scoped_runner_tasks,
)


def execute_runner_jobs(agent, ctx: DispatchContext, batch: RunnerBatchContext) -> list:
    records = list(batch.records)
    runner_max_attempts, same_run_limit = _runner_dispatch_limits(agent)
    all_tasks = agent.subagents.list_runs()
    active_run_ids = remembered_orchestration_run_ids(agent)
    scoped_tasks = scoped_current_turn_runner_tasks(
        scoped_runner_tasks(all_tasks, ctx),
        ctx,
        active_run_ids=active_run_ids,
    )
    selection_record = invalid_include_run_ids_record(agent, ctx, all_tasks, scoped_tasks)
    if selection_record is not None:
        records.append(selection_record)
        return records
    runner_candidates = _runner_candidates_for_context(
        scoped_tasks,
        ctx,
        runner_max_attempts,
        same_run_redispatch_limit=same_run_limit,
    )
    collaboration_report = collaboration_request_runner_candidates_report(agent, scoped_tasks)
    collaboration_candidates = collaboration_report.candidates
    if collaboration_report.load_errors:
        records.append(collaboration_candidate_load_error_record(agent, ctx, collaboration_report.load_errors))
    candidate_limit = collaboration_candidate_limit(
        agent,
        ctx,
        collaboration_candidates,
        has_explicit_run_ids=bool(requested_include_ids(ctx)),
    )
    runner_candidates = _merge_runner_candidates(
        collaboration_candidates,
        runner_candidates,
        limit=candidate_limit,
    )
    dry_records, pending_runner_jobs = collect_runner_candidates(
        agent, ctx, runner_max_attempts, runner_candidates
    )
    records.extend(dry_records)
    if not pending_runner_jobs:
        return records
    batch.records = records
    batch.pending_runner_jobs = pending_runner_jobs
    return _run_runner_jobs_with_limit(agent, ctx, batch)


def _runner_dispatch_limits(agent) -> tuple[int, int]:
    runtime_policy = getattr(agent, "runtime_guard_policy", None)
    return (
        _runner_max_attempts(agent.config.runner_failure_policy, runtime_policy=runtime_policy),
        _same_run_redispatch_limit(
            getattr(agent.config, "same_run_redispatch_limit", None),
            runtime_policy=runtime_policy,
        ),
    )


def _merge_runner_candidates(primary: list, secondary: list, *, limit: int) -> list:
    merged = []
    seen: set[str] = set()
    for task in [*primary, *secondary]:
        run_id = str(getattr(task, "id", "") or "")
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        merged.append(task)
    if limit <= 0:
        return merged
    return merged[:limit]


def collect_runner_candidates(agent, ctx: DispatchContext, runner_max_attempts: int, runner_candidates: list):
    pending_runner_jobs, dry_records = [], []
    for task in runner_candidates:
        before = agent.subagents.load(task.id)
        retry_reason = _runner_retry_reason(before, runner_max_attempts)
        if ctx.execution_plan.mutate_state:
            pending_runner_jobs.append((task.id, before, retry_reason))
            continue
        dry_records.append(dry_runner_record(agent, ctx, (task, before, retry_reason)))
    return dry_records, pending_runner_jobs


def _run_runner_jobs_with_limit(agent, ctx: DispatchContext, batch: RunnerBatchContext) -> list:
    batch.pending_runner_jobs = _limited_runner_jobs(agent, batch.pending_runner_jobs)
    _guard_multi_runner_instruction(agent, ctx, batch)
    return run_runner_batch(agent, batch)


def _limited_runner_jobs(agent, pending_runner_jobs: list) -> list:
    _, _, runner_start_rate = resolve_runner_config(agent.config, len(pending_runner_jobs))
    result = limit_runner_jobs(
        pending_runner_jobs,
        runner_start_rate=runner_start_rate,
        role_limits=_runner_role_limits(agent.config),
    )
    return result.allowed_jobs


def _guard_multi_runner_instruction(agent, ctx: DispatchContext, batch: RunnerBatchContext) -> None:
    if len(batch.pending_runner_jobs) <= 1 or not str(batch.effective_runner_instruction or "").strip():
        return
    task_dirs = [str(before.task_dir) for _, before, _ in batch.pending_runner_jobs if before.task_dir]
    batch.records.append(multi_runner_instruction_record(agent, ctx, task_dirs))
    batch.effective_runner_instruction = ""


def _runner_role_limits(config) -> dict[str, int]:
    raw = getattr(config, "runner_role_limits", {}) or {}
    if not isinstance(raw, dict):
        return {}
    limits: dict[str, int] = {}
    for role, limit in raw.items():
        try:
            limits[str(role)] = max(0, int(limit))
        except (TypeError, ValueError):
            continue
    return limits


def run_runner_batch(agent, ctx: RunnerBatchContext) -> list:
    runner_timeout_seconds, runner_concurrency, _ = resolve_runner_config(
        agent.config, len(ctx.pending_runner_jobs)
    )
    ctx.runner_timeout_seconds = runner_timeout_seconds
    if runner_concurrency > 1 and ctx.execution_plan.start_runners:
        ctx.runner_concurrency = runner_concurrency
        return _run_concurrent_batch(agent, ctx)
    return _run_sequential_batch(agent, ctx)


def _run_concurrent_batch(agent, ctx: RunnerBatchContext) -> list:
    completed = run_concurrent_runners(
        ConcurrentRunnerParams(
            agent=agent,
            pending_jobs=ctx.pending_runner_jobs,
            runner_concurrency=ctx.runner_concurrency,
            runner_timeout_seconds=ctx.runner_timeout_seconds,
            instruction=ctx.effective_runner_instruction,
            start_runners=ctx.execution_plan.start_runners,
            max_cards=ctx.max_cards,
            probe=ctx.probe,
        )
    )
    for run_id, before, retry_reason in ctx.pending_runner_jobs:
        result, after = completed[run_id]
        _append_runner_record(agent, ctx, ((run_id, before, retry_reason), after, result))
    return ctx.records


def _run_sequential_batch(agent, ctx: RunnerBatchContext) -> list:
    for run_id, before, retry_reason in ctx.pending_runner_jobs:
        result = run_single_runner(
            SingleRunnerParams(
                agent=agent,
                run_id=run_id,
                task_timeout=get_task_timeout(before, ctx.runner_timeout_seconds, agent.config),
                instruction=ctx.effective_runner_instruction,
                start_runner=ctx.execution_plan.start_runners,
                max_cards=ctx.max_cards,
                probe=ctx.probe,
                retry_reason=retry_reason,
            )
        )
        _append_runner_record(
            agent,
            ctx,
            ((run_id, before, retry_reason), agent.subagents.load(run_id), result),
        )
    return ctx.records


def _append_runner_record(
    agent,
    ctx: RunnerBatchContext,
    record_input: tuple[tuple[str, object, str], object, object],
) -> None:
    pending_job, after, result = record_input
    run_id, before, retry_reason = pending_job
    ctx.records.append(
        runner_dispatch_record(
            RunnerDispatchRecordParams(
                agent=agent,
                run_id=run_id,
                before=before,
                after=after,
                result=result,
                retry_reason=retry_reason,
                start_runner=ctx.execution_plan.start_runners,
            )
        )
    )
    if not result.ok and ctx.execution_plan.start_runners:
        ctx.effective_runner_instruction = handle_runner_failure(
            RunnerFailureParams(
                agent=agent,
                run_id=run_id,
                before=before,
                result=result,
                effective_instruction=ctx.effective_runner_instruction,
            )
        )
