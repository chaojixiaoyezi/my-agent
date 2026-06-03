
from __future__ import annotations

import os
import time as time_module
from dataclasses import dataclass
from typing import Any

from ...subagents.services.dispatch.params import DispatchWatchHeartbeatParams
from .watch_state import WatchCycleResult


@dataclass(frozen=True)
class LimitIdleResultRequest:
    agent: Any
    params: Any
    message: str
    record: Any
    store_record: bool


def write_watch_stopped(agent: Any, cycle: int, lock_path, token: str) -> None:
    agent.subagents.write_dispatch_watch_heartbeat(
        params=DispatchWatchHeartbeatParams(
            cycle=cycle,
            status="stopped",
            lock_path=str(lock_path),
            pid=os.getpid(),
            message=f"watch stopped; lock={token}",
        ),
    )


def append_watch_record(
    agent: Any,
    params: Any,
    *,
    started_at: float,
    ok: bool,
    message: str,
    record_count: int,
    dispatch_summary: dict,
    evidence_paths: list[str],
):
    from ...subagents.services.dispatch.params import DispatchWatchRecordParams
    from ..orchestration.dispatch.service import make_dispatch_watch_record

    watch_record_params = DispatchWatchRecordParams(
        cycle=params.cycle,
        dry_run=params.dispatch_params.preview_only,
        ok=ok,
        message=message,
        dispatch_record_count=record_count,
        dispatch_summary=dispatch_summary,
        started_at=started_at,
        ended_at=time_module.time(),
        evidence_paths=evidence_paths,
    )
    return make_dispatch_watch_record(agent, watch_record_params)


def write_watch_heartbeat(agent: Any, params: Any, *, status: str, message: str) -> None:
    agent.subagents.write_dispatch_watch_heartbeat(
        params=DispatchWatchHeartbeatParams(
            cycle=params.cycle,
            status=status,
            lock_path=str(params.lock_path),
            pid=os.getpid(),
            message=message,
        ),
    )


def write_idle_by_limit(agent: Any, params: Any, message: str) -> None:
    message = f"{message} 已达到最大连续空转轮数 ({params.max_consecutive})，进入空闲等待。"
    agent.subagents.write_dispatch_watch_heartbeat(
        params=DispatchWatchHeartbeatParams(
            cycle=params.cycle,
            status="idle_by_limit",
            lock_path=str(params.lock_path),
            pid=os.getpid(),
            message=message,
        ),
    )


def watch_sleep_state(params: Any, message: str) -> tuple[bool, str]:
    more_cycles = params.max_cycles == 0 or params.cycle < params.max_cycles
    if params.stop_path and params.stop_path.exists():
        return False, f"{message} stop requested."
    return more_cycles, message


def limit_idle_result(request: LimitIdleResultRequest) -> WatchCycleResult:
    from ..parameters import _sleep_with_stop

    write_idle_by_limit(request.agent, request.params, request.message)
    more_cycles, _ = watch_sleep_state(request.params, request.message)
    if more_cycles and _sleep_with_stop(request.params.idle_interval, request.params.stop_path):
        return WatchCycleResult(request.record, request.store_record, had_progress=False, stop_watch=True)
    request.agent._reset_dispatch_rounds()
    return WatchCycleResult(request.record, request.store_record, had_progress=False)
