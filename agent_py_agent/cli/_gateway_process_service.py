
from __future__ import annotations

import os
import sys
import threading
import time
from types import SimpleNamespace

from ..agent.gateway import (
    _process_gateway_requests,
    gateway_paths,
    gateway_request_counts,
    recover_gateway_processing_requests,
    write_json_file,
)
from .common import make_agent
from .models import GatewayRunContext, GatewayRunOptions


def _gateway_agent_from_context(context: GatewayRunContext):
    # LLM: legacy gateway process service now rebuilds workers from a context bundle.
    return make_agent(SimpleNamespace(config=str(context.config_path)))


def _gateway_request_loop(context: GatewayRunContext, paths, stop_event: threading.Event) -> None:

    try:
        bootstrap_agent = _gateway_agent_from_context(context)
        worker_count = max(1, int(bootstrap_agent.config.gateway_request_workers or 1))
    except Exception as exc:
        print(f"gateway request worker failed to initialize: {exc}", file=sys.stderr)
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
        thread.join(timeout=2)


def _gateway_request_worker_loop(
    context: GatewayRunContext,
    paths,
    stop_event: threading.Event,
    worker_index: int,
) -> None:

    try:
        agent = _gateway_agent_from_context(context)
    except Exception as exc:
        print(f"gateway request worker {worker_index} failed to initialize: {exc}", file=sys.stderr)
        return

    poll_interval = max(1, int(agent.config.gateway_request_poll_interval))
    while not stop_event.is_set():
        try:
            _recover_gateway_requests_if_primary(agent, paths, worker_index)
            processed = _process_gateway_requests(agent, paths, worker_id=f"gw-worker-{worker_index}")
        except Exception as exc:
            print(f"gateway request worker {worker_index} failed: {exc}", file=sys.stderr)
            processed = 0
        if processed:
            continue
        stop_event.wait(poll_interval)


def _recover_gateway_requests_if_primary(agent, paths, worker_index: int) -> None:
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
    paths,
    agent,
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
            "apply": options.apply,
            "execute_runners": options.execute_runners,
            "planner": options.planner,
            "interval": options.interval,
            "max_runners": options.max_runners,
            "max_cycles": options.max_cycles,
            "request_counts": gateway_request_counts(paths),
        },
    )
