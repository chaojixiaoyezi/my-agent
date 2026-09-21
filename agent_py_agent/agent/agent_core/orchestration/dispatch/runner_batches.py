# LLM: 候选在创建锁内接纳，线程池只消费原执行轮；联测取消、重试和并发排队。
# 模块用途: 组织子代理批次，固定每个工作项的身份并收集正式执行结果。
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
from .conversation_lifecycle_gate import conversation_lifecycle_decisions
from .limiter import limit_runner_jobs
from .params import DispatchContext, DispatchParams, RunnerBatchContext
from .runner_candidates import (
    _is_explicit_recovery_dispatch,
    _runner_candidates_for_context,
)
from .runner_records import (
    collaboration_candidate_load_error_record,
    conversation_lifecycle_gate_record,
    dry_runner_record,
    multi_runner_instruction_record,
)
from .runner_selection import (
    invalid_include_run_ids_record,
    requested_include_ids,
    scoped_current_turn_runner_tasks,
    scoped_runner_tasks,
)


def run_dispatch_runner_stage(
    agent=None,
    *,
    ctx: DispatchContext | None = None,
    params: DispatchParams | None = None,
    records: list | None = None,
) -> list:
    if agent is None or ctx is None or params is None:
        raise TypeError("run_dispatch_runner_stage requires agent, ctx, and params")
    batch_ctx = RunnerBatchContext(
        pending_runner_jobs=[],
        runner_concurrency=0,
        runner_timeout_seconds=0,
        effective_runner_instruction=ctx.runner_instruction,
        max_cards=params.max_cards,
        probe=params.probe,
        records=list(records or []),
        execution_plan=ctx.execution_plan,
    )
    records = execute_runner_jobs(agent, ctx, batch_ctx)
    ctx.runner_instruction = batch_ctx.effective_runner_instruction
    return records


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


# LLM: 候选接纳只在 creation guard 内预留准确 pending；外部已冻结 map 缺项即拒绝，不重新领取。
# 函数用途: 选出可执行的工作并固定轮次，线程池稍后启动仍使用同一身份。
def collect_runner_candidates(agent, ctx: DispatchContext, runner_max_attempts: int, runner_candidates: list):
    from ....subagents.runner_start import reserve_runner_start

    pending_runner_jobs, dry_records = [], []
    supplied_attempts = ctx.expected_attempt_ids
    admitted_attempts = dict(supplied_attempts or {})
    resume_run_ids = (
        set(requested_include_ids(ctx))
        if _is_explicit_recovery_dispatch(ctx)
        else set()
    )
    decisions = conversation_lifecycle_decisions(
        agent,
        runner_candidates,
        resume_run_ids=resume_run_ids,
    )
    for task in runner_candidates:
        decision = decisions[str(getattr(task, "id", "") or "")]
        if not decision.allowed:
            dry_records.append(conversation_lifecycle_gate_record(agent, ctx, task, decision))
            continue
        before = agent.subagents.load(task.id)
        retry_reason = _runner_retry_reason(before, runner_max_attempts)
        if ctx.execution_plan.mutate_state:
            if ctx.should_start_runners:
                expected = supplied_attempts[task.id] if supplied_attempts is not None else None
                admitted = reserve_runner_start(
                    agent.subagents, task.id, expected_attempt_id=expected,
                    resume_user_stop=task.id in resume_run_ids,
                    launch_id=ctx.background_launch_id,
                )
                admitted_attempts[task.id] = admitted
            pending_runner_jobs.append((task.id, before, retry_reason))
            continue
        dry_records.append(dry_runner_record(agent, ctx, (task, before, retry_reason)))
    ctx.expected_attempt_ids = admitted_attempts
    return dry_records, pending_runner_jobs


# LLM: 限流不能改变已接纳工作项的执行轮，批次复制同一映射。
# 函数用途: 应用并发限制并交给统一 runner 批次入口。
def _run_runner_jobs_with_limit(agent, ctx: DispatchContext, batch: RunnerBatchContext) -> list:
    batch.expected_attempt_ids = dict(ctx.expected_attempt_ids or {})
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


# LLM: 并行工作参数携带完整身份 map；不得因某个工作失败而写另一轮结果。
# 函数用途: 并行执行已接纳工作，并按原任务顺序收集结果。
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
            expected_attempt_ids=ctx.expected_attempt_ids,
        )
    )
    for run_id, before, retry_reason in ctx.pending_runner_jobs:
        result, after = completed[run_id]
        _append_runner_record(agent, ctx, ((run_id, before, retry_reason), after, result))
    return ctx.records


# LLM: 顺序排队同样使用接纳时的执行轮；上一项耗时不能让下一项重新领取新 current。
# 函数用途: 逐项执行固定工作批次，记录每项实际结果。
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
                expected_attempt_id=ctx.expected_attempt_ids[run_id] if ctx.execution_plan.start_runners else None,
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
