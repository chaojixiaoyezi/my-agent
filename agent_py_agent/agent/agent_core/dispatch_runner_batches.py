# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""Runner candidate collection and batch execution for dispatch mixins."""

from dataclasses import dataclass

from .dispatch_collaboration_candidates import (
    CollaborationCandidateLimitInput,
    collaboration_candidate_limit,
    collaboration_request_runner_candidates,
)
from .dispatch_limiter import RunnerJobLimitRequest, limit_runner_jobs
from .dispatch_params import DispatchContext, RunnerBatchContext
from .dispatch_runner_candidates import (
    _runner_candidates_for_context,
)
from .dispatch_runner_records import (
    RunnerDryRecordParams,
    dry_runner_record,
    multi_runner_instruction_record,
)
from .dispatch_runner_selection import (
    invalid_include_run_ids_record,
    requested_include_ids,
    scoped_current_turn_runner_tasks,
    scoped_runner_tasks,
)
from .orchestration_run_scope import remembered_orchestration_run_ids
from .runner_dispatch import (
    RunnerDispatchRecordParams,
    _runner_dispatch_record,
    _runner_max_attempts,
    _runner_retry_reason,
    _same_run_redispatch_limit,
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


# LLM: RunnerRecordInput 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存执行器记录input字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class RunnerRecordInput:
    run_id: str
    before: object
    after: object
    result: object
    retry_reason: str


@dataclass(frozen=True)
class RunnerJobsRunInput:
    agent: object
    ctx: DispatchContext
    batch: RunnerBatchContext
    records: list
    pending_runner_jobs: list


# LLM: execute_runner_jobs 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器jobs的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def execute_runner_jobs(agent, ctx: DispatchContext, batch: RunnerBatchContext) -> list:
    records = list(batch.records)
    runner_max_attempts = _runner_max_attempts(agent.config.runner_failure_policy)
    same_run_limit = _same_run_redispatch_limit(getattr(agent.config, "same_run_redispatch_limit", None))
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
    collaboration_candidates = collaboration_request_runner_candidates(agent, scoped_tasks)
    candidate_limit = collaboration_candidate_limit(
        CollaborationCandidateLimitInput(
            config=getattr(agent, "config", None),
            requested_limit=ctx.max_runners,
            candidates=collaboration_candidates,
            execute_runners=batch.execute_runners,
            has_explicit_run_ids=bool(requested_include_ids(ctx)),
        )
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
    return _run_runner_jobs_with_limit(RunnerJobsRunInput(agent, ctx, batch, records, pending_runner_jobs))


# LLM: _merge_runner_candidates gives collaboration wakeups priority while preserving normal candidate order.
# 函数用途: 合并协作唤醒候选和普通候选，按 run_id 去重并遵守 max_runners。
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
        return []
    return merged[:limit]


# LLM: collect_runner_candidates 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 读取或查询执行器candidates需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def collect_runner_candidates(agent, ctx: DispatchContext, runner_max_attempts: int, runner_candidates: list):
    pending_runner_jobs, dry_records = [], []
    for task in runner_candidates:
        before = agent.subagents.load(task.id)
        retry_reason = _runner_retry_reason(before, runner_max_attempts)
        if ctx.apply:
            pending_runner_jobs.append((task.id, before, retry_reason))
            continue
        dry_records.append(
            dry_runner_record(RunnerDryRecordParams(agent, ctx, task, before, retry_reason))
        )
    return dry_records, pending_runner_jobs


# LLM: _run_runner_jobs_with_limit centralizes rate limiting and instruction guards for selected runners.
# 函数用途: 统一执行父级选中的 runner，保持父级 run_ids 顺序，不再按角色拆隐藏波次。
def _run_runner_jobs_with_limit(request: RunnerJobsRunInput) -> list:
    request.batch.pending_runner_jobs = _limited_runner_jobs(request.agent, request.pending_runner_jobs)
    request.batch.records = request.records
    _guard_multi_runner_instruction(request.agent, request.ctx, request.batch)
    return run_runner_batch(request.agent, request.batch)


# LLM: _limited_runner_jobs 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 计算limited执行器jobs的预算、数量或限制，影响后续调度节奏；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _limited_runner_jobs(agent, pending_runner_jobs: list) -> list:
    _, _, runner_start_rate = resolve_runner_config(agent.config, len(pending_runner_jobs))
    result = limit_runner_jobs(
        RunnerJobLimitRequest(
            jobs=pending_runner_jobs,
            runner_start_rate=runner_start_rate,
            role_limits=_runner_role_limits(agent.config),
        )
    )
    return result.allowed_jobs


# LLM: _guard_multi_runner_instruction prevents one child-specific hint from poisoning a batch of distinct runners.
# 函数用途: 多个 runner 同轮执行时清空共享 runner_instruction，并写一条调度记录，避免 auth 指令串到 catalog/cart 等不同分支。
def _guard_multi_runner_instruction(agent, ctx: DispatchContext, batch: RunnerBatchContext) -> None:
    if len(batch.pending_runner_jobs) <= 1 or not str(batch.effective_runner_instruction or "").strip():
        return
    task_dirs = [str(before.task_dir) for _, before, _ in batch.pending_runner_jobs if before.task_dir]
    batch.records.append(multi_runner_instruction_record(agent, ctx, task_dirs))
    batch.effective_runner_instruction = ""


# LLM: _runner_role_limits reads an optional future config hook without requiring schema changes today.
# 函数用途: 获取可选 runner_role_limits；不存在时返回空，保持旧行为。
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


# LLM: run_runner_batch 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器batch的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def run_runner_batch(agent, ctx: RunnerBatchContext) -> list:
    runner_timeout_seconds, runner_concurrency, _ = resolve_runner_config(
        agent.config, len(ctx.pending_runner_jobs)
    )
    ctx.runner_timeout_seconds = runner_timeout_seconds
    if runner_concurrency > 1 and ctx.execute_runners:
        ctx.runner_concurrency = runner_concurrency
        return _run_concurrent_batch(agent, ctx)
    return _run_sequential_batch(agent, ctx)


# LLM: _run_concurrent_batch 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进concurrentbatch的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
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


# LLM: _run_sequential_batch 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进sequentialbatch的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
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


# LLM: _append_runner_record 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 写入执行器记录的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
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
