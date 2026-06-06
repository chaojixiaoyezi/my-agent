

from __future__ import annotations

import json
import os
import time as time_module
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.capability.runtime_config_models import CapabilityConfigSnapshot
from agent_py_agent.agent.capability.runtime_config_reload import (
    default_capability_config_path,
    load_capability_config_snapshot,
    reload_capability_config_if_changed,
)

from ...runtime_errors import runtime_error_report
from ...subagents.services.dispatch.params import (
    DispatchWatchHeartbeatParams,
    DispatchWatchRecordParams,
)
from ..orchestration.dispatch.lock import _DispatchWatchLock
from ..orchestration.dispatch.no_progress import DispatchNoProgressTracker, dispatch_made_progress
from ..orchestration.dispatch.params import (
    DispatchParams,
    DispatchRuntimePolicy,
    WatchParams,
    dispatch_params_from_watch,
)

if TYPE_CHECKING:
    from ..core import SimpleAgent


@dataclass(frozen=True)
class WatchCycleResult:
    record: object | None
    store_record: bool
    had_progress: bool
    stop_watch: bool = False


@dataclass
class WatchLoopState:
    last_dispatch_had_changes: bool = False
    idle_record_written: bool = False
    no_progress_tracker: DispatchNoProgressTracker = field(default_factory=DispatchNoProgressTracker)


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


@dataclass(frozen=True)
class WatchRuntimeConfigRequest:
    agent: object
    current_cfg: CapabilityConfig
    router: CapabilityRouter
    snapshot: CapabilityConfigSnapshot | None


@dataclass(frozen=True)
class WatchRuntimeConfigResult:
    cfg: CapabilityConfig
    router: CapabilityRouter
    snapshot: CapabilityConfigSnapshot | None


@dataclass(frozen=True)
class LimitIdleResultRequest:
    agent: object
    params: RunSingleWatchCycleParams
    message: str
    record: object
    store_record: bool


@dataclass(frozen=True)
class WatchDispatchInputState:
    has_inputs: bool
    load_error: dict[str, object] | None = None


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

    from ...subagents import DispatchWatchReport

    cfg = capability_config or CapabilityConfig()
    records = []
    lock_path = agent.subagents.workspace / "subagent_dispatch_watch.lock"
    stop_path = Path(params.stop_file) if params.stop_file else None

    policy = DispatchRuntimePolicy.from_config(getattr(agent, "config", None))
    interval = max(0.0, float(params.interval or 0.0))

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
                active_interval=interval,
                idle_interval=interval,
                max_consecutive=policy.max_consecutive_rounds,
            ),
        )
        write_watch_stopped(agent, cycle, lock_path, lock.token)

    report = agent.subagents.dispatch.build_dispatch_watch_report(records, dry_run=params.preview_only)
    return agent.subagents.dispatch.write_dispatch_watch_report(report)


def initial_watch_config_snapshot(
    agent: object,
    cfg: CapabilityConfig,
    router: CapabilityRouter,
) -> CapabilityConfigSnapshot | None:
    path = Path(
        getattr(agent, "capability_config_path", "")
        or default_capability_config_path(getattr(agent, "root", "."))
    )
    if not path.exists():
        router.config = cfg
        return None
    return _load_initial_snapshot(agent, cfg, router, path)


def watch_runtime_config(request: WatchRuntimeConfigRequest) -> WatchRuntimeConfigResult:
    if request.snapshot is None:
        return WatchRuntimeConfigResult(request.current_cfg, request.router, None)
    try:
        result = reload_capability_config_if_changed(request.snapshot, router=request.router)
    except (FileNotFoundError, OSError, ValueError):
        return WatchRuntimeConfigResult(request.current_cfg, request.router, request.snapshot)
    if result.changed:
        request.agent._capability_config_runtime_snapshot = result.snapshot
        _write_watch_config_reload(request.agent, result.snapshot)
    return WatchRuntimeConfigResult(result.snapshot.config, request.router, result.snapshot)


def _load_initial_snapshot(
    agent: object,
    cfg: CapabilityConfig,
    router: CapabilityRouter,
    path: Path,
) -> CapabilityConfigSnapshot | None:
    try:
        snapshot = load_capability_config_snapshot(path)
    except (FileNotFoundError, OSError, ValueError):
        router.config = cfg
        return None
    agent.capability_config_path = path
    agent._capability_config_runtime_snapshot = snapshot
    router.config = snapshot.config
    return snapshot


