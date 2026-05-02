from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..agent.gateway import AdapterPaths, adapter_paths, gateway_paths, gateway_running, process_file_adapter_once
from ..agent.gateway_parts.daemon_control import (
    get_running_pid,
    is_pid_alive,
    write_pid_record,
    remove_pid_file_if_owned,
)
from ..agent.gateway_parts.process_control import terminate_pid, wait_for_pid_exit
from .common import make_agent
from .gateway_client import ensure_gateway_started
from ..agent.adapter import ChannelManager, FeishuAdapter, QQAdapter


def cmd_adapter(args) -> int:
    """adapter 命令族入口。"""

    print("请指定 adapter 子命令：file。", file=sys.stderr)
    return 2



def cmd_adapter_file(args) -> int:
    """文件协议 adapter：inbox JSON -> gateway -> outbox JSON。"""

    agent = make_agent(args)
    gpaths = gateway_paths(agent)
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

    if not args.no_start_gateway:
        code = ensure_gateway_started(args)
        if code:
            return code
    else:
        _, alive = gateway_running(gpaths)
        if not alive:
            print("gateway 未在运行，且指定了 --no-start-gateway。", file=sys.stderr)
            return 2

    timeout = args.timeout if args.timeout is not None else agent.config.gateway_request_timeout
    total = 0
    while True:
        processed = process_file_adapter_once(
            agent,
            gateway_paths_obj=gpaths,
            adapter_paths_obj=apaths,
            timeout=timeout,
            limit=args.limit,
        )
        total += processed
        if args.once or not args.watch:
            break
        time.sleep(max(0.2, args.poll_interval))
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
    return 0


# ---------------------------------------------------------------------------
# 通道适配器子命令
# ---------------------------------------------------------------------------


def cmd_adapter_start(args) -> int:
    """启动指定通道的适配器（feishu / qq / all）。"""
    import sys
    from pathlib import Path

    agent = make_agent(args)
    gpaths = gateway_paths(agent)

    # Ensure gateway workspace exists
    gpaths.root.mkdir(parents=True, exist_ok=True)

    # Determine PID file path
    pid_file = Path(args.pid_file).expanduser() if args.pid_file else gpaths.adapter_pid

    # Daemon mode: fork to background
    if args.daemon:
        return _daemonize_adapter(agent, gpaths, pid_file)

    # Foreground mode (original behavior)
    return _run_adapter_foreground(agent, args, gpaths)


def _daemonize_adapter(agent, gpaths, pid_file: Path) -> int:
    """Fork adapter to background and write PID file."""
    # Check if already running
    existing_pid = get_running_pid(pid_file)
    if existing_pid is not None:
        print(f"适配器已在运行 (PID {existing_pid})，或 PID 文件存在。", file=sys.stderr)
        print(f"请先使用 'stop' 命令停止现有实例，或删除 {pid_file} 后重试。", file=sys.stderr)
        return 1

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

    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        start_new_session = True

    # Start subprocess
    with gpaths.log.open("ab") as log_file:
        process = subprocess.Popen(
            cmd,
            cwd=str(agent.root),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )

    # Wait for child to write PID file
    deadline = time.time() + 30.0
    while time.time() < deadline:
        child_pid = get_running_pid(pid_file)
        if child_pid is not None:
            print(f"适配器已启动 (PID {child_pid})，PID 文件: {pid_file}", file=sys.stderr)
            return 0
        if not is_pid_alive(process.pid):
            print("适配器进程启动失败", file=sys.stderr)
            return 1
        time.sleep(0.2)

    print("适配器启动超时（未收到 PID 文件）", file=sys.stderr)
    return 1


