# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

import time as time_module
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ...capabilities import CapabilityRouter
from ...capability_config import CapabilityConfig
from ..dispatch_no_progress import DispatchNoProgressTracker, dispatch_made_progress
from ..dispatch_params import DispatchParams, WatchParams, dispatch_params_from_watch
from .watch_config_reload import (
    WatchRuntimeConfigRequest,
    initial_watch_config_snapshot,
    watch_runtime_config,
)
from .watch_record_io import (
    LimitIdleResultRequest,
    append_watch_record,
    limit_idle_result,
    watch_sleep_state,
    write_watch_heartbeat,
    write_watch_stopped,
)
from .watch_state import WatchCycleResult, WatchLoopState

if TYPE_CHECKING:
    from ..core import SimpleAgent


# LLM: RunSingleWatchCycleParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存run单个监控cycle参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RunSingleWatchCycleParams:

    cycle: int
    lock_path: Path
    stop_path: Path | None
    router: CapabilityRouter
    cfg: CapabilityConfig
    dispatch_params: DispatchParams
    active_interval: float
    idle_interval: float
    max_consecutive: int
    last_dispatch_had_changes: bool
    idle_record_already_written: bool = False
    no_progress_tracker: DispatchNoProgressTracker | None = None
    max_cycles: int = 0


WatchSubagentsParams = WatchParams


# LLM: WatchLoopParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存监控循环参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class WatchLoopParams:
    params: WatchParams
    cfg: CapabilityConfig
    router: CapabilityRouter
    lock_path: Path
    stop_path: Path | None
    active_interval: float
    idle_interval: float
    max_consecutive: int


# LLM: WatchCycleBuildParams bundles one cycle's dynamic config and progress state.
# 类用途: 构建 RunSingleWatchCycleParams 时集中携带 loop、热加载配置和上轮是否有变化。
@dataclass(frozen=True)
class WatchCycleBuildParams:
    loop: WatchLoopParams
    cycle: int
    last_dispatch_had_changes: bool
    idle_record_already_written: bool
    no_progress_tracker: DispatchNoProgressTracker
    cfg: CapabilityConfig
    router: CapabilityRouter


# LLM: watch_subagents 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进子代理的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def watch_subagents(
    agent: SimpleAgent,
    router: CapabilityRouter,
    capability_config: CapabilityConfig | None,
    *,
    params: WatchParams,
) -> DispatchWatchReport:
    if params.max_cycles < 0:
        raise ValueError("max_cycles 不能小于 0。")
    if params.interval < 0:
        raise ValueError("interval 不能小于 0。")

    from ...subagent import DispatchWatchReport
    from ..dispatch_lock import _DispatchWatchLock

    cfg = capability_config or CapabilityConfig()
    records = []
    lock_path = agent.subagents.workspace / "subagent_dispatch_watch.lock"
    stop_path = Path(params.stop_file) if params.stop_file else None

    active_interval = getattr(agent.config, "dispatch_active_interval", 5)
    idle_interval = getattr(agent.config, "dispatch_idle_interval", 30)
    max_consecutive = getattr(agent.config, "dispatch_max_consecutive_rounds", 20)

    agent._reset_dispatch_rounds()

    with _DispatchWatchLock(lock_path, force=params.force_lock) as lock:
        cycle = _run_watch_cycles(
            agent,
            records,
            WatchLoopParams(
                params=params,
                cfg=cfg,
                router=router,
                lock_path=lock_path,
                stop_path=stop_path,
                active_interval=active_interval,
                idle_interval=idle_interval,
                max_consecutive=max_consecutive,
            ),
        )
        write_watch_stopped(agent, cycle, lock_path, lock.token)

    report = agent.subagents.build_dispatch_watch_report(records, dry_run=not params.apply)
    return agent.subagents.write_dispatch_watch_report(report)


