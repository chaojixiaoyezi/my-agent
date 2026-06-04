

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ..agent.capability.config import load_capability_config
from ..agent.gateway_parts import (
    GatewayPaths,
    gateway_paths,
    is_pid_alive,
    log_gateway_event,
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
from ..agent.gateway_parts.io import read_json_file, read_json_file_report
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
from ._gateway_stop_helpers import _force_kill_gateway
from .common import ROOT, make_agent, make_capability_router
from .daemon import _resolve_daemon_options
from .gateway_service import (
    install_service,
    uninstall_service,
)
from .models import (
    GatewayRunCleanupRequest,
    GatewayRunContext,
    GatewayRunOptions,
    GatewayStartOptions,
    GatewayThreadsRequest,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GatewayRunBuildRequest:
    agent: object
    paths: GatewayPaths
    options: GatewayRunOptions
    args: argparse.Namespace


def _resolve_gateway_options(agent, args):
    options = _resolve_daemon_options(agent, args)
    return GatewayRunOptions(
        mutate_state=options.mutate_state,
        start_runners=options.start_runners,
        planner=options.planner,
        interval=options.interval,
        max_runners=options.max_runners,
        limit=options.limit,
        max_cycles=options.max_cycles,
        max_cards=options.max_cards,
        reviewer=options.reviewer,
        instruction=options.instruction,
        probe=options.probe,
    )


def _gateway_start_options_from_args(args) -> GatewayStartOptions:
    return GatewayStartOptions(config=Path(args.config), force_lock=bool(args.force_lock))


def _gateway_run_context_from_args(request: _GatewayRunBuildRequest) -> GatewayRunContext:
    args = request.args
    return GatewayRunContext(
        agent=request.agent,
        paths=request.paths,
        options=request.options,
        config_path=Path(args.config),
        note=args.note or "",
        take_over_by=args.take_over_by or "",
        locked_files=args.locked_file or [],
        force_lock=args.force_lock,
    )


def _gateway_context_with_router(run_context: GatewayRunContext, args) -> GatewayRunContext:
    capability_config = load_capability_config(args.capability_config)
    run_context.agent.capability_config_path = args.capability_config
    router = make_capability_router(run_context.agent, capability_config, args.skill_dir)
    return GatewayRunContext(
        agent=run_context.agent,
        paths=run_context.paths,
        options=run_context.options,
        config_path=run_context.config_path,
        note=run_context.note,
        take_over_by=run_context.take_over_by,
        locked_files=run_context.locked_files,
        force_lock=run_context.force_lock,
        router=router,
        capability_config=capability_config,
    )


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
    command = _gateway_start_command(_gateway_start_options_from_args(args))
    process = _spawn_gateway_process(paths, command, cwd=ROOT.parent)
    _write_gateway_start_files(paths, pid=process.pid, command=command)
    wait_for_gateway_running(paths, timeout=float(getattr(agent.config, "gateway_ready_timeout_seconds", 10) or 10))
    print(f"gateway starting pid={process.pid}")
    print(f"state: {paths.state}")
    print(f"log: {paths.log}")
    return 0


def cmd_gateway_run(args) -> int:
    agent = make_agent(args)
    paths = gateway_paths(agent)
    paths.root.mkdir(parents=True, exist_ok=True)
    requeued, pid = _cmd_gateway_run_setup(agent, paths)
    try:
        options = _resolve_gateway_options(agent, args)
    except ValueError as exc:
        _record_gateway_run_options_error(paths, agent, pid, exc)
        print(str(exc), file=sys.stderr)
        return 2

    http_port = getattr(agent.config, "gateway_port", 0) or 0
    run_context = _gateway_run_context_from_args(_GatewayRunBuildRequest(agent, paths, options, args))
    stop_event, heartbeat_thread, request_thread, background_thread, http_server = _cmd_gateway_run_threads(
        GatewayThreadsRequest(context=run_context, requeued=requeued, failed=0, http_port=http_port)
    )

    watch_context = _gateway_context_with_router(run_context, args)
    exit_code = 0
    try:
        report = _run_gateway_watch(watch_context)
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
            GatewayRunCleanupRequest(
                context=run_context,
                pid=pid,
                stop_event=stop_event,
                heartbeat_thread=heartbeat_thread,
                request_thread=request_thread,
                background_thread=background_thread,
                http_server=http_server,
            )
        )
    return exit_code


def cmd_gateway_status(args) -> int:
    agent = make_agent(args)
    paths = gateway_paths(agent)
    pid_record = read_pid_record(paths.pid)
    running = is_pid_alive(pid_record["pid"]) if pid_record else False
    state_report = read_json_file_report(paths.state, context="gateway.cli.status.state.read")
    state = state_report.payload
    http_port = getattr(agent.config, "gateway_port", 0)

    status = "running" if running else "stopped"
    state_status = state.get("status", "unknown")
    print(f"gateway status={status} pid={pid_record.get('pid') if pid_record else 'none'} state={state_status}")
    print(f"  workspace={paths.root}")
    if state_report.load_error:
        print("  state_load_error=" + json.dumps(state_report.load_error, ensure_ascii=False, sort_keys=True))
    if http_port > 0:
        print(f"  http_port={http_port}")

    if running:
        inbox_count = len(list(paths.inbox.glob("*.json"))) if paths.inbox.exists() else 0
        processing_count = len(list(paths.processing.glob("*.json"))) if paths.processing.exists() else 0
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
    if args.kill and _force_kill_gateway(agent, paths, pid):
        return 0
    print(f"gateway stop requested but still running pid={pid}", file=sys.stderr)
    return 2


def cmd_gateway_restart(args) -> int:
    stop_args = argparse.Namespace(
        config=args.config,
        timeout=args.timeout,
        kill=True,
        reason="gateway restart",
    )
    stop_code = cmd_gateway_stop(stop_args)
    if stop_code not in {0}:
        return stop_code
    start_args = argparse.Namespace(
        config=args.config,
        force=True,
        force_lock=getattr(args, "force_lock", False),
    )
    return cmd_gateway_start(start_args)


def cmd_gateway_logs(args) -> int:
    agent = make_agent(args)
    paths = gateway_paths(agent)
    if not paths.log.exists():
        print("没有日志文件")
        return 1
    lines = tail_lines(paths.log, args.lines or 50)
    print(lines)
    return 0
