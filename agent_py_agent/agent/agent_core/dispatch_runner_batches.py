# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""Runner candidate collection and batch execution for dispatch mixins."""

from dataclasses import dataclass

from ..subagents.services.dispatch_params import DispatchRecordParams
from .dispatch_limiter import RunnerJobLimitRequest, limit_runner_jobs
from .dispatch_params import DispatchContext, RunnerBatchContext
from .dispatch_runner_selection import (
    invalid_include_run_ids_record,
    requested_include_ids,
    scoped_current_turn_runner_tasks,
    scoped_runner_tasks,
)
from .orchestration_run_scope import remembered_orchestration_run_ids
from .runner_dispatch import (
    RunnerDispatchRecordParams,
    _dispatch_runner_candidates,
    _is_dispatch_runner_candidate,
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
    all_tasks = agent.subagents.list_runs()
    scoped_tasks = scoped_current_turn_runner_tasks(
        scoped_runner_tasks(all_tasks, ctx),
        ctx,
        active_run_ids=remembered_orchestration_run_ids(agent),
    )
    selection_record = invalid_include_run_ids_record(agent, ctx, all_tasks, scoped_tasks)
    if selection_record is not None:
        records.append(selection_record)
        return records
    runner_candidates = _runner_candidates_for_context(scoped_tasks, ctx, runner_max_attempts)
    dry_records, pending_runner_jobs = collect_runner_candidates(
        agent, ctx, runner_max_attempts, runner_candidates
    )
    records.extend(dry_records)
    if not pending_runner_jobs:
        return records
    batch.pending_runner_jobs = _limited_runner_jobs(agent, pending_runner_jobs)
    batch.records = records
    _guard_multi_runner_instruction(agent, ctx, batch)
    return run_runner_batch(agent, batch)


# LLM: _runner_candidates_for_context lets explicit packet recovery rerun the original blocked child.
# 函数用途: 普通 dispatch 仍走候选过滤；只有显式 run_id 加恢复指令时，才允许 BLOCKED/FAILED 原 run 续跑。
def _runner_candidates_for_context(tasks: list, ctx: DispatchContext, runner_max_attempts: int) -> list:
    if requested_include_ids(ctx):
        candidates = _included_normal_runner_tasks(tasks, ctx, runner_max_attempts)
        if candidates or not _is_explicit_recovery_dispatch(ctx):
            return candidates
        return _included_recovery_runner_tasks(tasks, ctx)
    candidates = _dispatch_runner_candidates(
        tasks,
        ctx.max_runners,
        runner_max_attempts=runner_max_attempts,
    )
    return candidates


# LLM: _included_normal_runner_tasks treats explicit run_ids as exact ordered targets.
# 函数用途: 模型或父级已经给定 run_ids 时，不再用阶段闸门静默丢掉 worker/coordinator 混合批次。
def _included_normal_runner_tasks(tasks: list, ctx: DispatchContext, runner_max_attempts: int) -> list:
    selected = [
        task for task in tasks
        if _is_dispatch_runner_candidate(task, runner_max_attempts=runner_max_attempts)
    ]
    return selected[: max(0, int(ctx.max_runners or 0))]


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


# LLM: _guard_multi_runner_instruction prevents one child-specific hint from poisoning a batch of distinct runners.
# 函数用途: 多个 runner 同轮执行时清空共享 runner_instruction，并写一条调度记录，避免 auth 指令串到 catalog/cart 等不同分支。
def _guard_multi_runner_instruction(agent, ctx: DispatchContext, batch: RunnerBatchContext) -> None:
    if len(batch.pending_runner_jobs) <= 1 or not str(batch.effective_runner_instruction or "").strip():
        return
    task_dirs = [str(before.task_dir) for _, before, _ in batch.pending_runner_jobs if before.task_dir]
    batch.records.append(_multi_runner_instruction_record(agent, ctx, task_dirs))
    batch.effective_runner_instruction = ""


# LLM: _multi_runner_instruction_record keeps the safety downgrade visible without leaking full prompt text.
# 函数用途: 构建“多 runner 指令被忽略”的报告记录；只写原因和任务目录引用，不写完整指令正文。
def _multi_runner_instruction_record(agent, ctx: DispatchContext, evidence_paths: list[str]):
    return agent.subagents.make_dispatch_record(
        params=DispatchRecordParams(
            step="runner_instruction",
            action="ignore_multi_runner_instruction",
            dry_run=not ctx.apply,
            applied=False,
            ok=True,
            message=(
                "已忽略本轮共享 runner_instruction：同一次 dispatch 选中了多个 runner，"
                "为避免某个子任务专属提示污染其他分支，请改为分别 dispatch 单个 run_id，"
                "或把通用要求写入每个 child goal/context bundle。"
            ),
            evidence_paths=evidence_paths[:20],
        ),
    )


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
