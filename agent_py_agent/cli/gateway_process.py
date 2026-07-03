

from __future__ import annotations

"""Public gateway command implementation.

Gateway process commands live in this module directly so CLI registration,
tests, and runtime entrypoints all target the same command surface.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..agent.agent_core.orchestration.dispatch.params import DispatchExecutionPlan, WatchParams
from ..agent.capability.config import load_capability_config
from ..agent.gateway_parts import (
    GatewayPaths,
    gateway_paths,
    gateway_request_counts,
    gateway_running,
    is_pid_alive,
    log_gateway_event,
    recover_gateway_processing_requests,
    tail_lines,
    terminate_pid,
    wait_for_gateway_running,
    wait_for_pid_exit,
    write_json_file,
)
from ..agent.gateway_parts.daemon_control import (
    _get_process_start_time,
    _utc_now_iso,
    get_running_pid,
    read_pid_record,
    remove_pid_file_if_owned,
    write_pid_record,
)
from ..agent.auth.manager import AuthManager
from ..agent.auth.middleware import AuthMiddleware
from ..agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
    start_http_server,
)
from ..agent.gateway_parts.io import read_json_file, read_json_file_report
from .common import ROOT, make_agent, make_capability_router
from .daemon import _resolve_daemon_options
from .gateway_loops import (
    _gateway_background_main_loop,
    _gateway_heartbeat_loop,
    _gateway_request_loop,
    _write_gateway_heartbeat,
)
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


def _gateway_start_options_from_args(args, *, workspace_root: str | Path = "") -> GatewayStartOptions:
    return GatewayStartOptions(
        config=Path(args.config),
        force_lock=bool(args.force_lock),
        workspace_root=str(workspace_root or ""),
    )


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
        "mutate_state": options.mutate_state,
        "start_runners": options.start_runners,
        "planner": options.planner,
        "interval": options.interval,
        "max_runners": options.max_runners,
        "max_cycles": options.max_cycles,
        "requeued_requests": request.requeued,
        "failed_processing_requests": request.failed,
        "user_inflight_limit": max(1, int(getattr(agent.config, "gateway_user_inflight_limit", 8) or 8)),
        "global_inflight_limit": max(1, int(getattr(agent.config, "gateway_global_inflight_limit", 500) or 500)),
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
        "mutate_state": options.mutate_state,
        "start_runners": options.start_runners,
        "planner": options.planner,
        "interval": options.interval,
        "max_runners": options.max_runners,
        "max_cycles": options.max_cycles,
        "requeued_requests": request.requeued,
        "failed_processing_requests": request.failed,
        "user_inflight_limit": max(1, int(getattr(agent.config, "gateway_user_inflight_limit", 8) or 8)),
        "global_inflight_limit": max(1, int(getattr(agent.config, "gateway_global_inflight_limit", 500) or 500)),
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
    if options.workspace_root:
        command.extend(["--workspace-root", str(Path(options.workspace_root).expanduser().resolve())])
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


def _gateway_ready_for_pid(paths: GatewayPaths, pid: int) -> bool:
    for path, context in (
        (paths.state, "gateway.start.state.read"),
        (paths.heartbeat, "gateway.start.heartbeat.read"),
    ):
        report = read_json_file_report(path, context=context)
        payload = report.payload
        if int(payload.get("pid") or 0) == pid and str(payload.get("status") or "") == "running":
            return True
    return False


def _wait_for_gateway_start_ready(paths: GatewayPaths, process, *, timeout: float) -> bool:
    deadline = time.time() + max(0.0, timeout)
    while time.time() <= deadline:
        if _gateway_ready_for_pid(paths, int(process.pid)):
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.2)
    return _gateway_ready_for_pid(paths, int(process.pid))


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
    write_json_file(
        paths.state,
        {
            "status": "starting",
            "pid": pid,
            "gateway_workspace": str(paths.root),
            "subagent_workspace": str(agent.subagents.workspace),
            "requeued_requests": requeued,
            "failed_processing_requests": recovery["failed"],
            "started_at": time.time(),
        },
    )
    print(f"[gateway-run] status=starting pid={pid}", flush=True)
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


def _build_gateway_auth_middleware(config) -> AuthMiddleware | None:
    """按 config 接线鉴权:auth_enabled 时返回强制鉴权的中间件;关闭则返回 None。

    返回 None 时,网关只允许绑回环(http_service._guard_network_exposure fail-closed),
    不会出现"绑 0.0.0.0 + 无鉴权"的未认证远程入口。每个请求均经过鉴权校验。
    """
    if not getattr(config, "auth_enabled", True):
        return None
    manager = AuthManager(admin_user_id=getattr(config, "admin_user_id", "admin"), auth_enabled=True)
    return AuthMiddleware(manager, auth_token=getattr(config, "gateway_auth_token", ""))


def _cmd_gateway_run_threads(request: GatewayThreadsRequest):
    context = request.context
    paths = context.paths
    agent = context.agent
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
    background_thread = threading.Thread(
        target=_gateway_background_main_loop,
        args=(context, stop_event),
        daemon=True,
    )
    background_thread.start()
    http_server: GatewayHTTPServer | None = None
    http_port = request.http_port
    pid = os.getpid()
    if http_port > 0:
        http_params = GatewayHTTPServerParams(
            auth_middleware=_build_gateway_auth_middleware(agent.config),
            bind_host=getattr(agent.config, "gateway_bind_host", "127.0.0.1"),
        )
        http_server = start_http_server(http_port, paths, params=http_params)
    write_json_file(paths.state, _build_run_state(request, pid))
    log_gateway_event(agent, "gateway_run_running", _build_run_payload(request, pid))
    print(
        "[gateway-run] "
        f"status=running pid={pid} inflight_limits=user:{max(1, int(getattr(agent.config, 'gateway_user_inflight_limit', 8) or 8))}"
        f"/global:{max(1, int(getattr(agent.config, 'gateway_global_inflight_limit', 500) or 500))} "
        f"http_port={http_port}",
        flush=True,
    )
    return stop_event, heartbeat_thread, request_thread, background_thread, http_server


def _cmd_gateway_run_cleanup(request: GatewayRunCleanupRequest):
    request.stop_event.set()
    request.heartbeat_thread.join(timeout=2)
    request.request_thread.join(timeout=2)
    request.background_thread.join(timeout=2)
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
    print(f"[gateway-run] status=stopped pid={request.pid}", flush=True)


def _run_gateway_watch(context: GatewayRunContext):
    return context.agent.watch_subagents(
        context.router,
        context.capability_config,
        params=_gateway_watch_params(context),
    )


def _gateway_watch_params(context: GatewayRunContext) -> WatchParams:
    options = context.options
    return WatchParams(
        execution_plan=DispatchExecutionPlan.from_parts(
            mutate_state=options.mutate_state,
            start_runners=options.start_runners,
            max_runners=options.max_runners,
        ),
        planner=options.planner,
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
        advance=True,
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
    command = _gateway_start_command(_gateway_start_options_from_args(args, workspace_root=agent.root))
    process = _spawn_gateway_process(paths, command, cwd=ROOT.parent)
    _write_gateway_start_files(paths, pid=process.pid, command=command)
    ready = _wait_for_gateway_start_ready(
        paths,
        process,
        timeout=float(getattr(agent.config, "gateway_ready_timeout_seconds", 10) or 10),
    )
    print(f"gateway starting pid={process.pid}")
    print(f"state: {paths.state}")
    print(f"log: {paths.log}")
    if not ready:
        print("gateway started but did not become ready before timeout", file=sys.stderr)
        return 2
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
    state_status = _gateway_state_status_for_display(running, state)
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
        print(f"  status_detail={json.dumps(_gateway_state_detail_for_display(running, state), ensure_ascii=False)}")

    heartbeat_report = read_json_file_report(paths.heartbeat, context="gateway.cli.status.heartbeat.read")
    heartbeat = heartbeat_report.payload
    if heartbeat:
        print(f"  last_heartbeat={heartbeat.get('updated_at', 'none')}")
        print(f"  last_status={_gateway_state_status_for_display(running, heartbeat)}")
    if heartbeat_report.load_error:
        print("  heartbeat_load_error=" + json.dumps(heartbeat_report.load_error, ensure_ascii=False, sort_keys=True))
    return 0


def _gateway_state_status_for_display(running: bool, state: dict) -> str:
    status = str(state.get("status") or "unknown")
    if not running and status == "running":
        return "stale-running"
    return status


def _gateway_state_detail_for_display(running: bool, state: dict) -> dict:
    detail = dict(state)
    if not running and detail.get("status") == "running":
        detail["stale_state"] = True
        detail["stale_state_reason"] = "pid_not_alive"
    return detail


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


def _force_kill_gateway(agent, paths: GatewayPaths, pid: int) -> bool:
    terminate_pid(pid)
    if not wait_for_pid_exit(pid, 5):
        return False
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
    return True


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
    lines = _current_gateway_log_lines(paths.log, args.lines or 50)
    for line in lines:
        print(line)
    return 0


def _current_gateway_log_lines(path: Path, line_count: int) -> list[str]:
    lines = tail_lines(path, 0)
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith("[gateway-run] status=starting"):
            lines = lines[index:]
            break
    if line_count <= 0:
        return lines
    return lines[-line_count:]


__all__ = [
    "cmd_gateway_install",
    "cmd_gateway_start",
    "cmd_gateway_status",
    "cmd_gateway_stop",
    "cmd_gateway_restart",
    "cmd_gateway_logs",
    "cmd_gateway_uninstall",
    "cmd_gateway_run",
    "gateway_running",
    "get_running_pid",
    "gateway_paths",
    "make_agent",
    "read_json_file",
    "read_pid_record",
    "remove_pid_file_if_owned",
    "terminate_pid",
    "wait_for_gateway_running",
    "gateway_request_counts",
    "install_service",
    "uninstall_service",
    "_gateway_heartbeat_loop",
    "_gateway_request_loop",
    "_gateway_background_main_loop",
    "_write_gateway_heartbeat",
]