def _write_watch_config_reload(agent: object, snapshot: CapabilityConfigSnapshot) -> None:
    path = agent.subagents.workspace / "capability_config_hot_reload.jsonl"
    payload = {
        "kind": "capability_config_hot_reload",
        "version": snapshot.version,
        "path": str(snapshot.path),
        "mtime_ns": snapshot.mtime_ns,
        "size": snapshot.size,
        "ts": time_module.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def write_watch_stopped(agent: object, cycle: int, lock_path, token: str) -> None:
    agent.subagents.dispatch.write_dispatch_watch_heartbeat(
        params=DispatchWatchHeartbeatParams(
            cycle=cycle,
            status="stopped",
            lock_path=str(lock_path),
            pid=os.getpid(),
            message=f"watch stopped; lock={token}",
        ),
    )


def append_watch_record(
    agent: object,
    params: RunSingleWatchCycleParams,
    *,
    started_at: float,
    ok: bool,
    message: str,
    record_count: int,
    dispatch_summary: dict,
    evidence_paths: list[str],
):
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
    from ..orchestration.dispatch.service import make_dispatch_watch_record

    return make_dispatch_watch_record(agent, watch_record_params)


def write_watch_heartbeat(agent: object, params: RunSingleWatchCycleParams, *, status: str, message: str) -> None:
    agent.subagents.dispatch.write_dispatch_watch_heartbeat(
        params=DispatchWatchHeartbeatParams(
            cycle=params.cycle,
            status=status,
            lock_path=str(params.lock_path),
            pid=os.getpid(),
            message=message,
        ),
    )


def write_idle_by_limit(agent: object, params: RunSingleWatchCycleParams, message: str) -> None:
    message = f"{message} 已达到最大连续空转轮数 ({params.max_consecutive})，进入空闲等待。"
    agent.subagents.dispatch.write_dispatch_watch_heartbeat(
        params=DispatchWatchHeartbeatParams(
            cycle=params.cycle,
            status="idle_by_limit",
            lock_path=str(params.lock_path),
            pid=os.getpid(),
            message=message,
        ),
    )


def watch_sleep_state(params: RunSingleWatchCycleParams, message: str) -> tuple[bool, str]:
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


def _run_readonly_watch_cycle(
    agent: SimpleAgent,
    params: RunSingleWatchCycleParams,
    *,
    started_at: float,
) -> WatchCycleResult:
    from ..agent_tree.status import agent_tree_status_payload
    from ..parameters import _sleep_with_stop

    payload = agent_tree_status_payload(agent, {"root_id": params.dispatch_params.root_id})
    nodes = payload.get("nodes") if isinstance(payload, dict) else []
    node_count = len(nodes) if isinstance(nodes, list) else 0
    message = f"read-only watch observed agent tree nodes={node_count}; advance=false。"
    record = append_watch_record(
        agent,
        params,
        started_at=started_at,
        ok=True,
        message=message,
        record_count=0,
        dispatch_summary={
            "inspect_agent_tree": 1,
            "agent_tree_nodes": node_count,
            "read_only": 1,
        },
        evidence_paths=[],
    )
    store_record = not params.idle_record_already_written
    if store_record:
        agent.subagents.dispatch.append_dispatch_watch_log(record)
    more_cycles, message = watch_sleep_state(params, message)
    write_watch_heartbeat(
        agent,
        params,
        status="observing" if more_cycles else "stopping",
        message=message,
    )
    if not more_cycles:
        return WatchCycleResult(record, store_record, had_progress=False)
    if _sleep_with_stop(params.idle_interval, params.stop_path):
        return WatchCycleResult(record, store_record, had_progress=False, stop_watch=True)
    return WatchCycleResult(record, store_record, had_progress=False)


def _run_idle_watch_cycle(
    agent: SimpleAgent,
    params: RunSingleWatchCycleParams,
    *,
    started_at: float,
) -> WatchCycleResult:
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
        agent.subagents.dispatch.append_dispatch_watch_log(record)
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


def _run_watch_input_load_error_cycle(
    agent: SimpleAgent,
    params: RunSingleWatchCycleParams,
    *,
    started_at: float,
    load_error: dict[str, object],
) -> WatchCycleResult:
    from ..parameters import _sleep_with_stop

    message = (
        "watch 无法读取子代理任务列表；本轮不推进也不当作空闲。"
        f" error={json.dumps(load_error, ensure_ascii=False)}"
    )
    record = append_watch_record(
        agent,
        params,
        started_at=started_at,
        ok=False,
        message=message,
        record_count=0,
        dispatch_summary={
            "list_runs_load_error": load_error,
            "read_only": 1,
        },
        evidence_paths=[],
    )
    store_record = True
    agent.subagents.dispatch.append_dispatch_watch_log(record)
    more_cycles, message = watch_sleep_state(params, message)
    write_watch_heartbeat(
        agent,
        params,
        status="load_error" if more_cycles else "stopping",
        message=message,
    )
    if not more_cycles:
        return WatchCycleResult(record, store_record, had_progress=False)
    if _sleep_with_stop(params.idle_interval, params.stop_path):
        return WatchCycleResult(record, store_record, had_progress=False, stop_watch=True)
    return WatchCycleResult(record, store_record, had_progress=False)


def _watch_dispatch_input_state(agent: SimpleAgent) -> WatchDispatchInputState:
    try:
        return WatchDispatchInputState(has_inputs=bool(agent.subagents.list_runs()))
    except Exception as exc:
        return WatchDispatchInputState(
            has_inputs=False,
            load_error=runtime_error_report(exc, context="watch.subagents.list_runs"),
        )


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
        agent.subagents.dispatch.append_dispatch_watch_log(record)
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
