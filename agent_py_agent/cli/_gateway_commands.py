
from __future__ import annotations

import argparse
import json
import sys
import time

from ..agent.capability_config import load_capability_config
from ..agent.gateway import (
    GatewayPaths,
    gateway_paths,
    is_pid_alive,
    log_gateway_event,
    read_json_file,
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
)
from ._gateway_state_helpers import (
    _clear_gateway_stop_request,
    _cmd_gateway_run_cleanup,
    _cmd_gateway_run_setup,
    _cmd_gateway_run_threads,
    _gateway_start_command,
    _record_gateway_run_failed,
    _record_gateway_run_interrupted,
    _record_gateway_run_options_error,
    _record_gateway_run_stopped,
    _run_gateway_watch,
    _spawn_gateway_process,
    _write_gateway_start_files,
    _write_gateway_stop_request,
)
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
    return _resolve_daemon_options(agent, args)


def _write_gateway_state(paths, data: dict) -> None:
    write_json_file(paths.state, data)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_gateway_install(args) -> int:
    success = install_service(force=args.force)
    return 0 if success else 1


def cmd_gateway_uninstall(args) -> int:
    success = uninstall_service()
    return 0 if success else 1


def cmd_gateway_start(args) -> int:
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
        _write_gateway_stop_request(paths, reason="force restart before start")
        if not wait_for_pid_exit(pid, agent.config.gateway_stop_timeout):
            terminate_pid(pid)
            wait_for_pid_exit(pid, 5)
    _clear_gateway_stop_request(paths)
    command = _gateway_start_command(args)
    process = _spawn_gateway_process(paths, command, cwd=ROOT.parent)
    _write_gateway_start_files(paths, pid=process.pid, command=command)
    wait_for_gateway_running(paths, timeout=10.0)
    print(f"gateway starting pid={process.pid}")
    print(f"state: {paths.state}")
    print(f"log: {paths.log}")
    return 0


def cmd_gateway_run(args) -> int:
    agent = make_agent(args)
    paths = gateway_paths(agent)
    paths.root.mkdir(parents=True, exist_ok=True)
    requeued, pid = _cmd_gateway_run_setup(args, agent, paths)
    try:
        options = _resolve_gateway_options(agent, args)
    except ValueError as exc:
        _record_gateway_run_options_error(paths, agent, pid, exc)
        print(str(exc), file=sys.stderr)
        return 2

    http_port = getattr(agent.config, "gateway_port", 0) or 0
    run_context = {"args": args, "paths": paths, "agent": agent, "options": options}
    stop_event, heartbeat_thread, request_thread, http_server = _cmd_gateway_run_threads(
        {**run_context, "requeued": requeued, "failed": 0, "http_port": http_port}
    )

    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)
    exit_code = 0
    try:
        report = _run_gateway_watch(
            {**run_context, "router": router, "capability_config": capability_config}
        )
        _record_gateway_run_stopped(paths, agent, pid, report.summary)
    except KeyboardInterrupt:
        _record_gateway_run_interrupted(paths, agent, pid)
        exit_code = 130
    except Exception as exc:
        _record_gateway_run_failed(paths, agent, pid, exc)
        print(str(exc), file=sys.stderr)
        exit_code = 2
    finally:
        _cmd_gateway_run_cleanup(
            {
                **run_context,
                "pid": pid,
                "stop_event": stop_event,
                "heartbeat_thread": heartbeat_thread,
                "request_thread": request_thread,
                "http_server": http_server,
            }
        )
    return exit_code


def cmd_gateway_status(args) -> int:
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

    runtime = read_runtime_status(paths.state)
    if runtime:
        print(f"  last_heartbeat={runtime.get('updated_at', 'none')}")
        print(f"  last_status={runtime.get('status', 'none')}")
    return 0


def cmd_gateway_stop(args) -> int:
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
        note=getattr(args, "note", None),
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
    agent = make_agent(args)
    paths = gateway_paths(agent)
    if not paths.log.exists():
        print("没有日志文件")
        return 1
    lines = tail_lines(paths.log, args.lines or 50)
    print(lines)
    return 0
