# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

import os
import time as time_module
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ...capabilities import CapabilityRouter
from ...capability_config import CapabilityConfig
from ...subagents.services.dispatch_params import DispatchWatchHeartbeatParams
from ..dispatch_params import DispatchParams, WatchParams, dispatch_params_from_watch

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
        _write_watch_stopped(agent, cycle, lock_path, lock.token)

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
    last_dispatch_had_changes = False
    while loop.params.max_cycles == 0 or cycle < loop.params.max_cycles:
        if loop.stop_path and loop.stop_path.exists():
            break
        cycle += 1
        cycle_params = _watch_cycle_params(
            loop,
            cycle,
            last_dispatch_had_changes,
        )
        record = _run_single_watch_cycle(agent, cycle_params)
        records.append(record)
        last_dispatch_had_changes = getattr(record, "dispatch_record_count", 0) > 0
    return cycle


# LLM: _watch_cycle_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进cycle参数的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _watch_cycle_params(
    loop: WatchLoopParams,
    cycle: int,
    last_dispatch_had_changes: bool,
) -> RunSingleWatchCycleParams:
    params = loop.params
    return RunSingleWatchCycleParams(
        cycle=cycle, lock_path=loop.lock_path, stop_path=loop.stop_path, router=loop.router, cfg=loop.cfg,
        dispatch_params=dispatch_params_from_watch(params),
        active_interval=loop.active_interval, idle_interval=loop.idle_interval,
        max_consecutive=loop.max_consecutive, last_dispatch_had_changes=last_dispatch_had_changes,
        max_cycles=params.max_cycles,
    )


# LLM: _write_watch_stopped 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 写入stopped的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def _write_watch_stopped(agent, cycle: int, lock_path: Path, token: str) -> None:
    agent.subagents.write_dispatch_watch_heartbeat(
        params=DispatchWatchHeartbeatParams(
            cycle=cycle,
            status="stopped",
            lock_path=str(lock_path),
            pid=os.getpid(),
            message=f"watch stopped; lock={token}",
        ),
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
        ok = False
        message = f"dispatch cycle failed: {exc}"
        record_count = 0
        dispatch_summary = {}
        evidence_paths = []
    return ok, message, record_count, dispatch_summary, evidence_paths


# LLM: _run_single_watch_cycle 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进单个监控cycle的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _run_single_watch_cycle(
    agent: SimpleAgent,
    params: RunSingleWatchCycleParams,
) -> DispatchWatchRecord:
    from ..parameters import _sleep_with_stop

    started_at = time_module.time()
    _write_watch_heartbeat(agent, params, status="running", message="dispatch cycle started")

    ok, message, record_count, dispatch_summary, evidence_paths = _execute_watch_dispatch(
        agent, params
    )

    record = _append_watch_record(
        agent,
        params,
        started_at=started_at,
        ok=ok,
        message=message,
        record_count=record_count,
        dispatch_summary=dispatch_summary,
        evidence_paths=evidence_paths,
    )
    agent.subagents.append_dispatch_watch_log(record)
    agent._increment_dispatch_rounds()

    if agent._consecutive_dispatch_rounds >= params.max_consecutive:
        _write_stopped_by_limit(agent, params, message)
        return record

    more_cycles, message = _watch_sleep_state(params, message)
    _write_watch_heartbeat(
        agent,
        params,
        status="sleeping" if more_cycles else "stopping",
        message=message,
    )
    if not more_cycles:
        return record

    current_interval = (
        params.active_interval if params.last_dispatch_had_changes else params.idle_interval
    )
    if _sleep_with_stop(current_interval, params.stop_path):
        return record

    return record


# LLM: _append_watch_record 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 写入append监控记录的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def _append_watch_record(
    agent,
    params: RunSingleWatchCycleParams,
    *,
    started_at: float,
    ok: bool,
    message: str,
    record_count: int,
    dispatch_summary: dict,
    evidence_paths: list[str],
) -> DispatchWatchRecord:
    from ..dispatch_service import MakeDispatchWatchRecordParams, make_dispatch_watch_record

    watch_record_params = MakeDispatchWatchRecordParams(
        cycle=params.cycle,
        dry_run=not params.dispatch_params.apply,
        ok=ok,
        message=message,
        dispatch_record_count=record_count,
        dispatch_summary=dispatch_summary,
        started_at=started_at,
        ended_at=time_module.time(),
        evidence_paths=evidence_paths,
    )
    return make_dispatch_watch_record(agent, watch_record_params)


# LLM: _write_watch_heartbeat 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 写入heartbeat的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def _write_watch_heartbeat(
    agent,
    params: RunSingleWatchCycleParams,
    *,
    status: str,
    message: str,
) -> None:
    agent.subagents.write_dispatch_watch_heartbeat(
        params=DispatchWatchHeartbeatParams(
            cycle=params.cycle,
            status=status,
            lock_path=str(params.lock_path),
            pid=os.getpid(),
            message=message,
        ),
    )


# LLM: _write_stopped_by_limit 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 写入stopped限制的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def _write_stopped_by_limit(agent, params: RunSingleWatchCycleParams, message: str) -> None:
    message = f"{message} 已达到最大连续轮数限制 ({params.max_consecutive})，停止调度。"
    agent.subagents.write_dispatch_watch_heartbeat(
        params=DispatchWatchHeartbeatParams(
            cycle=params.cycle,
            status="stopped_by_limit",
            lock_path=str(params.lock_path),
            pid=os.getpid(),
            message=message,
        ),
    )


# LLM: _watch_sleep_state 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进sleep状态的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _watch_sleep_state(params: RunSingleWatchCycleParams, message: str) -> tuple[bool, str]:
    more_cycles = params.max_cycles == 0 or params.cycle < params.max_cycles
    if params.stop_path and params.stop_path.exists():
        return False, f"{message} stop requested."
    return more_cycles, message