def _run_adapter_foreground(agent, args, gpaths) -> int:
    """Run adapter in foreground mode (original behavior)."""
    manager = ChannelManager(gateway_port=agent.config.gateway_port)

    # 注册所需适配器
    if args.channel in ("feishu", "all"):
        feishu_cfg = {
            "feishu_app_id": agent.config.feishu_app_id or "",
            "feishu_app_secret": agent.config.feishu_app_secret or "",
            "feishu_verification_token": agent.config.feishu_verification_token or "",
            "feishu_encrypt_key": getattr(agent.config, "feishu_encrypt_key", ""),
        }
        feishu = FeishuAdapter(
            config=feishu_cfg,
            callback_port=agent.config.feishu_callback_port or 8421,
            workspace_root=Path(agent.config.workspace_root or ".").resolve()
            if agent.config.workspace_root
            else Path.cwd(),
        )
        feishu.on_message(lambda msg: manager.route_message(msg))
        manager.register_adapter(feishu)

    if args.channel in ("qq", "all"):
        qq_cfg = {
            "qq_app_id": agent.config.qq_app_id or "",
            "qq_app_secret": agent.config.qq_app_secret or "",
        }
        qq = QQAdapter(
            config=qq_cfg,
            workspace_root=Path(agent.config.workspace_root or ".").resolve()
            if agent.config.workspace_root
            else Path.cwd(),
        )
        qq.on_message(lambda msg: manager.route_message(msg))
        manager.register_adapter(qq)

    # 把 manager 存到全局（后续 stop/status 需要用到）
    _global_manager = manager
    globals()["_adapter_manager"] = _global_manager

    # Write adapter PID for supervisor monitoring (foreground mode too)
    write_pid_record(gpaths.adapter_pid)

    # Write initial state
    _write_adapter_state(gpaths, "running", {"channel": args.channel})

    print(f"启动通道适配器: {args.channel}", file=sys.stderr)
    manager.start_all()
    print(f"已启动: {manager.list_adapters()}", file=sys.stderr)

    # 前台保持运行，Ctrl+C 退出
    stop_event = threading.Event()

    def _sig_handler(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    try:
        stop_event.wait()
    except KeyboardInterrupt:
        pass

    manager.stop_all()
    remove_pid_file_if_owned(gpaths.adapter_pid)
    _write_adapter_state(gpaths, "stopped", {"reason": "user request"})
    print("适配器已停止。", file=sys.stderr)
    return 0


def _write_adapter_state(gpaths, state: str, extra: dict = None) -> None:
    """Write adapter runtime state to adapter_state.json."""
    from ..agent.gateway_parts.daemon_control import _utc_now_iso, _get_process_start_time

    state_path = gpaths.root / "adapter_state.json"
    payload = {
        "kind": "my-agent-adapter",
        "pid": os.getpid(),
        "start_time": _get_process_start_time(os.getpid()),
        "state": state,
        "updated_at": _utc_now_iso(),
    }
    if extra:
        payload.update(extra)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def cmd_adapter_status(args) -> int:
    """查看已注册适配器的运行状态。"""
    import sys

    agent = make_agent(args)
    gpaths = gateway_paths(agent)
    pid_file = Path(args.pid_file).expanduser() if args.pid_file else gpaths.adapter_pid

    # Check if adapter is running via PID file (daemon mode)
    pid = get_running_pid(pid_file)
    if pid is not None:
        record = read_pid_record(pid_file)
        print(f"适配器: 运行中 pid={pid}", file=sys.stderr)
        if record:
            print(f"  start_time: {record.get('start_time', 'unknown')}", file=sys.stderr)
        # Also check adapter state file
        state_path = gpaths.root / "adapter_state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                print(f"  state: {state.get('state', 'unknown')}", file=sys.stderr)
            except (OSError, json.JSONDecodeError):
                pass
        return 0

    # Fallback: check global manager (foreground mode)
    manager: ChannelManager | None = globals().get("_adapter_manager")
    if manager is None:
        print("没有正在运行的适配器管理器。", file=sys.stderr)
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
    """停止所有已启动的通道适配器（支持 daemon 模式）。"""
    import sys

    agent = make_agent(args)
    gpaths = gateway_paths(agent)
    pid_file = Path(args.pid_file).expanduser() if args.pid_file else gpaths.adapter_pid

    # Check if adapter is running via PID file (daemon mode)
    pid = get_running_pid(pid_file)
    if pid is not None:
        print(f"正在停止适配器 (PID {pid})...", file=sys.stderr)

        # Write stop request
        stop_request_path = gpaths.root / "adapter_stop.request"
        stop_request_path.parent.mkdir(parents=True, exist_ok=True)
        stop_request_path.write_text(
            json.dumps({"requested_at": time.time(), "reason": "user request"}, ensure_ascii=False),
            encoding="utf-8",
        )

        # Try graceful shutdown first
        timeout = getattr(args, 'timeout', 10.0)
        if wait_for_pid_exit(pid, timeout=timeout):
            remove_pid_file_if_owned(pid_file)
            print("适配器已停止。", file=sys.stderr)
            return 0

        # Force kill if still alive
        terminate_pid(pid)
        if wait_for_pid_exit(pid, timeout=5.0):
            remove_pid_file_if_owned(pid_file)
            print("适配器已强制停止。", file=sys.stderr)
            return 0

        print("适配器停止失败。", file=sys.stderr)
        return 1

    # Fallback: check global manager (foreground mode)
    manager: ChannelManager | None = globals().get("_adapter_manager")
    if manager is None:
        print("没有正在运行的适配器管理器。", file=sys.stderr)
        return 1
    manager.stop_all()
    print("所有适配器已停止。", file=sys.stderr)
    return 0
