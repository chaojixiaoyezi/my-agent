"""LLM: watch mode orchestration for continuous dispatch.

给人看的解释：
封装 watch 循环的编排逻辑，包括单轮执行、状态持久化、停止条件判断。
"""

from __future__ import annotations

import os
import time as time_module
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ...capabilities import CapabilityRouter
from ...capability_config import CapabilityConfig

if TYPE_CHECKING:
    from ..core import SimpleAgent


@dataclass(frozen=True)
class RunSingleWatchCycleParams:
    """Bundle of all _run_single_watch_cycle parameters."""

    cycle: int
    lock_path: Path
    stop_path: Path | None
    router: CapabilityRouter
    cfg: CapabilityConfig
    apply: bool
    execute_runners: bool
    planner: bool
    workflow_mode: str
    max_runners: int
    limit: int
    reviewer: str
    note: str
    runner_instruction: str
    max_cards: int
    probe: bool
    take_over_by: str
    locked_files: list[str] | None
    active_interval: float
    idle_interval: float
    max_consecutive: int
    last_dispatch_had_changes: bool
    max_cycles: int = 0


@dataclass(frozen=True)
class WatchSubagentsParams:
    """Bundle of all watch_subagents parameters."""

    apply: bool = False
    execute_runners: bool = False
    planner: bool = False
    workflow_mode: str = "off"
    max_runners: int = 1
    limit: int = 20
    reviewer: str = "parent-dispatch"
    note: str = ""
    runner_instruction: str = ""
    max_cards: int = 0
    probe: bool = True
    take_over_by: str = ""
    locked_files: list[str] | None = None
    interval: float = 30.0
    max_cycles: int = 0
    force_lock: bool = False
    stop_file: str | Path | None = None


def watch_subagents(
    agent: SimpleAgent,
    router: CapabilityRouter,
    capability_config: CapabilityConfig | None,
    *,
    params: WatchSubagentsParams,
) -> DispatchWatchReport:
    """以 watch 模式持续执行父代理调度。

    内部管理循环、锁、状态持久化和报告生成。
    """
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
        cycle = 0
        last_dispatch_had_changes = False
        while params.max_cycles == 0 or cycle < params.max_cycles:
            if stop_path and stop_path.exists():
                break
            cycle += 1
            cycle_params = RunSingleWatchCycleParams(
                cycle=cycle,
                lock_path=lock_path,
                stop_path=stop_path,
                router=router,
                cfg=cfg,
                apply=params.apply,
                execute_runners=params.execute_runners,
                planner=params.planner,
                workflow_mode=params.workflow_mode,
                max_runners=params.max_runners,
                limit=params.limit,
                reviewer=params.reviewer,
                note=params.note,
                runner_instruction=params.runner_instruction,
                max_cards=params.max_cards,
                probe=params.probe,
                take_over_by=params.take_over_by,
                locked_files=params.locked_files,
                active_interval=active_interval,
                idle_interval=idle_interval,
                max_consecutive=max_consecutive,
                last_dispatch_had_changes=last_dispatch_had_changes,
                max_cycles=params.max_cycles,
            )
            record = _run_single_watch_cycle(agent, cycle_params)
            records.append(record)
            last_dispatch_had_changes = getattr(record, "dispatch_record_count", 0) > 0

        agent.subagents.write_dispatch_watch_heartbeat(
            cycle=cycle,
            status="stopped",
            lock_path=str(lock_path),
            pid=os.getpid(),
            message=f"watch stopped; lock={lock.token}",
        )

    report = agent.subagents.build_dispatch_watch_report(records, dry_run=not params.apply)
    return agent.subagents.write_dispatch_watch_report(report)


def _run_single_watch_cycle(
    agent: SimpleAgent,
    params: RunSingleWatchCycleParams,
) -> DispatchWatchRecord:
    """Run a single watch cycle and return the dispatch watch record."""
    from ..dispatch_service import MakeDispatchWatchRecordParams, make_dispatch_watch_record
    from ..parameters import _sleep_with_stop

    started_at = time_module.time()
    agent.subagents.write_dispatch_watch_heartbeat(
        cycle=params.cycle,
        status="running",
        lock_path=str(params.lock_path),
        pid=os.getpid(),
        message="dispatch cycle started",
    )
    try:
        from ..dispatch_mixin import DispatchParams

        dispatch_params = DispatchParams(
            apply=params.apply,
            execute_runners=params.execute_runners,
            planner=params.planner,
            workflow_mode=params.workflow_mode,
            max_runners=params.max_runners,
            limit=params.limit,
            reviewer=params.reviewer,
            note=params.note,
            runner_instruction=params.runner_instruction,
            max_cards=params.max_cards,
            probe=params.probe,
            take_over_by=params.take_over_by,
            locked_files=params.locked_files,
        )
        dispatch_report = agent.dispatch_subagents(
            router=params.router,
            capability_config=params.cfg,
            apply=params.apply,
            execute_runners=params.execute_runners,
            planner=params.planner,
            workflow_mode=params.workflow_mode,
            max_runners=params.max_runners,
            limit=params.limit,
            reviewer=params.reviewer,
            note=params.note,
            runner_instruction=params.runner_instruction,
            max_cards=params.max_cards,
            probe=params.probe,
            take_over_by=params.take_over_by,
            locked_files=params.locked_files,
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

    ended_at = time_module.time()
    watch_record_params = MakeDispatchWatchRecordParams(
        cycle=params.cycle,
        dry_run=not params.apply,
        ok=ok,
        message=message,
        dispatch_record_count=record_count,
        dispatch_summary=dispatch_summary,
        started_at=started_at,
        ended_at=ended_at,
        evidence_paths=evidence_paths,
    )
    record = make_dispatch_watch_record(agent, watch_record_params)
    agent.subagents.append_dispatch_watch_log(record)
    agent._increment_dispatch_rounds()

    if agent._consecutive_dispatch_rounds >= params.max_consecutive:
        message = f"{message} 已达到最大连续轮数限制 ({params.max_consecutive})，停止调度。"
        agent.subagents.write_dispatch_watch_heartbeat(
            cycle=params.cycle,
            status="stopped_by_limit",
            lock_path=str(params.lock_path),
            pid=os.getpid(),
            message=message,
        )
        return record

    more_cycles = params.max_cycles == 0 or params.cycle < params.max_cycles
    stop_requested = bool(params.stop_path and params.stop_path.exists())
    if stop_requested:
        more_cycles = False
        message = f"{message} stop requested."
    agent.subagents.write_dispatch_watch_heartbeat(
        cycle=params.cycle,
        status="sleeping" if more_cycles else "stopping",
        lock_path=str(params.lock_path),
        pid=os.getpid(),
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
