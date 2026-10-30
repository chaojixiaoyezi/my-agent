# LLM: 合并普通与协作候选，经会话门禁和限流后委托统一 runner 执行；真正启动时先预留执行轮，顺序和并发路径都复用该身份。
# 模块用途: 组织子代理批次，固定每个工作项的身份并收集正式执行结果。
from __future__ import annotations

"""Runner candidate collection and batch execution for dispatch mixins."""

from ...runner.dispatch import (
    _runner_retry_reason,
    runner_failure_retry_limit,
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


# LLM: 缺少代理、派工上下文或参数即拒绝；复制已有记录进入批次，执行后将有效指令回写派工上下文。
# 函数用途: 为单轮派工组织 runner 阶段上下文并返回累积执行记录。
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


# LLM: 普通失败的重跑次数只读配置 runner_failure_retry_limit（唯一的家）；候选接纳、协作请求合并与并发限流顺序不变，
#   会预留 runner 启动并执行批次（有副作用）。
# 函数用途: 挑出本次要跑的子代理 run 并按并发上限执行，返回派工记录。
def execute_runner_jobs(agent, ctx: DispatchContext, batch: RunnerBatchContext) -> list:
    records = list(batch.records)
    runner_retry_limit = runner_failure_retry_limit(agent.config)
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
        scoped_tasks, ctx, runner_retry_limit, runtime_policy=getattr(agent, "runtime_guard_policy", None))
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
        agent, ctx, runner_retry_limit, runner_candidates
    )
    records.extend(dry_records)
    if not pending_runner_jobs:
        return records
    batch.records = records
    batch.pending_runner_jobs = pending_runner_jobs
    return _run_runner_jobs_with_limit(agent, ctx, batch)


# LLM: 按主列表优先的顺序合并，以非空 run ID 去重；非正限额表示不截断，不修改输入列表或任务。
# 函数用途: 将协作候选与普通候选合成有序、限额内的派工列表。
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
def collect_runner_candidates(agent, ctx: DispatchContext, runner_retry_limit: int, runner_candidates: list):
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
        retry_reason = _runner_retry_reason(before, runner_retry_limit, runtime_policy=getattr(agent, "runtime_guard_policy", None))
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


# LLM: 读取代理启动速率和角色限额，委托限流器返回允许的原工作项；这里不启动 runner，也不重领执行轮。
# 函数用途: 在批次执行前按启动速率及角色配额裁剪待跑工作。
def _limited_runner_jobs(agent, pending_runner_jobs: list) -> list:
    _, _, runner_start_rate = resolve_runner_config(agent.config, len(pending_runner_jobs))
    result = limit_runner_jobs(
        pending_runner_jobs,
        runner_start_rate=runner_start_rate,
        role_limits=_runner_role_limits(agent.config),
    )
    return result.allowed_jobs


# LLM: 仅当多项工作共享非空指令时追加防误用记录并清空批次指令；单任务或空指令不受影响。
# 函数用途: 防止同一条可能针对单个任务的 runner 指令被广播到多个任务。
def _guard_multi_runner_instruction(agent, ctx: DispatchContext, batch: RunnerBatchContext) -> None:
    if len(batch.pending_runner_jobs) <= 1 or not str(batch.effective_runner_instruction or "").strip():
        return
    task_dirs = [str(before.task_dir) for _, before, _ in batch.pending_runner_jobs if before.task_dir]
    batch.records.append(multi_runner_instruction_record(agent, ctx, task_dirs))
    batch.effective_runner_instruction = ""


# LLM: 非字典配置视为空限制；角色名转字符串，限额转非负整数，无法转换的条目跳过，不回写配置。
# 函数用途: 为批次限流器生成标准化的角色并发配额映射。
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


# LLM: 从代理配置解析超时和并发并写入批次上下文；只有允许启动且并发大于一才走并行，否则逐项委托。
# 函数用途: 为已接纳的工作选择并行或顺序 runner 执行入口。
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


# LLM: 用执行前后快照和真实结果追加派工记录；实际启动失败时调用失败处理器，可能更新后续工作的有效指令。
# 函数用途: 为并行和顺序路径统一记录单项执行结果并承接失败后的批次处理。
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
