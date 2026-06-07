
from __future__ import annotations

"""gateway background loop functions — request workers, heartbeat, and worker pool management.

给人看的解释：
这个文件放 gateway 后台跑的各种循环：请求处理 worker 池、心跳写入。
从 gateway_process.py 拆出来，让主入口文件更短。
"""

import json
import os
import sys
import threading
import time
from types import SimpleNamespace

from ..agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    FakeChannelHub,
)
from ..agent.core import SimpleAgent
from ..agent.gateway_parts import (
    GatewayPaths,
    _process_gateway_requests,
    gateway_request_counts,
    log_gateway_event,
    recover_gateway_processing_requests,
    write_json_file,
)
from ..agent.runtime_errors import runtime_error_report
from .common import make_agent
from .models import GatewayRunContext, GatewayRunOptions


def _gateway_agent_from_context(context: GatewayRunContext) -> SimpleAgent:
    return make_agent(SimpleNamespace(config=str(context.config_path)))


def _gateway_request_loop(context: GatewayRunContext, paths: GatewayPaths, stop_event: threading.Event) -> None:

    try:
        bootstrap_agent = _gateway_agent_from_context(context)
        worker_count = max(1, int(bootstrap_agent.config.gateway_request_workers or 1))
    except Exception as exc:
        _print_gateway_loop_error("gateway_request_pool.initialize", "pool", exc)
        return
    workers: list[threading.Thread] = []
    for index in range(worker_count):
        thread = threading.Thread(
            target=_gateway_request_worker_loop,
            args=(context, paths, stop_event, index),
            daemon=True,
        )
        thread.start()
        workers.append(thread)
    while not stop_event.is_set():
        stop_event.wait(0.5)
    for thread in workers:
        thread.join(timeout=max(0, int(getattr(bootstrap_agent.config, "gateway_worker_join_timeout_seconds", 2) or 0)))


def _gateway_request_worker_loop(
    context: GatewayRunContext,
    paths: GatewayPaths,
    stop_event: threading.Event,
    worker_index: int,
) -> None:

    try:
        agent = _gateway_agent_from_context(context)
    except Exception as exc:
        _print_gateway_loop_error("gateway_request_worker.initialize", str(worker_index), exc)
        return

    poll_interval = _gateway_request_poll_interval(agent)
    while not stop_event.is_set():
        try:
            _recover_gateway_requests_if_primary(agent, paths, worker_index)
            processed = _process_gateway_requests(agent, paths, worker_id=f"gw-worker-{worker_index}")
        except Exception as exc:
            _print_gateway_loop_error("gateway_request_worker.iteration", str(worker_index), exc)
            processed = 0
        if processed:
            continue
        stop_event.wait(poll_interval)


def _gateway_background_main_loop(context: GatewayRunContext, stop_event: threading.Event) -> None:
    try:
        agent = _gateway_agent_from_context(context)
        runtime = BackgroundMainAgentRuntime(
            agent=agent,
            store=agent.conversation_store,
            channels=FakeChannelHub(),
        )
        scheduler = BackgroundMainAgentScheduler(
            {
                "runtime": runtime,
                "store": agent.conversation_store,
                "collaboration_store": getattr(agent, "collaboration_store", None),
            }
        )
        poll_interval = _background_main_poll_interval(agent)
    except Exception as exc:
        _print_gateway_loop_error("gateway_background_main.initialize", "background-main", exc)
        return

    while not stop_event.is_set():
        try:
            reports = scheduler.tick()
            if reports:
                _record_background_main_reports(agent, reports)
                continue
        except Exception as exc:
            _print_gateway_loop_error("gateway_background_main.iteration", "background-main", exc)
        stop_event.wait(poll_interval)


def _record_background_main_reports(agent: SimpleAgent, reports: list[object]) -> None:
    for report in reports:
        payload = {
            "status": "background_reported",
            "thread_id": str(getattr(report, "thread_id", "") or ""),
            "task_id": str(getattr(report, "task_id", "") or ""),
            "reason": str(getattr(report, "reason", "") or ""),
            "route_channel": str(getattr(report, "route_channel", "") or ""),
            "route_target": str(getattr(report, "route_target", "") or ""),
            "created_at": float(getattr(report, "created_at", 0.0) or 0.0),
        }
        log_gateway_event(agent, "gateway_background_main_reported", payload)
        print(
            "[gateway-background-main] "
            f"reason={payload['reason']} task={payload['task_id']} thread={payload['thread_id']}",
            flush=True,
        )


def _background_main_poll_interval(agent: SimpleAgent) -> float:
    request_interval = _float_config(agent, "gateway_request_poll_interval", default=1.0)
    heartbeat_interval = _float_config(agent, "gateway_heartbeat_interval", default=5.0)
    return max(1.0, min(5.0, request_interval, heartbeat_interval))


def _gateway_request_poll_interval(agent: SimpleAgent) -> float:
    return max(0.05, _float_config(agent, "gateway_request_poll_interval", default=0.2))


def _float_config(agent: SimpleAgent, key: str, *, default: float) -> float:
    try:
        return float(getattr(agent.config, key))
    except (TypeError, ValueError):
        return default


def _recover_gateway_requests_if_primary(agent: SimpleAgent, paths: GatewayPaths, worker_index: int) -> None:
    if worker_index != 0:
        return
    recover_gateway_processing_requests(
        paths,
        startup=False,
        max_attempts=agent.config.gateway_request_max_attempts,
        timeout_seconds=agent.config.gateway_processing_timeout_seconds,
        agent=agent,
    )


def _gateway_heartbeat_loop(context: GatewayRunContext, stop_event: threading.Event) -> None:
    paths = context.paths
    agent = context.agent
    options = context.options

    while not stop_event.is_set():
        _write_gateway_heartbeat(paths, agent, options, status="running", pid=os.getpid())
        stop_event.wait(max(1, agent.config.gateway_heartbeat_interval))


def _write_gateway_heartbeat(
    paths: GatewayPaths,
    agent: SimpleAgent,
    options: GatewayRunOptions,
    *,
    status: str,
    pid: int,
) -> None:
    write_json_file(
        paths.heartbeat,
        {
            "status": status,
            "pid": pid,
            "updated_at": time.time(),
            "gateway_workspace": str(paths.root),
            "subagent_workspace": str(agent.subagents.workspace),
            "mutate_state": options.mutate_state,
            "start_runners": options.start_runners,
            "planner": options.planner,
            "interval": options.interval,
            "max_runners": options.max_runners,
            "max_cycles": options.max_cycles,
            "request_counts": gateway_request_counts(paths, include_archives=False),
        },
    )


def _print_gateway_loop_error(context: str, worker_id: str, exc: BaseException) -> None:
    report = runtime_error_report(exc, context=context)
    report["worker_id"] = worker_id
    print("[gateway-loop-error] " + json.dumps(report, ensure_ascii=False, sort_keys=True), file=sys.stderr)
