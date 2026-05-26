# LLM: Read-only and idle watch cycle helpers; keep watch observation separate from dispatch advancement.
# 模块用途: 实现 watch 默认观察和无任务 idle，不调用 dispatch_subagents。

from __future__ import annotations

from .watch_record_io import append_watch_record, watch_sleep_state, write_watch_heartbeat
from .watch_state import WatchCycleResult


# LLM: _run_readonly_watch_cycle observes the agent tree without dispatching runners or clearing work.
# 函数用途: watch 默认只巡检代理树状态；需要推进时必须显式 advance，避免看状态误触发调度。
def _run_readonly_watch_cycle(agent, params: RunSingleWatchCycleParams, *, started_at: float) -> WatchCycleResult:
    from ..agent_tree_status import agent_tree_status_payload
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
