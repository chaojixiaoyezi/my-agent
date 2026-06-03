
from __future__ import annotations

import json
from dataclasses import dataclass

from ...runtime_errors import runtime_error_report
from .watch_record_io import append_watch_record, watch_sleep_state, write_watch_heartbeat
from .watch_state import WatchCycleResult


@dataclass(frozen=True)
class WatchDispatchInputState:
    has_inputs: bool
    load_error: dict[str, object] | None = None


def _run_readonly_watch_cycle(agent, params: RunSingleWatchCycleParams, *, started_at: float) -> WatchCycleResult:
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
        agent.subagents.append_dispatch_watch_log(record)
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


def _run_watch_input_load_error_cycle(
    agent,
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
    agent.subagents.append_dispatch_watch_log(record)
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


def _watch_dispatch_input_state(agent) -> WatchDispatchInputState:
    try:
        return WatchDispatchInputState(has_inputs=bool(agent.subagents.list_runs()))
    except Exception as exc:
        return WatchDispatchInputState(
            has_inputs=False,
            load_error=runtime_error_report(exc, context="watch.subagents.list_runs"),
        )
