
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..agent.gateway import log_gateway_event, recover_gateway_processing_requests, write_json_file
from ..agent.gateway_parts.daemon_control import (
    _get_process_start_time,
    _utc_now_iso,
    remove_pid_file_if_owned,
    write_pid_record,
)
from ..agent.gateway_parts.http_service import GatewayHTTPServer, start_http_server
from ._gateway_process_service import (
    _gateway_heartbeat_loop,
    _gateway_request_loop,
    _write_gateway_heartbeat,
)


def _build_run_state(context: dict, status: str = "running") -> dict:
    paths = context["paths"]
    agent = context["agent"]
    options = context["options"]
    return {
        "status": status,
        "pid": context["pid"],
        "started_at": time.time(),
        "gateway_workspace": str(paths.root),
        "subagent_workspace": str(agent.subagents.workspace),
        "apply": options.apply,
        "execute_runners": options.execute_runners,
        "planner": options.planner,
        "interval": options.interval,
        "max_runners": options.max_runners,
        "max_cycles": options.max_cycles,
        "requeued_requests": context["requeued"],
        "failed_processing_requests": context["failed"],
        "request_workers": max(1, int(agent.config.gateway_request_workers or 1)),
        "http_port": context["http_port"],
    }


def _build_run_payload(context: dict, extra: dict | None = None) -> dict:
    agent = context["agent"]
    options = context["options"]
    payload = {
        "status": "running",
        "pid": context["pid"],
        "gateway_workspace": str(agent.subagents.workspace),
        "subagent_workspace": str(agent.subagents.workspace),
        "apply": options.apply,
        "execute_runners": options.execute_runners,
        "planner": options.planner,
        "interval": options.interval,
        "max_runners": options.max_runners,
        "max_cycles": options.max_cycles,
        "requeued_requests": context["requeued"],
        "failed_processing_requests": context["failed"],
        "request_workers": max(1, int(agent.config.gateway_request_workers or 1)),
        "http_port": context["http_port"],
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


def _gateway_start_command(args) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "agent_py_agent",
        "--config",
        str(Path(args.config).resolve()),
        "gateway",
        "run",
    ]
    if args.force_lock:
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


def _cmd_gateway_run_setup(args, agent, paths):
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


def _cmd_gateway_run_threads(context: dict):
    args = context["args"]
    paths = context["paths"]
    agent = context["agent"]
    options = context["options"]
    stop_event = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_gateway_heartbeat_loop,
        args=(paths, agent, options, stop_event),
        daemon=True,
    )
    heartbeat_thread.start()
    request_thread = threading.Thread(
        target=_gateway_request_loop,
        args=(args, paths, stop_event),
        daemon=True,
    )
    request_thread.start()
    http_server: GatewayHTTPServer | None = None
    http_port = context["http_port"]
    if http_port > 0:
        http_server = start_http_server(http_port, paths)
        state_context = {**context, "pid": os.getpid()}
        write_json_file(paths.state, _build_run_state(state_context))
        log_gateway_event(agent, "gateway_run_running", _build_run_payload(state_context))
    return stop_event, heartbeat_thread, request_thread, http_server


def _cmd_gateway_run_cleanup(context: dict):
    context["stop_event"].set()
    context["heartbeat_thread"].join(timeout=2)
    context["request_thread"].join(timeout=2)
    if context["http_server"]:
        context["http_server"].stop()
    paths = context["paths"]
    try:
        remove_pid_file_if_owned(paths.pid)
    except OSError:
        pass
    _clear_gateway_stop_request(paths)
    _write_gateway_heartbeat(
        paths,
        context["agent"],
        context["options"],
        status="stopped",
        pid=context["pid"],
    )
    log_gateway_event(
        context["agent"],
        "gateway_run_cleanup",
        {"status": "cleanup", "pid": context["pid"], "updated_at": time.time()},
    )


def _run_gateway_watch(context: dict):
    args = context["args"]
    options = context["options"]
    return context["agent"].watch_subagents(
        context["router"],
        context["capability_config"],
        apply=options.apply,
        execute_runners=options.execute_runners,
        planner=options.planner,
        max_runners=options.max_runners,
        limit=options.limit,
        reviewer=options.reviewer,
        note=args.note or "",
        runner_instruction=options.instruction or "",
        max_cards=options.max_cards,
        probe=options.probe,
        take_over_by=args.take_over_by or "",
        locked_files=args.locked_file or [],
        interval=options.interval,
        max_cycles=options.max_cycles,
        force_lock=args.force_lock,
        stop_file=context["paths"].stop_request,
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
    write_json_file(
        paths.state,
        {
            "status": "starting",
            "pid": pid,
            "started_at": time.time(),
            "command": command,
            "log": str(paths.log),
        },
    )
