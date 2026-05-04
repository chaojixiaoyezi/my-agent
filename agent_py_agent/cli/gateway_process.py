from __future__ import annotations

"""LLM: implements gateway process lifecycle, foreground run loop, status, stop/restart/logs, and request workers.

给人看的解释：
这个文件只管 gateway 进程怎么启动、怎么停止、后台 worker 怎么跑、heartbeat 怎么写。
用户侧投递请求和 adapter 转换拆在别的文件里。
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..agent.capability_config import load_capability_config
from ..agent.core import SimpleAgent
from ..agent.gateway import (
    GatewayPaths,
    _process_gateway_requests,
    gateway_paths,
    gateway_request_counts,
    gateway_running,
    gateway_stale_processing,
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
    write_runtime_status,
)
from ..agent.gateway_parts.http_service import GatewayHTTPServer, start_http_server
from .common import ROOT, make_agent, make_capability_router
from .daemon import _resolve_daemon_options
from .gateway_service import (
    install_service,
    uninstall_service,
)
from .models import DaemonOptions


def cmd_gateway_install(args) -> int:
    """Install the gateway as a system service (systemd or launchd)."""
    success = install_service(force=args.force)
    return 0 if success else 1


def cmd_gateway_uninstall(args) -> int:
    """Uninstall the gateway system service (systemd or launchd)."""
    success = uninstall_service()
    return 0 if success else 1


def cmd_gateway_start(args) -> int:
    """启动第一版后台 gateway。"""

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

    # Note: write_pid_record() writes os.getpid() which is the parent
    # cmd_gateway_start process, NOT the actual daemon. Write process.pid
    # and the child's start_time.
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


def _resolve_gateway_options(agent, args):
    """Resolve daemon options for gateway run."""
    return _resolve_daemon_options(agent, args)


def _write_gateway_state(paths, data: dict) -> None:
    """Write gateway state file."""
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
    write_json_file(
        paths.state,
        {
            "status": "running",
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
        },
    )


def _log_gateway_running(agent, options, pid, requeued, failed, http_port) -> None:
    """Log gateway_run_running event."""
    log_gateway_event(
        agent,
        "gateway_run_running",
        {
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
        },
    )


def cmd_gateway_run(args) -> int:
    """内部命令：前台运行 gateway 后台循环。"""

    agent = make_agent(args)
    paths = gateway_paths(agent)
    paths.root.mkdir(parents=True, exist_ok=True)
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

    # Start HTTP server if configured
    http_server: GatewayHTTPServer | None = None
    http_port = getattr(agent.config, "gateway_port", 0) or 0
    if http_port > 0:
        http_server = start_http_server(http_port, paths)
        _update_gateway_state_running(paths, agent, options, pid, requeued, recovery["failed"], http_port)
        _log_gateway_running(agent, options, pid, requeued, recovery["failed"], http_port)

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
    return exit_code


def cmd_gateway_status(args) -> int:
    """显示 gateway 状态。"""

    agent = make_agent(args)
    paths = gateway_paths(agent)
    pid, alive = gateway_running(paths)
    state = read_json_file(paths.state)
    heartbeat = read_json_file(paths.heartbeat)
    runtime = read_runtime_status(paths.state)
    heartbeat_at = float(heartbeat.get("updated_at", 0) or 0)
    age = time.time() - heartbeat_at if heartbeat_at else 0
    stale = bool(heartbeat_at and age > agent.config.gateway_stale_seconds)
    status = "running" if alive else state.get("status", "stopped")
    if alive and stale:
        status = "stale"

    pid_record = read_pid_record(paths.pid)

    print("MY-AGENT GATEWAY")
    print(f"status={status} pid={pid if pid else '-'} alive={alive}")
    if heartbeat_at:
        print(f"heartbeat_age_seconds={age:.1f}")
    if pid_record:
        print(f"gateway_start_time={pid_record.get('start_time', 'unknown')}")
    if runtime:
        print(f"gateway_state={runtime.get('gateway_state', 'unknown')}")
    if state:
        print("state=" + json.dumps(state, ensure_ascii=False, sort_keys=True))
    counts = gateway_request_counts(paths)
    print(
        "requests="
        + json.dumps(counts, ensure_ascii=False, sort_keys=True)
    )
    stale_processing = gateway_stale_processing(paths, agent.config.gateway_processing_timeout_seconds)
    if stale_processing:
        print("stale_processing=" + json.dumps(stale_processing, ensure_ascii=False, sort_keys=True))
    print(f"workspace: {paths.root}")
    print(f"log: {paths.log}")
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

    paths.root.mkdir(parents=True, exist_ok=True)
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
        force_lock=args.force_lock,
    )
    return cmd_gateway_start(start_args)


def cmd_gateway_logs(args) -> int:
    """输出 gateway 日志尾部。"""

    agent = make_agent(args)
    paths = gateway_paths(agent)
    lines = tail_lines(paths.log, args.lines)
    if not lines:
        print(f"暂无 gateway 日志: {paths.log}")
        return 0
    for line in lines:
        print(line)
    return 0

def _gateway_request_loop(args, paths: GatewayPaths, stop_event: threading.Event) -> None:
    """后台处理 gateway inbox 请求。

    这个线程会按配置启动一个很保守的 worker pool。每个 worker 都有自己的
    `SimpleAgent` 实例，避免并发请求共享 backend / LocalStore 连接。
    """

    try:
        bootstrap_agent = make_agent(args)
        worker_count = max(1, int(bootstrap_agent.config.gateway_request_workers or 1))
    except Exception as exc:
        print(f"gateway request worker failed to initialize: {exc}", file=sys.stderr)
        return
    workers: list[threading.Thread] = []
    for index in range(worker_count):
        thread = threading.Thread(
            target=_gateway_request_worker_loop,
            args=(args, paths, stop_event, index),
            daemon=True,
        )
        thread.start()
        workers.append(thread)
    while not stop_event.is_set():
        stop_event.wait(0.5)
    for thread in workers:
        thread.join(timeout=2)


def _gateway_request_worker_loop(args, paths: GatewayPaths, stop_event: threading.Event, worker_index: int) -> None:
    """单个 gateway request worker。"""

    try:
        agent = make_agent(args)
    except Exception as exc:
        print(f"gateway request worker {worker_index} failed to initialize: {exc}", file=sys.stderr)
        return

    poll_interval = max(1, int(agent.config.gateway_request_poll_interval))
    while not stop_event.is_set():
        try:
            if worker_index == 0:
                recover_gateway_processing_requests(
                    paths,
                    startup=False,
                    max_attempts=agent.config.gateway_request_max_attempts,
                    timeout_seconds=agent.config.gateway_processing_timeout_seconds,
                    agent=agent,
                )
            processed = _process_gateway_requests(agent, paths, worker_id=f"gw-worker-{worker_index}")
        except Exception as exc:
            print(f"gateway request worker {worker_index} failed: {exc}", file=sys.stderr)
            processed = 0
        if processed:
            continue
        stop_event.wait(poll_interval)



def _gateway_heartbeat_loop(paths: GatewayPaths, agent: SimpleAgent, options: DaemonOptions, stop_event: threading.Event) -> None:
    """定期写 gateway heartbeat。"""

    while not stop_event.is_set():
        _write_gateway_heartbeat(paths, agent, options, status="running", pid=os.getpid())
        stop_event.wait(max(1, agent.config.gateway_heartbeat_interval))


def _write_gateway_heartbeat(
    paths: GatewayPaths,
    agent: SimpleAgent,
    options: DaemonOptions,
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
