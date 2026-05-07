
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..agent.agent_core.dispatch_params import WatchParams
from ..agent.gateway import log_gateway_event, recover_gateway_processing_requests, write_json_file
from ..agent.gateway_parts.daemon_control import (
    _get_process_start_time,
    _utc_now_iso,
    remove_pid_file_if_owned,
    write_pid_record,
)
from ..agent.gateway_parts.http_service import GatewayHTTPServer, start_http_server
from .gateway_loops import (
    _gateway_heartbeat_loop,
    _gateway_request_loop,
    _write_gateway_heartbeat,
)
from .models import (
    GatewayRunCleanupRequest,
    GatewayRunContext,
    GatewayStartOptions,
    GatewayThreadsRequest,
)


def _build_run_state(request: GatewayThreadsRequest, pid: int, status: str = "running") -> dict:
    context = request.context
    paths = context.paths
    agent = context.agent
    options = context.options
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
        "requeued_requests": request.requeued,
        "failed_processing_requests": request.failed,
        "request_workers": max(1, int(agent.config.gateway_request_workers or 1)),
        "http_port": request.http_port,
    }


def _build_run_payload(request: GatewayThreadsRequest, pid: int, extra: dict | None = None) -> dict:
    context = request.context
    agent = context.agent
    options = context.options
    payload = {
        "status": "running",
        "pid": pid,
        "gateway_workspace": str(context.paths.root),
        "subagent_workspace": str(agent.subagents.workspace),
        "apply": options.apply,
        "execute_runners": options.execute_runners,
        "planner": options.planner,
        "interval": options.interval,
        "max_runners": options.max_runners,
        "max_cycles": options.max_cycles,
        "requeued_requests": request.requeued,
        "failed_processing_requests": request.failed,
        "request_workers": max(1, int(agent.config.gateway_request_workers or 1)),
        "http_port": request.http_port,
    }
    if extra:
        payload.update(extra)
    return payload


def _clear_gateway_stop_request(paths) -> None:
    try:
        paths.stop_request.unlink()
    except OSError:
        pass


def _write_gateway_stop_request(paths, *, reason: str) -> None:
    paths.stop_request.write_text(
        json.dumps({"requested_at": time.time(), "reason": reason}, ensure_ascii=False),
        encoding="utf-8",
    )


def _gateway_start_command(options: GatewayStartOptions) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "agent_py_agent",
        "--config",
        str(options.config.expanduser().resolve()),
        "gateway",
        "run",
    ]
    if options.force_lock:
        command.append("--force-lock")
    return command


def _gateway_popen_options() -> tuple[int, bool]:
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return flags, False
    return 0, True


def _spawn_gateway_process(paths, command: list[str], *, cwd: Path):
    creationflags, start_new_session = _gateway_popen_options()
    with paths.log.open("ab") as log_file:
        return subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )


def _write_gateway_start_files(paths, *, pid: int, command: list[str]) -> None:
    paths.pid.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(
        paths.pid,
        {
            "pid": pid,
            "kind": "my-agent-gateway",
            "argv": command,
            "start_time": _get_process_start_time(pid),
            "updated_at": _utc_now_iso(),
        },
    )


def _cmd_gateway_run_setup(agent, paths):
    for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        path.mkdir(parents=True, exist_ok=True)
    recovery = recover_gateway_processing_requests(
        paths,
        startup=True,
        max_attempts=agent.config.gateway_request_max_attempts,
        timeout_seconds=agent.config.gateway_processing_timeout_seconds,
        agent=agent,
    )
    requeued = recovery["requeued"]
    pid = os.getpid()
    write_pid_record(paths.pid)
    log_gateway_event(
        agent,
        "gateway_run_started",
        {
            "status": "starting",
            "pid": pid,
            "gateway_workspace": str(paths.root),
            "subagent_workspace": str(agent.subagents.workspace),
            "requeued_requests": requeued,
            "failed_processing_requests": recovery["failed"],
        },
    )
    return requeued, pid


def _cmd_gateway_run_threads(request: GatewayThreadsRequest):
    context = request.context
    paths = context.paths
    agent = context.agent
    options = context.options
    stop_event = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_gateway_heartbeat_loop,
        args=(context, stop_event),
        daemon=True,
    )
    heartbeat_thread.start()
    request_thread = threading.Thread(
        target=_gateway_request_loop,
        args=(context, paths, stop_event),
        daemon=True,
    )
    request_thread.start()
    http_server: GatewayHTTPServer | None = None
    http_port = request.http_port
    if http_port > 0:
        http_server = start_http_server(http_port, paths)
        pid = os.getpid()
        write_json_file(paths.state, _build_run_state(request, pid))
        log_gateway_event(agent, "gateway_run_running", _build_run_payload(request, pid))
    return stop_event, heartbeat_thread, request_thread, http_server


def _cmd_gateway_run_cleanup(request: GatewayRunCleanupRequest):
    request.stop_event.set()
    request.heartbeat_thread.join(timeout=2)
    request.request_thread.join(timeout=2)
    if request.http_server:
        request.http_server.stop()
    paths = request.context.paths
    try:
        remove_pid_file_if_owned(paths.pid)
    except OSError:
        pass
    _clear_gateway_stop_request(paths)
    _write_gateway_heartbeat(
        paths,
        request.context.agent,
        request.context.options,
        status="stopped",
        pid=request.pid,
    )
    log_gateway_event(
        request.context.agent,
        "gateway_run_cleanup",
        {"status": "cleanup", "pid": request.pid, "updated_at": time.time()},
    )


def _run_gateway_watch(context: GatewayRunContext):
    return context.agent.watch_subagents(
        context.router,
        context.capability_config,
        params=_gateway_watch_params(context),
    )


def _gateway_watch_params(context: GatewayRunContext) -> WatchParams:
    # LLM: gateway run context is the bundle; watch params are derived in one place.
    options = context.options
    return WatchParams(
        apply=options.apply,
        execute_runners=options.execute_runners,
        planner=options.planner,
        max_runners=options.max_runners,
        limit=options.limit,
        reviewer=options.reviewer,
        note=context.note,
        runner_instruction=options.instruction or "",
        max_cards=options.max_cards,
        probe=options.probe,
        take_over_by=context.take_over_by,
        locked_files=context.locked_files,
        interval=options.interval,
        max_cycles=options.max_cycles,
        force_lock=context.force_lock,
        stop_file=context.paths.stop_request,
    )


def _record_gateway_run_options_error(paths, agent, pid: int, exc: Exception) -> None:
    payload = {"status": "failed", "pid": pid, "error": str(exc), "updated_at": time.time()}
    write_json_file(paths.state, payload)
    log_gateway_event(agent, "gateway_run_failed", payload)


def _record_gateway_run_stopped(paths, agent, pid: int, summary: str) -> None:
    final_status = "stopped" if paths.stop_request.exists() else "exited"
    payload = {"status": final_status, "pid": pid, "stopped_at": time.time(), "summary": summary}
    write_json_file(paths.state, payload)
    log_gateway_event(agent, "gateway_run_stopped", payload)


def _record_gateway_run_interrupted(paths, agent, pid: int) -> None:
    payload = {"status": "interrupted", "pid": pid, "stopped_at": time.time()}
    write_json_file(paths.state, payload)
    log_gateway_event(agent, "gateway_run_interrupted", payload)


def _record_gateway_run_failed(paths, agent, pid: int, exc: Exception) -> None:
    payload = {"status": "failed", "pid": pid, "error": str(exc), "updated_at": time.time()}
    write_json_file(paths.state, payload)
    log_gateway_event(agent, "gateway_run_failed", payload)
