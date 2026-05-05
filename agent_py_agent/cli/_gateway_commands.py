"""Gateway commands: run, status, stop, restart, logs, and their helpers."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..agent.capability_config import load_capability_config
from ..agent.gateway import (
    GatewayPaths,
    gateway_paths,
    is_pid_alive,
    log_gateway_event,
    read_json_file,
    recover_gateway_processing_requests,
    tail_lines,
    terminate_pid,
    wait_for_gateway_running,
    wait_for_pid_exit,
    write_json_file,
)
from ..agent.gateway_parts.daemon_control import (
    get_running_pid,
    read_pid_record,
    read_runtime_status,
    remove_pid_file_if_owned,
    write_pid_record,
)
from ..agent.gateway_parts.http_service import GatewayHTTPServer, start_http_server
from ._gateway_process_service import (
    _gateway_heartbeat_loop,
    _gateway_request_loop,
    _gateway_request_worker_loop,
    _write_gateway_heartbeat,
)
from ._gateway_state_helpers import _build_run_payload, _build_run_state
from .common import ROOT, make_agent, make_capability_router
from .daemon import _resolve_daemon_options
from .gateway_service import (
    install_service,
    uninstall_service,
)
from .models import DaemonOptions

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_gateway_options(agent, args):
    """Resolve gateway run options from agent config and CLI args."""
    return _resolve_daemon_options(agent, args)


def _write_gateway_state(paths, data: dict) -> None:
    """Write gateway state JSON file."""
    write_json_file(paths.state, data)


def _update_gateway_state_running(
    paths,
    agent,
    options,
    pid: int,
    requeued: int,
    failed: int,
    http_port: int,
) -> None:
    """Update state file for running status."""
    write_json_file(paths.state, _build_run_state(paths, agent, options, pid, requeued, failed, http_port))


def _log_gateway_running(agent, options, pid, requeued, failed, http_port) -> None:
    """Log gateway_run_running event."""
    log_gateway_event(agent, "gateway_run_running", _build_run_payload(agent, options, pid, requeued, failed, http_port))


# ---------------------------------------------------------------------------
# cmd_gateway_run helpers
# ---------------------------------------------------------------------------


def _cmd_gateway_run_setup(args, agent, paths):
    """Setup gateway run: recovery and initial logging. Returns requeued count."""
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


def _cmd_gateway_run_threads(args, paths, agent, options, requeued, failed, http_port):
    """Start heartbeat and request threads. Returns (heartbeat_thread, request_thread, http_server)."""
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
    if http_port > 0:
        http_server = start_http_server(http_port, paths)
        _update_gateway_state_running(paths, agent, options, os.getpid(), requeued, failed, http_port)
        _log_gateway_running(agent, options, os.getpid(), requeued, failed, http_port)
    return stop_event, heartbeat_thread, request_thread, http_server


def _cmd_gateway_run_cleanup(stop_event, heartbeat_thread, request_thread, http_server, paths, agent, options, pid):
    """Cleanup gateway run: stop threads, remove pid file, write heartbeat."""
    stop_event.set()
    heartbeat_thread.join(timeout=2)
    request_thread.join(timeout=2)
    if http_server:
        http_server.stop()
    try:
        remove_pid_file_if_owned(paths.pid)
    except OSError:
        pass
    try:
        paths.stop_request.unlink()
    except OSError:
        pass
    _write_gateway_heartbeat(paths, agent, options, status="stopped", pid=pid)
    log_gateway_event(
        agent,
        "gateway_run_cleanup",
        {"status": "cleanup", "pid": pid, "updated_at": time.time()},
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_gateway_install(args) -> int:
    """Install the gateway as a system service (systemd or launchd)."""
    success = install_service(force=args.force)
    return 0 if success else 1


def cmd_gateway_uninstall(args) -> int:
    """Uninstall the gateway system service (systemd or launchd)."""
    success = uninstall_service()
    return 0 if success else 1


def cmd_gateway_start(args) -> int:
    """Start gateway in background."""

    agent = make_agent(args)
    paths = gateway_paths(agent)
    paths.root.mkdir(parents=True, exist_ok=True)
    pid = get_running_pid(paths.pid)
    if pid and is_pid_alive(pid) and not args.force:
        record = read_pid_record(paths.pid)
        print(f"gateway 已在运行 pid={pid}")
        print(f"status: {paths.state}")
        if record:
            print(f"start_time: {record.get('start_time')}")
        return 0
    if pid and is_pid_alive(pid) and args.force:
        paths.stop_request.write_text(
            json.dumps({"requested_at": time.time(), "reason": "force restart before start"}, ensure_ascii=False),
            encoding="utf-8",
        )
        if not wait_for_pid_exit(pid, agent.config.gateway_stop_timeout):
            terminate_pid(pid)
            wait_for_pid_exit(pid, 5)

    try:
        paths.stop_request.unlink()
    except OSError:
        pass

    config_path = Path(args.config).resolve()
    command = [
        sys.executable,
        "-m",
        "agent_py_agent",
        "--config",
        str(config_path),
        "gateway",
        "run",
    ]
    if args.force_lock:
        command.append("--force-lock")

    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        start_new_session = True

    with paths.log.open("ab") as log_file:
        process = subprocess.Popen(
            command,
            cwd=ROOT.parent,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )

    from ..agent.gateway_parts.daemon_control import _get_process_start_time, _utc_now_iso
    paths.pid.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.pid, {
        "pid": process.pid,
        "kind": "my-agent-gateway",
        "argv": command,
        "start_time": _get_process_start_time(process.pid),
        "updated_at": _utc_now_iso(),
    })
    write_json_file(
        paths.state,
        {
            "status": "starting",
            "pid": process.pid,
            "started_at": time.time(),
            "command": command,
            "log": str(paths.log),
        },
    )
    wait_for_gateway_running(paths, timeout=10.0)
    print(f"gateway starting pid={process.pid}")
    print(f"state: {paths.state}")
    print(f"log: {paths.log}")
    return 0


def cmd_gateway_run(args) -> int:
    """内部命令：前台运行 gateway 后台循环。"""
    agent = make_agent(args)
    paths = gateway_paths(agent)
    paths.root.mkdir(parents=True, exist_ok=True)
    requeued, pid = _cmd_gateway_run_setup(args, agent, paths)
    try:
        options = _resolve_gateway_options(agent, args)
    except ValueError as exc:
        _write_gateway_state(paths, {"status": "failed", "pid": pid, "error": str(exc), "updated_at": time.time()})
        log_gateway_event(
            agent,
            "gateway_run_failed",
            {"status": "failed", "pid": pid, "error": str(exc), "updated_at": time.time()},
        )
        print(str(exc), file=sys.stderr)
        return 2

    http_port = getattr(agent.config, "gateway_port", 0) or 0
    stop_event, heartbeat_thread, request_thread, http_server = _cmd_gateway_run_threads(
        args, paths, agent, options, requeued, 0, http_port,
    )

    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)
    exit_code = 0
    try:
        report = agent.watch_subagents(
            router,
            capability_config,
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
            stop_file=paths.stop_request,
        )
        final_status = "stopped" if paths.stop_request.exists() else "exited"
        _write_gateway_state(paths, {
            "status": final_status,
            "pid": pid,
            "stopped_at": time.time(),
            "summary": report.summary,
        })
        log_gateway_event(
            agent,
            "gateway_run_stopped",
            {"status": final_status, "pid": pid, "stopped_at": time.time(), "summary": report.summary},
        )
    except KeyboardInterrupt:
        _write_gateway_state(paths, {"status": "interrupted", "pid": pid, "stopped_at": time.time()})
        log_gateway_event(
            agent,
            "gateway_run_interrupted",
            {"status": "interrupted", "pid": pid, "stopped_at": time.time()},
        )
        exit_code = 130
    except Exception as exc:
        _write_gateway_state(paths, {"status": "failed", "pid": pid, "error": str(exc), "updated_at": time.time()})
        log_gateway_event(
            agent,
            "gateway_run_failed",
            {"status": "failed", "pid": pid, "error": str(exc), "updated_at": time.time()},
        )
        print(str(exc), file=sys.stderr)
        exit_code = 2
    finally:
        _cmd_gateway_run_cleanup(stop_event, heartbeat_thread, request_thread, http_server, paths, agent, options, pid)
    return exit_code


def cmd_gateway_status(args) -> int:
    """显示 gateway 状态。"""
    agent = make_agent(args)
    paths = gateway_paths(agent)
    pid_record = read_pid_record(paths.pid)
    running = is_pid_alive(pid_record["pid"]) if pid_record else False
    state = read_json_file(paths.state) if paths.state.exists() else {}
    http_port = getattr(agent.config, "gateway_port", 0)

    status = "running" if running else "stopped"
    state_status = state.get("status", "unknown")
    print(f"gateway status={status} pid={pid_record.get('pid') if pid_record else 'none'} state={state_status}")
    print(f"  workspace={paths.root}")
    if http_port > 0:
        print(f"  http_port={http_port}")

    if running:
        inbox_count = len(list(paths.inbox.glob("*"))) if paths.inbox.exists() else 0
        processing_count = len(list(paths.processing.glob("*"))) if paths.processing.exists() else 0
        print(f"  inbox={inbox_count} processing={processing_count}")
        print(f"  pid_file={paths.pid}")
        print(f"  state_file={paths.state}")

    if state:
        print(f"  status_detail={json.dumps(state, ensure_ascii=False)}")

    runtime = read_runtime_status(paths)
    if runtime:
        print(f"  last_heartbeat={runtime.get('updated_at', 'none')}")
        print(f"  last_status={runtime.get('status', 'none')}")
    return 0


def cmd_gateway_stop(args) -> int:
    """请求 gateway 正常停止。"""
    agent = make_agent(args)
    paths = gateway_paths(agent)
    pid = get_running_pid(paths.pid)
    if not pid:
        remove_pid_file_if_owned(paths.pid)
        print("gateway 未在运行")
        return 0

    paths.stop_request.write_text(
        json.dumps({"requested_at": time.time(), "reason": args.reason or "user stop"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log_gateway_event(
        agent,
        "gateway_stop_requested",
        {"status": "stop_requested", "pid": pid, "reason": args.reason or "user stop", "updated_at": time.time()},
    )
    timeout = args.timeout if args.timeout is not None else agent.config.gateway_stop_timeout
    if wait_for_pid_exit(pid, timeout):
        log_gateway_event(
            agent,
            "gateway_stopped",
            {"status": "stopped", "pid": pid, "updated_at": time.time()},
        )
        print(f"gateway stopped pid={pid}")
        return 0
    if args.kill:
        terminate_pid(pid)
        if wait_for_pid_exit(pid, 5):
            write_json_file(paths.state, {"status": "killed", "pid": pid, "stopped_at": time.time()})
            log_gateway_event(
                agent,
                "gateway_killed",
                {"status": "killed", "pid": pid, "updated_at": time.time()},
            )
            try:
                paths.pid.unlink()
            except OSError:
                pass
            print(f"gateway killed pid={pid}")
            return 0
    print(f"gateway stop requested but still running pid={pid}", file=sys.stderr)
    return 2


def cmd_gateway_restart(args) -> int:
    """重启 gateway。"""
    stop_args = argparse.Namespace(
        config=args.config,
        timeout=args.timeout,
        kill=args.force,
        reason="gateway restart",
    )
    stop_code = cmd_gateway_stop(stop_args)
    if stop_code not in {0}:
        return stop_code
    start_args = argparse.Namespace(
        config=args.config,
        force=True,
        note=args.note,
        take_over_by=getattr(args, "take_over_by", ""),
        locked_file=getattr(args, "locked_file", []),
        force_lock=getattr(args, "force_lock", False),
        capability_config=getattr(args, "capability_config", None),
        skill_dir=getattr(args, "skill_dir", None),
        interval=0,
        max_cycles=0,
        max_cards=100,
        max_runners=1,
        limit=20,
        apply=False,
        execute_runners=False,
        planner="",
        reviewer="",
        instruction="",
        no_probe=False,
        probe=False,
    )
    return cmd_gateway_run(start_args)


def cmd_gateway_logs(args) -> int:
    """显示 gateway 日志。"""
    agent = make_agent(args)
    paths = gateway_paths(agent)
    if not paths.log.exists():
        print("没有日志文件")
        return 1
    lines = tail_lines(paths.log, args.lines or 50)
    print(lines)
    return 0