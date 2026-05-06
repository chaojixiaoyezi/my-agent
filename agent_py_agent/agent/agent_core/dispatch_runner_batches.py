from __future__ import annotations

"""Runner candidate collection and batch execution for dispatch mixins."""

from dataclasses import dataclass

from .dispatch_params import DispatchContext, RunnerBatchContext
from .runner_dispatch import (
    RunnerDispatchRecordParams,
    _dispatch_runner_candidates,
    _runner_dispatch_record,
    _runner_max_attempts,
    _runner_retry_reason,
)
from .runner_gate import (
    ConcurrentRunnerParams,
    RunnerFailureParams,
    SingleRunnerParams,
    get_task_timeout,
    handle_runner_failure,
    resolve_runner_config,
    run_concurrent_runners,
    run_single_runner,
)


@dataclass(frozen=True)
class RunnerRecordInput:
    run_id: str
    before: object
    after: object
    result: object
    retry_reason: str


@dataclass(frozen=True)
class RunnerDryRecordParams:
    agent: object
    ctx: DispatchContext
    task: object
    before: object
    retry_reason: str


def execute_runner_jobs(agent, ctx: DispatchContext, batch: RunnerBatchContext) -> list:
    records = list(batch.records)
    runner_max_attempts = _runner_max_attempts(agent.config.runner_failure_policy)
    runner_candidates = _dispatch_runner_candidates(
        agent.subagents.list_runs(),
        ctx.max_runners,
        runner_max_attempts=runner_max_attempts,
    )
    dry_records, pending_runner_jobs = collect_runner_candidates(
        agent, ctx, runner_max_attempts, runner_candidates
    )
    records.extend(dry_records)
    if not pending_runner_jobs:
        return records
    batch.pending_runner_jobs = _limited_runner_jobs(agent, pending_runner_jobs)
    batch.records = records
    return run_runner_batch(agent, batch)


def collect_runner_candidates(agent, ctx: DispatchContext, runner_max_attempts: int, runner_candidates: list):
    pending_runner_jobs, dry_records = [], []
    for task in runner_candidates:
        before = agent.subagents.load(task.id)
        retry_reason = _runner_retry_reason(before, runner_max_attempts)
        if ctx.apply:
            pending_runner_jobs.append((task.id, before, retry_reason))
            continue
        dry_records.append(
            _dry_runner_record(RunnerDryRecordParams(agent, ctx, task, before, retry_reason))
        )
    return dry_records, pending_runner_jobs


def _dry_runner_record(params: RunnerDryRecordParams):
    return params.agent.subagents.make_dispatch_record(
        step="runner",
        action="retry_runner" if params.retry_reason else "execute_runner",
        run_id=params.task.id,
        dry_run=True,
        applied=False,
        ok=True,
        message=_dry_runner_message(params.retry_reason),
        before_status=params.before.status,
        after_status=params.before.status,
        before_verification_status=params.before.verification_status,
        after_verification_status=params.before.verification_status,
        evidence_paths=[params.before.task_dir],
    )


def _dry_runner_message(retry_reason: str) -> str:
    if retry_reason:
        return f"dry-run: 将重试 runner（{retry_reason}）。"
    return "dry-run: apply 时会生成执行上下文；带 --execute-runners 时会调用模型。"


def _limited_runner_jobs(agent, pending_runner_jobs: list) -> list:
    _, _, runner_start_rate = resolve_runner_config(agent.config, len(pending_runner_jobs))
    if runner_start_rate and runner_start_rate < len(pending_runner_jobs):
        return pending_runner_jobs[:runner_start_rate]
    return pending_runner_jobs


def run_runner_batch(agent, ctx: RunnerBatchContext) -> list:
    runner_timeout_seconds, runner_concurrency, _ = resolve_runner_config(
        agent.config, len(ctx.pending_runner_jobs)
    )
    ctx.runner_timeout_seconds = runner_timeout_seconds
    if runner_concurrency > 1 and ctx.execute_runners:
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
            execute_runners=ctx.execute_runners,
            max_cards=ctx.max_cards,
            probe=ctx.probe,
        )
    )
    for run_id, before, retry_reason in ctx.pending_runner_jobs:
        result, after = completed[run_id]
        _append_runner_record(agent, ctx, RunnerRecordInput(run_id, before, after, result, retry_reason))
    return ctx.records


def _run_sequential_batch(agent, ctx: RunnerBatchContext) -> list:
    for run_id, before, retry_reason in ctx.pending_runner_jobs:
        result = run_single_runner(
            SingleRunnerParams(
                agent=agent,
                run_id=run_id,
                task_timeout=get_task_timeout(before, ctx.runner_timeout_seconds, agent.config),
                instruction=ctx.effective_runner_instruction,
                execute_runners=ctx.execute_runners,
                max_cards=ctx.max_cards,
                probe=ctx.probe,
                retry_reason=retry_reason,
            )
        )
        _append_runner_record(
            agent,
            ctx,
            RunnerRecordInput(run_id, before, agent.subagents.load(run_id), result, retry_reason),
        )
    return ctx.records


def _append_runner_record(agent, ctx: RunnerBatchContext, item: RunnerRecordInput) -> None:
    ctx.records.append(
        _runner_dispatch_record(
            RunnerDispatchRecordParams(
                agent=agent,
                run_id=item.run_id,
                before=item.before,
                after=item.after,
                result=item.result,
                retry_reason=item.retry_reason,
                execute_runners=ctx.execute_runners,
            )
        )
    )
    if not item.result.ok and ctx.execute_runners:
        ctx.effective_runner_instruction = handle_runner_failure(
            RunnerFailureParams(
                agent=agent,
                run_id=item.run_id,
                before=item.before,
                result=item.result,
                effective_instruction=ctx.effective_runner_instruction,
            )
        )
