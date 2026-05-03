"""LLM: watch mode orchestration for continuous dispatch.

给人看的解释：
封装 watch 循环的编排逻辑，包括单轮执行、状态持久化、停止条件判断。
"""

from __future__ import annotations

import os
import time as time_module
from pathlib import Path
from typing import TYPE_CHECKING

from ...capabilities import CapabilityRouter
from ...capability_config import CapabilityConfig

if TYPE_CHECKING:
    from ..core import SimpleAgent


def watch_subagents(
    agent: SimpleAgent,
    router: CapabilityRouter,
    capability_config: CapabilityConfig | None,
    *,
    apply: bool,
    execute_runners: bool,
    planner: bool,
    workflow_mode: str,
    max_runners: int,
    limit: int,
    reviewer: str,
    note: str,
    runner_instruction: str,
    max_cards: int,
    probe: bool,
    take_over_by: str,
    locked_files: list[str] | None,
    interval: float,
    max_cycles: int,
    force_lock: bool,
    stop_file: str | Path | None,
) -> DispatchWatchReport:
    """以 watch 模式持续执行父代理调度。

    内部管理循环、锁、状态持久化和报告生成。
    """
    if max_cycles < 0:
        raise ValueError("max_cycles 不能小于 0。")
    if interval < 0:
        raise ValueError("interval 不能小于 0。")

    from ..subagent import DispatchWatchReport
    from .dispatch_lock import _DispatchWatchLock

    cfg = capability_config or CapabilityConfig()
    records = []
    lock_path = agent.subagents.workspace / "subagent_dispatch_watch.lock"
    stop_path = Path(stop_file) if stop_file else None

    active_interval = getattr(agent.config, "dispatch_active_interval", 5)
    idle_interval = getattr(agent.config, "dispatch_idle_interval", 30)
    max_consecutive = getattr(agent.config, "dispatch_max_consecutive_rounds", 20)

    agent._reset_dispatch_rounds()

    with _DispatchWatchLock(lock_path, force=force_lock) as lock:
        cycle = 0
        last_dispatch_had_changes = False
        while max_cycles == 0 or cycle < max_cycles:
            if stop_path and stop_path.exists():
                break
            cycle += 1
            record = _run_single_watch_cycle(
                agent, cycle, lock_path, stop_path, router, cfg, apply, execute_runners,
                planner, workflow_mode, max_runners, limit, reviewer, note,
                runner_instruction, max_cards, probe, take_over_by, locked_files,
                active_interval, idle_interval, max_consecutive, last_dispatch_had_changes,
                max_cycles=max_cycles,
            )
            records.append(record)
            last_dispatch_had_changes = getattr(record, 'dispatch_record_count', 0) > 0

        agent.subagents.write_dispatch_watch_heartbeat(
            cycle=cycle,
            status="stopped",
            lock_path=str(lock_path),
            pid=os.getpid(),
            message=f"watch stopped; lock={lock.token}",
        )

    report = agent.subagents.build_dispatch_watch_report(records, dry_run=not apply)
    return agent.subagents.write_dispatch_watch_report(report)


def _run_single_watch_cycle(
    agent: SimpleAgent,
    cycle: int,
    lock_path: Path,
    stop_path: Path | None,
    router: CapabilityRouter,
    cfg: CapabilityConfig,
    apply: bool,
    execute_runners: bool,
    planner: bool,
    workflow_mode: str,
    max_runners: int,
    limit: int,
    reviewer: str,
    note: str,
    runner_instruction: str,
    max_cards: int,
    probe: bool,
    take_over_by: str,
    locked_files: list[str] | None,
    active_interval: float,
    idle_interval: float,
    max_consecutive: int,
    last_dispatch_had_changes: bool,
    max_cycles: int = 0,
):
    """Run a single watch cycle and return the dispatch watch record."""
    from .dispatch_service import make_dispatch_watch_record
    from .parameters import _sleep_with_stop

    started_at = time_module.time()
    agent.subagents.write_dispatch_watch_heartbeat(
        cycle=cycle, status="running", lock_path=str(lock_path),
        pid=os.getpid(), message="dispatch cycle started",
    )
    try:
        dispatch_report = agent.dispatch_subagents(
            router, cfg, apply=apply, execute_runners=execute_runners,
            planner=planner, workflow_mode=workflow_mode, max_runners=max_runners,
            limit=limit, reviewer=reviewer, note=note, runner_instruction=runner_instruction,
            max_cards=max_cards, probe=probe, take_over_by=take_over_by,
            locked_files=locked_files or [],
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
    record = make_dispatch_watch_record(
        agent, cycle=cycle, dry_run=not apply, ok=ok, message=message,
        dispatch_record_count=record_count, dispatch_summary=dispatch_summary,
        started_at=started_at, ended_at=ended_at, evidence_paths=evidence_paths,
    )
    agent.subagents.append_dispatch_watch_log(record)
    agent._increment_dispatch_rounds()

    if agent._consecutive_dispatch_rounds >= max_consecutive:
        message = f"{message} 已达到最大连续轮数限制 ({max_consecutive})，停止调度。"
        agent.subagents.write_dispatch_watch_heartbeat(
            cycle=cycle, status="stopped_by_limit", lock_path=str(lock_path),
            pid=os.getpid(), message=message,
        )
        return record

    more_cycles = max_cycles == 0 or cycle < max_cycles
    stop_requested = bool(stop_path and stop_path.exists())
    if stop_requested:
        more_cycles = False
        message = f"{message} stop requested."
    agent.subagents.write_dispatch_watch_heartbeat(
        cycle=cycle, status="sleeping" if more_cycles else "stopping",
        lock_path=str(lock_path), pid=os.getpid(), message=message,
    )
    if not more_cycles:
        return record

    current_interval = active_interval if last_dispatch_had_changes else idle_interval
    if _sleep_with_stop(current_interval, stop_path):
        return record

    return record