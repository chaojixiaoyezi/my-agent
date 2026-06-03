
from __future__ import annotations

"""daemon process helpers for CLI channel adapters."""

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent.common.json_io import JsonObjectReadReport, read_json_object_report
from ..agent.gateway_parts.daemon_control import (
    get_running_pid,
    is_pid_alive,
    remove_pid_file_if_owned,
)
from ..agent.gateway_parts.process_control import terminate_pid, wait_for_pid_exit
from .models import AdapterOptions


@dataclass(frozen=True)
class AdapterDaemonRequest:
    agent: object
    gpaths: object
    pid_file: Path
    options: AdapterOptions


@dataclass(frozen=True)
class AdapterStopRequest:
    options: AdapterOptions
    gpaths: object
    pid_file: Path
    pid: int


def daemonize_adapter(request: AdapterDaemonRequest) -> int:
    existing_pid = get_running_pid(request.pid_file)
    if existing_pid is not None:
        print(f"adapter already running (PID {existing_pid}) or PID file exists", file=sys.stderr)
        print(f"use stop first, or delete {request.pid_file} before retrying", file=sys.stderr)
        return 1

    process = _start_adapter_daemon_process(request.agent, request.gpaths, request.options)
    return _wait_for_adapter_pid(process, request.pid_file)


def _start_adapter_daemon_process(agent, gpaths, options: AdapterOptions) -> subprocess.Popen:
    cmd = [
        sys.executable,
        "-m",
        "agent_py_agent",
        "--config",
        str(agent.config.config_path),
        "adapter",
        "start",
        "--channel",
        options.channel,
    ]
    creationflags, start_new_session = _daemon_subprocess_flags()
    with gpaths.log.open("ab") as log_file:
        return subprocess.Popen(
            cmd,
            cwd=str(agent.root),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )


def _daemon_subprocess_flags() -> tuple[int, bool]:
    if os.name != "nt":
        return 0, True
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return flags, False


def _wait_for_adapter_pid(process: subprocess.Popen, pid_file: Path) -> int:
    deadline = time.time() + 30.0
    while time.time() < deadline:
        child_pid = get_running_pid(pid_file)
        if child_pid is not None:
            print(f"adapter started (PID {child_pid}), PID file: {pid_file}", file=sys.stderr)
            return 0
        if not is_pid_alive(process.pid):
            print("adapter process failed to start", file=sys.stderr)
            return 1
        time.sleep(0.2)
    print("adapter start timed out before PID file appeared", file=sys.stderr)
    return 1


def print_daemon_adapter_status(pid: int, pid_file: Path, gpaths) -> None:
    print(f"adapter running: pid={pid}", file=sys.stderr)
    record = read_pid_record(pid_file)
    if record:
        print(f"  start_time: {record.get('start_time', 'unknown')}", file=sys.stderr)
    state_report = read_adapter_state_report(gpaths)
    state = state_report.payload
    if state_report.load_error:
        print(f"  state_load_error={state_report.load_error.get('context')}", file=sys.stderr)
    if state:
        print(f"  state: {state.get('state', 'unknown')}", file=sys.stderr)


def read_adapter_state(gpaths) -> dict[str, Any] | None:
    payload = read_adapter_state_report(gpaths).payload
    return payload or None


def read_adapter_state_report(gpaths) -> JsonObjectReadReport:
    state_path = gpaths.root / "adapter_state.json"
    return read_json_object_report(state_path, context="cli.adapter_daemon.state.read")


def read_pid_record(path: Path):
    from ..agent.gateway_parts.daemon_control import _read_json_file

    return _read_json_file(path)


def stop_adapter_daemon(request: AdapterStopRequest) -> int:
    print(f"stopping adapter (PID {request.pid})...", file=sys.stderr)
    _write_stop_request(request.gpaths)
    if wait_for_pid_exit(request.pid, timeout=request.options.stop_timeout):
        remove_pid_file_if_owned(request.pid_file)
        print("adapter stopped", file=sys.stderr)
        return 0
    terminate_pid(request.pid)
    if wait_for_pid_exit(request.pid, timeout=5.0):
        remove_pid_file_if_owned(request.pid_file)
        print("adapter force-stopped", file=sys.stderr)
        return 0
    print("adapter stop failed", file=sys.stderr)
    return 1


def _write_stop_request(gpaths) -> None:
    stop_request_path = gpaths.root / "adapter_stop.request"
    stop_request_path.parent.mkdir(parents=True, exist_ok=True)
    stop_request_path.write_text(
        json.dumps({"requested_at": time.time(), "reason": "user request"}, ensure_ascii=False),
        encoding="utf-8",
    )
