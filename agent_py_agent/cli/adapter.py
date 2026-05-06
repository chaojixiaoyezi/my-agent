from __future__ import annotations

"""LLM: CLI entrypoints for file and channel adapters.

Small helper functions keep daemon, foreground, and file-loop flows below soft limits.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..agent.adapter import ChannelManager, FeishuAdapter, QQAdapter
from ..agent.gateway import (
    AdapterPaths,
    adapter_paths,
    gateway_paths,
    gateway_running,
    process_file_adapter_once,
)
from ..agent.gateway_parts.daemon_control import (
    get_running_pid,
    is_pid_alive,
    remove_pid_file_if_owned,
    write_pid_record,
)
from ..agent.gateway_parts.process_control import terminate_pid, wait_for_pid_exit
from .common import make_agent
from .gateway_client import ensure_gateway_started


def cmd_adapter(args) -> int:
    """Top-level adapter command placeholder."""
    print("please specify adapter subcommand: file", file=sys.stderr)
    return 2


def cmd_adapter_file(args) -> int:
    """Run the file protocol adapter: inbox JSON -> gateway -> outbox JSON."""
    agent = make_agent(args)
    gpaths = gateway_paths(agent)
    apaths = _resolve_adapter_paths(agent, args)
    gateway_code = _ensure_gateway_available(args, gpaths)
    if gateway_code:
        return gateway_code

    timeout = args.timeout if args.timeout is not None else agent.config.gateway_request_timeout
    total = _process_file_adapter_loop(agent, args, gpaths, apaths, timeout)
    _print_file_adapter_summary(total, apaths, gpaths)
    return 0


def _resolve_adapter_paths(agent, args) -> AdapterPaths:
    apaths = adapter_paths(agent)
    if args.root:
        root = Path(args.root).expanduser()
        apaths = AdapterPaths(
            root=root,
            inbox=root / "inbox",
            processing=root / "processing",
            done=root / "done",
            failed=root / "failed",
            outbox=root / "outbox",
        )
    if args.inbox:
        apaths.inbox = Path(args.inbox).expanduser()
    if args.outbox:
        apaths.outbox = Path(args.outbox).expanduser()
    return apaths


def _ensure_gateway_available(args, gpaths) -> int:
    if not args.no_start_gateway:
        return ensure_gateway_started(args)
    _, alive = gateway_running(gpaths)
    if alive:
        return 0
    print("gateway is not running and --no-start-gateway was specified", file=sys.stderr)
    return 2


def _process_file_adapter_loop(agent, args, gpaths, apaths: AdapterPaths, timeout) -> int:
    total = 0
    while True:
        total += process_file_adapter_once(
            agent,
            gateway_paths_obj=gpaths,
            adapter_paths_obj=apaths,
            timeout=timeout,
            limit=args.limit,
        )
        if args.once or not args.watch:
            return total
        time.sleep(max(0.2, args.poll_interval))


def _print_file_adapter_summary(total: int, apaths: AdapterPaths, gpaths) -> None:
    print(
        json.dumps(
            {
                "processed": total,
                "adapter_root": str(apaths.root),
                "inbox": str(apaths.inbox),
                "outbox": str(apaths.outbox),
                "gateway_workspace": str(gpaths.root),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def cmd_adapter_start(args) -> int:
    """Start channel adapters in foreground or daemon mode."""
    agent = make_agent(args)
    gpaths = gateway_paths(agent)
    gpaths.root.mkdir(parents=True, exist_ok=True)
    pid_file = Path(args.pid_file).expanduser() if args.pid_file else gpaths.adapter_pid
    if args.daemon:
        return _daemonize_adapter(agent, gpaths, pid_file)
    return _run_adapter_foreground(agent, args, gpaths)


def _daemonize_adapter(agent, gpaths, pid_file: Path) -> int:
    existing_pid = get_running_pid(pid_file)
    if existing_pid is not None:
        print(f"adapter already running (PID {existing_pid}) or PID file exists", file=sys.stderr)
        print(f"use stop first, or delete {pid_file} before retrying", file=sys.stderr)
        return 1

    process = _start_adapter_daemon_process(agent, gpaths)
    return _wait_for_adapter_pid(process, pid_file)


def _start_adapter_daemon_process(agent, gpaths) -> subprocess.Popen:
    cmd = [
        sys.executable,
        "-m",
        "agent_py_agent",
        "--config",
        str(agent.config.config_path),
        "adapter",
        "start",
        "--channel",
        getattr(agent.config, "adapter_channel", "all"),
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


def _register_channel_adapter(manager: ChannelManager, channel: str, agent) -> None:
    workspace_root = _adapter_workspace_root(agent)
    if channel == "feishu":
        adapter = FeishuAdapter(
            config=_feishu_adapter_config(agent),
            callback_port=agent.config.feishu_callback_port or 8421,
            workspace_root=workspace_root,
        )
    elif channel == "qq":
        adapter = QQAdapter(
            config=_qq_adapter_config(agent),
            workspace_root=workspace_root,
        )
    else:
        return
    adapter.on_message(lambda msg: manager.route_message(msg))
    manager.register_adapter(adapter)


def _adapter_workspace_root(agent) -> Path:
    return Path(agent.config.workspace_root).resolve() if agent.config.workspace_root else Path.cwd()


def _feishu_adapter_config(agent) -> dict[str, str]:
    return {
        "feishu_app_id": agent.config.feishu_app_id or "",
        "feishu_app_secret": agent.config.feishu_app_secret or "",
        "feishu_verification_token": agent.config.feishu_verification_token or "",
        "feishu_encrypt_key": getattr(agent.config, "feishu_encrypt_key", ""),
    }


def _qq_adapter_config(agent) -> dict[str, str]:
    return {
        "qq_app_id": agent.config.qq_app_id or "",
        "qq_app_secret": agent.config.qq_app_secret or "",
    }


def _run_adapter_foreground(agent, args, gpaths) -> int:
    manager = ChannelManager(gateway_port=agent.config.gateway_port)
    _register_requested_adapters(manager, args.channel, agent)
    globals()["_adapter_manager"] = manager

    write_pid_record(gpaths.adapter_pid)
    _write_adapter_state(gpaths, "running", {"channel": args.channel})
    print(f"starting channel adapter: {args.channel}", file=sys.stderr)
    manager.start_all()
    print(f"started adapters: {manager.list_adapters()}", file=sys.stderr)

    _wait_for_adapter_shutdown()
    manager.stop_all()
    remove_pid_file_if_owned(gpaths.adapter_pid)
    _write_adapter_state(gpaths, "stopped", {"reason": "user request"})
    print("adapter stopped", file=sys.stderr)
    return 0


def _register_requested_adapters(manager: ChannelManager, channel: str, agent) -> None:
    if channel in ("feishu", "all"):
        _register_channel_adapter(manager, "feishu", agent)
    if channel in ("qq", "all"):
        _register_channel_adapter(manager, "qq", agent)


def _wait_for_adapter_shutdown() -> None:
    stop_event = threading.Event()

    def _sig_handler(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
    try:
        stop_event.wait()
    except KeyboardInterrupt:
        pass


def _write_adapter_state(gpaths, state: str, extra: dict | None = None) -> None:
    from ..agent.gateway_parts.daemon_control import _get_process_start_time, _utc_now_iso

    payload = {
        "kind": "my-agent-adapter",
        "pid": os.getpid(),
        "start_time": _get_process_start_time(os.getpid()),
        "state": state,
        "updated_at": _utc_now_iso(),
    }
    if extra:
        payload.update(extra)
    state_path = gpaths.root / "adapter_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def cmd_adapter_status(args) -> int:
    """Print adapter daemon or foreground-manager status."""
    agent = make_agent(args)
    gpaths = gateway_paths(agent)
    pid_file = Path(args.pid_file).expanduser() if args.pid_file else gpaths.adapter_pid
    pid = get_running_pid(pid_file)
    if pid is not None:
        _print_daemon_adapter_status(pid, pid_file, gpaths)
        return 0
    return _print_foreground_adapter_status()


def _print_daemon_adapter_status(pid: int, pid_file: Path, gpaths) -> None:
    print(f"adapter running: pid={pid}", file=sys.stderr)
    record = read_pid_record(pid_file)
    if record:
        print(f"  start_time: {record.get('start_time', 'unknown')}", file=sys.stderr)
    state = _read_adapter_state(gpaths)
    if state:
        print(f"  state: {state.get('state', 'unknown')}", file=sys.stderr)


def _read_adapter_state(gpaths) -> dict[str, Any] | None:
    state_path = gpaths.root / "adapter_state.json"
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _print_foreground_adapter_status() -> int:
    manager: ChannelManager | None = globals().get("_adapter_manager")
    if manager is None:
        print("no running adapter manager", file=sys.stderr)
        return 1
    for name in manager.list_adapters():
        adapter = manager.get_adapter(name)
        status = "running" if adapter and adapter.running else "stopped"
        print(f"  {name}: {status}")
    return 0


def read_pid_record(path: Path):
    """Read PID record from file."""
    from ..agent.gateway_parts.daemon_control import _read_json_file

    return _read_json_file(path)


def cmd_adapter_stop(args) -> int:
    """Stop adapter daemon or foreground manager."""
    agent = make_agent(args)
    gpaths = gateway_paths(agent)
    pid_file = Path(args.pid_file).expanduser() if args.pid_file else gpaths.adapter_pid
    pid = get_running_pid(pid_file)
    if pid is not None:
        return _stop_adapter_daemon(args, gpaths, pid_file, pid)
    return _stop_foreground_adapter_manager()


def _stop_adapter_daemon(args, gpaths, pid_file: Path, pid: int) -> int:
    print(f"stopping adapter (PID {pid})...", file=sys.stderr)
    _write_stop_request(gpaths)
    timeout = getattr(args, "timeout", 10.0)
    if wait_for_pid_exit(pid, timeout=timeout):
        remove_pid_file_if_owned(pid_file)
        print("adapter stopped", file=sys.stderr)
        return 0
    terminate_pid(pid)
    if wait_for_pid_exit(pid, timeout=5.0):
        remove_pid_file_if_owned(pid_file)
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


def _stop_foreground_adapter_manager() -> int:
    manager: ChannelManager | None = globals().get("_adapter_manager")
    if manager is None:
        print("no running adapter manager", file=sys.stderr)
        return 1
    manager.stop_all()
    print("all adapters stopped", file=sys.stderr)
    return 0