# LLM: _run_watch_cycles 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进cycles的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _run_watch_cycles(
    agent,
    records: list,
    loop: WatchLoopParams,
) -> int:
    cycle = 0
    state = WatchLoopState()
    config_snapshot = initial_watch_config_snapshot(agent, loop.cfg, loop.router)
    while loop.params.max_cycles == 0 or cycle < loop.params.max_cycles:
        if loop.stop_path and loop.stop_path.exists():
            break
        cycle += 1
        runtime = watch_runtime_config(
            WatchRuntimeConfigRequest(agent, loop.cfg, loop.router, config_snapshot)
        )
        config_snapshot = runtime.snapshot
        cycle_params = _watch_cycle_params(
            WatchCycleBuildParams(
                loop=loop,
                cycle=cycle,
                last_dispatch_had_changes=state.last_dispatch_had_changes,
                idle_record_already_written=state.idle_record_written,
                no_progress_tracker=state.no_progress_tracker,
                cfg=runtime.cfg,
                router=runtime.router,
            )
        )
        result = _run_single_watch_cycle(agent, cycle_params)
        if result.record is not None and result.store_record:
            records.append(result.record)
        state.last_dispatch_had_changes = result.had_progress
        state.idle_record_written = _next_idle_record_state(state, result)
        if result.stop_watch:
            break
    return cycle


# LLM: _next_idle_record_state coalesces repeated idle ticks while resetting after real progress.
# 函数用途: 让持续空闲的 gateway 只留一条 idle 记录，出现真实进展后允许下一次 idle 再留证据。
def _next_idle_record_state(state: WatchLoopState, result: WatchCycleResult) -> bool:
    if result.had_progress:
        return False
    if result.record is None:
        return state.idle_record_written
    return state.idle_record_written or result.store_record


# LLM: _watch_cycle_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进cycle参数的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _watch_cycle_params(request: WatchCycleBuildParams) -> RunSingleWatchCycleParams:
    loop = request.loop
    params = request.loop.params
    return RunSingleWatchCycleParams(
        cycle=request.cycle, lock_path=loop.lock_path, stop_path=loop.stop_path, router=request.router, cfg=request.cfg,
        dispatch_params=dispatch_params_from_watch(params),
        active_interval=loop.active_interval, idle_interval=loop.idle_interval,
        max_consecutive=loop.max_consecutive, last_dispatch_had_changes=request.last_dispatch_had_changes,
        idle_record_already_written=request.idle_record_already_written,
        no_progress_tracker=request.no_progress_tracker,
        max_cycles=params.max_cycles,
    )


# LLM: _execute_watch_dispatch 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进execute监控调度的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _execute_watch_dispatch(agent, params):
    try:
        dispatch_report = agent.dispatch_subagents(
            router=params.router,
            capability_config=params.cfg,
            params=params.dispatch_params,
        )
        ok = all(item.ok for item in dispatch_report.records)
        message = f"完成一轮 dispatch，records={len(dispatch_report.records)}。"
        record_count = len(dispatch_report.records)
        dispatch_summary = dispatch_report.summary
        evidence_paths = [
            str(agent.subagents.workspace / "subagent_dispatch_report.json"),
            str(agent.subagents.workspace / "SUBAGENT_DISPATCH.md"),
        ]
    except Exception as exc:
        dispatch_report = None
        ok = False
        message = f"dispatch cycle failed: {exc}"
        record_count = 0
        dispatch_summary = {}
        evidence_paths = []
    return ok, message, record_count, dispatch_summary, evidence_paths, dispatch_report


