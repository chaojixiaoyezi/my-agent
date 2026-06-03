

from __future__ import annotations

import time as time_module
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from ...capabilities import CapabilityRouter
from ...capability_config import CapabilityConfig
from ..orchestration.dispatch.lock import _DispatchWatchLock
from ..orchestration.dispatch.no_progress import DispatchNoProgressTracker, dispatch_made_progress
from ..orchestration.dispatch.params import (
    DispatchParams,
    DispatchRuntimePolicy,
    WatchParams,
    dispatch_params_from_watch,
)
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
    advance: bool = False


WatchSubagentsParams = WatchParams


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


@dataclass(frozen=True)
class WatchCycleBuildParams:
    loop: WatchLoopParams
    cycle: int
    last_dispatch_had_changes: bool
    idle_record_already_written: bool
    no_progress_tracker: DispatchNoProgressTracker
    cfg: CapabilityConfig
    router: CapabilityRouter


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

    cfg = capability_config or CapabilityConfig()
    records = []
    lock_path = agent.subagents.workspace / "subagent_dispatch_watch.lock"
    stop_path = Path(params.stop_file) if params.stop_file else None

    policy = DispatchRuntimePolicy.from_config(getattr(agent, "config", None))

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
                active_interval=policy.active_interval,
                idle_interval=policy.idle_interval,
                max_consecutive=policy.max_consecutive_rounds,
            ),
        )
        write_watch_stopped(agent, cycle, lock_path, lock.token)

    report = agent.subagents.build_dispatch_watch_report(records, dry_run=params.preview_only)
    return agent.subagents.write_dispatch_watch_report(report)


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


def _next_idle_record_state(state: WatchLoopState, result: WatchCycleResult) -> bool:
    if result.had_progress:
        return False
    if result.record is None:
        return state.idle_record_written
    return state.idle_record_written or result.store_record


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
        advance=params.advance,
    )


def _execute_watch_dispatch(agent, params):
    try:
        dispatch_report = agent.dispatch_subagents(
            router=params.router,
            capability_config=params.cfg,
            params=_watch_scoped_dispatch_params(agent, params.dispatch_params),
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


# 避免触发“模型顶层 dispatch 不得猜历史任务”的保护。
def _watch_scoped_dispatch_params(agent: SimpleAgent, dispatch_params: DispatchParams) -> DispatchParams:
    updates: dict[str, object] = {}
    if dispatch_params.include_run_ids or dispatch_params.parent_run_id or dispatch_params.root_id:
        return replace(dispatch_params, **updates) if updates else dispatch_params
    run_ids = [
        str(getattr(task, "id", "") or "")
        for task in agent.subagents.list_runs()
        if str(getattr(task, "id", "") or "").strip()
    ]
    if not run_ids:
        return replace(dispatch_params, **updates) if updates else dispatch_params
    limit = int(getattr(dispatch_params, "limit", 0) or 0)
    scoped = run_ids[:limit] if limit > 0 else run_ids
    updates["include_run_ids"] = scoped
    return replace(dispatch_params, **updates)


def _run_single_watch_cycle(
    agent: SimpleAgent,
    params: RunSingleWatchCycleParams,
) -> WatchCycleResult:
    started_at = time_module.time()
    write_watch_heartbeat(agent, params, status="running", message="dispatch cycle started")

    input_state = _watch_dispatch_input_state(agent)
    if input_state.load_error is not None:
        return _run_watch_input_load_error_cycle(
            agent,
            params,
            started_at=started_at,
            load_error=input_state.load_error,
        )
    if not input_state.has_inputs:
        return _run_idle_watch_cycle(agent, params, started_at=started_at)
    if not params.advance:
        return _run_readonly_watch_cycle(agent, params, started_at=started_at)

    return _run_advancing_watch_cycle(agent, params, started_at=started_at)


def _run_advancing_watch_cycle(
    agent: SimpleAgent,
    params: RunSingleWatchCycleParams,
    *,
    started_at: float,
) -> WatchCycleResult:
    from ..parameters import _sleep_with_stop

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


from .watch_cycle_observe import (
    _run_idle_watch_cycle,
    _run_readonly_watch_cycle,
    _run_watch_input_load_error_cycle,
    _watch_dispatch_input_state,
)


def _watch_dispatch_had_progress(params: RunSingleWatchCycleParams, dispatch_report) -> bool:
    if dispatch_report is None:
        return False
    return dispatch_made_progress(dispatch_report)


def _watch_repeated_no_progress(params: RunSingleWatchCycleParams, dispatch_report) -> bool:
    if dispatch_report is None or params.no_progress_tracker is None:
        return False
    return params.no_progress_tracker.should_stop(dispatch_report)


def _update_dispatch_rounds(agent, had_progress: bool) -> None:
    if had_progress:
        agent._reset_dispatch_rounds()
        return
    agent._increment_dispatch_rounds()
