# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""Runner candidate collection and batch execution for dispatch mixins."""

from dataclasses import dataclass

from ..subagents.services.dispatch_params import DispatchRecordParams
from .dispatch_limiter import RunnerJobLimitRequest, limit_runner_jobs
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


# LLM: RunnerRecordInput 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存执行器记录input字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class RunnerRecordInput:
    run_id: str
    before: object
    after: object
    result: object
    retry_reason: str


# LLM: RunnerDryRecordParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存执行器dry记录参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RunnerDryRecordParams:
    agent: object
    ctx: DispatchContext
    task: object
    before: object
    retry_reason: str


# LLM: execute_runner_jobs 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器jobs的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def execute_runner_jobs(agent, ctx: DispatchContext, batch: RunnerBatchContext) -> list:
    records = list(batch.records)
    runner_max_attempts = _runner_max_attempts(agent.config.runner_failure_policy)
    runner_candidates = _dispatch_runner_candidates(
        _scoped_runner_tasks(agent.subagents.list_runs(), ctx),
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


# LLM: _scoped_runner_tasks keeps nested dispatch focused on the current node's descendants.
# 函数用途: 根据 parent/root/exclude 过滤 runner 候选；默认顶层不变，runner 内部可只跑直接 child。
def _scoped_runner_tasks(tasks: list, ctx: DispatchContext) -> list:
    excluded = {str(item) for item in (ctx.exclude_run_ids or []) if str(item).strip()}
    scoped = []
    for task in tasks:
        if task.id in excluded:
            continue
        if ctx.parent_run_id and task.parent_id != ctx.parent_run_id:
            continue
        if ctx.root_id and task.root_id != ctx.root_id:
            continue
        scoped.append(task)
    return scoped


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
            _dry_runner_record(RunnerDryRecordParams(agent, ctx, task, before, retry_reason))
        )
    return dry_records, pending_runner_jobs


# LLM: _dry_runner_record 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理dry执行器记录相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def _dry_runner_record(params: RunnerDryRecordParams):
    return params.agent.subagents.make_dispatch_record(
        params=DispatchRecordParams(
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
        ),
    )


# LLM: _dry_runner_message 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理dry执行器消息相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _dry_runner_message(retry_reason: str) -> str:
    if retry_reason:
        return f"dry-run: 将重试 runner（{retry_reason}）。"
    return "dry-run: apply 时会生成执行上下文；带 --execute-runners 时会调用模型。"


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