# LLM: _run_single_watch_cycle 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进单个监控cycle的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _run_single_watch_cycle(
    agent: SimpleAgent,
    params: RunSingleWatchCycleParams,
) -> WatchCycleResult:
    from ..parameters import _sleep_with_stop

    started_at = time_module.time()
    write_watch_heartbeat(agent, params, status="running", message="dispatch cycle started")

    if not _watch_has_dispatch_inputs(agent):
        return _run_idle_watch_cycle(agent, params, started_at=started_at)

    ok, message, record_count, dispatch_summary, evidence_paths, dispatch_report = _execute_watch_dispatch(
        agent, params
    )
    had_progress = _watch_dispatch_had_progress(params, dispatch_report)

    record = append_watch_record(
        agent, params, started_at=started_at, ok=ok, message=message,
        record_count=record_count, dispatch_summary=dispatch_summary, evidence_paths=evidence_paths,
    )
    store_record = had_progress or not _watch_repeated_no_progress(params, dispatch_report)
    if store_record:
        agent.subagents.append_dispatch_watch_log(record)
    _update_dispatch_rounds(agent, had_progress)

    if params.max_consecutive > 0 and agent._consecutive_dispatch_rounds >= params.max_consecutive:
        return limit_idle_result(LimitIdleResultRequest(agent, params, message, record, store_record))

    more_cycles, message = watch_sleep_state(params, message)
    write_watch_heartbeat(
        agent, params, status="sleeping" if more_cycles else "stopping", message=message,
    )
    if not more_cycles:
        return WatchCycleResult(record, store_record, had_progress=had_progress)

    current_interval = params.active_interval if had_progress else params.idle_interval
    if _sleep_with_stop(current_interval, params.stop_path):
        return WatchCycleResult(record, store_record, had_progress=had_progress, stop_watch=True)

    return WatchCycleResult(record, store_record, had_progress=had_progress)


# LLM: _run_idle_watch_cycle keeps persistent gateways alive without creating due-check audit noise.
# 函数用途: 没有任何子代理任务时只写心跳和一条可选 idle 记录，不调用 dispatch_subagents。
def _run_idle_watch_cycle(agent, params: RunSingleWatchCycleParams, *, started_at: float) -> WatchCycleResult:
    from ..parameters import _sleep_with_stop

    message = "暂无子代理任务，watch idle。"
    record = append_watch_record(
        agent,
        params,
        started_at=started_at,
        ok=True,
        message=message,
        record_count=0,
        dispatch_summary={"idle": 1},
        evidence_paths=[],
    )
    store_record = not params.idle_record_already_written
    if store_record:
        agent.subagents.append_dispatch_watch_log(record)
    more_cycles, message = watch_sleep_state(params, message)
    write_watch_heartbeat(
        agent,
        params,
        status="idle" if more_cycles else "stopping",
        message=message,
    )
    if not more_cycles:
        return WatchCycleResult(record, store_record, had_progress=False)
    if _sleep_with_stop(params.idle_interval, params.stop_path):
        return WatchCycleResult(record, store_record, had_progress=False, stop_watch=True)
    return WatchCycleResult(record, store_record, had_progress=False)


# LLM: _watch_has_dispatch_inputs is the cheap preflight before writing dispatch reports.
# 函数用途: 没有任何子代理 run 时跳过 dispatch，避免 gateway 空闲时重复写 due-check/report/index。
def _watch_has_dispatch_inputs(agent) -> bool:
    try:
        return bool(agent.subagents.list_runs())
    except Exception:
        return True


# LLM: _watch_dispatch_had_progress shares dispatch_loop's progress contract with gateway watch.
# 函数用途: 用统一 no-progress 规则判断本轮是否应按活跃间隔继续。
def _watch_dispatch_had_progress(params: RunSingleWatchCycleParams, dispatch_report) -> bool:
    if dispatch_report is None:
        return False
    return dispatch_made_progress(dispatch_report)


# LLM: _watch_repeated_no_progress suppresses repeated audit-only watch records.
# 函数用途: 连续看到相同 due-check/inspect 轮次时，只保留首轮证据，后续靠 heartbeat 表示仍存活。
def _watch_repeated_no_progress(params: RunSingleWatchCycleParams, dispatch_report) -> bool:
    if dispatch_report is None or params.no_progress_tracker is None:
        return False
    return params.no_progress_tracker.should_stop(dispatch_report)


# LLM: _update_dispatch_rounds treats real progress as a reset and audit-only rounds as consecutive idle work.
# 函数用途: 维护 max_consecutive 的计数语义，避免真实推进被误算为空转。
def _update_dispatch_rounds(agent, had_progress: bool) -> None:
    if had_progress:
        agent._reset_dispatch_rounds()
        return
    agent._increment_dispatch_rounds()
