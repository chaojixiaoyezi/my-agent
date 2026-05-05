"""Gateway state management helpers."""

from __future__ import annotations

import time


def _build_run_state(
    paths,
    agent,
    options,
    pid: int,
    requeued: int,
    failed: int,
    http_port: int,
    status: str = "running",
) -> dict:
    """Build state dict for running/interrupted/failed statuses."""
    return {
        "status": status,
        "pid": pid,
        "started_at": time.time(),
        "gateway_workspace": str(paths.root),
        "subagent_workspace": str(agent.subagents.workspace),
        "apply": options.apply,
        "execute_runners": options.execute_runners,
        "planner": options.planner,
        "interval": options.interval,
        "max_runners": options.max_runners,
        "max_cycles": options.max_cycles,
        "requeued_requests": requeued,
        "failed_processing_requests": failed,
        "request_workers": max(1, int(agent.config.gateway_request_workers or 1)),
        "http_port": http_port,
    }


def _build_run_payload(
    agent,
    options,
    pid: int,
    requeued: int,
    failed: int,
    http_port: int,
    extra: dict | None = None,
) -> dict:
    """Build event payload dict for gateway_run events."""
    payload = {
        "status": "running",
        "pid": pid,
        "gateway_workspace": str(agent.subagents.workspace),
        "subagent_workspace": str(agent.subagents.workspace),
        "apply": options.apply,
        "execute_runners": options.execute_runners,
        "planner": options.planner,
        "interval": options.interval,
        "max_runners": options.max_runners,
        "max_cycles": options.max_cycles,
        "requeued_requests": requeued,
        "failed_processing_requests": failed,
        "request_workers": max(1, int(agent.config.gateway_request_workers or 1)),
        "http_port": http_port,
    }
    if extra:
        payload.update(extra)
    return payload