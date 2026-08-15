
from __future__ import annotations

"""implements gateway supervisor and start-all CLI commands.

给人看的解释：
这个文件实现两个功能：
1. supervisor 子命令：启动/停止/查看 gateway 看门狗进程
2. start-all 子命令：一键启动 gateway + 所有通道适配器
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ..agent.gateway_parts import gateway_paths, gateway_running
from ..agent.gateway_parts.daemon_control import get_running_pid, read_pid_record
from ..agent.gateway_parts.supervisor import (
    is_supervisor_running,
    run_supervisor,
    stop_supervisor,
)
from .common import ROOT, make_agent


def _supervisor_daemon_cmd(config: str) -> list[str]:
    """supervisor 守护进程的启动命令(前台跑 gateway supervisor 循环)。
    supervisor-start 和 start-all 两处共用,防再写成不一致(曾 start-all 漏 gateway→看门狗起不来)。"""
    return [sys.executable, "-m", "agent_py_agent", "--config", config, "gateway", "supervisor"]


def _spawn_daemon(cmd: list[str], log_path: Path) -> subprocess.Popen:
    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        start_new_session = True
    with log_path.open("ab") as log_file:
        return subprocess.Popen(
            cmd, cwd=ROOT.parent, stdin=subprocess.DEVNULL,
            stdout=log_file, stderr=subprocess.STDOUT,
            creationflags=creationflags, start_new_session=start_new_session,
        )


def _wait_for_gateway_ready(paths, timeout: float = 30.0) -> tuple[int, bool] | None:
    print("等待 gateway 就绪...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        gpid, galive = gateway_running(paths)
        if gpid and galive:
            return gpid, galive
        time.sleep(1.0)
    return None


def _pid_file_matches(pid_path: Path, expected_pid: int) -> bool | None:
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        return pid and pid == expected_pid
    except (OSError, ValueError):
        return None


def _wait_for_supervisor_start(supervisor_pid_path: Path, process: subprocess.Popen, deadline: float) -> bool:
    while time.time() < deadline:
        if _pid_file_matches(supervisor_pid_path, process.pid) is True:
            return True
        time.sleep(0.2)
    return False


def cmd_supervisor_start(args) -> int:
    agent = make_agent(args)
    paths = gateway_paths(agent)

    if is_supervisor_running(args.config):
        print("supervisor 已在运行")
        return 0

    supervisor_pid_path = paths.root / "supervisor.pid"
    cmd = [sys.executable, "-m", "agent_py_agent", "--config", args.config, "gateway", "supervisor"]
    process = _spawn_daemon(cmd, paths.log)

    deadline = time.time() + 10.0
    if _wait_for_supervisor_start(supervisor_pid_path, process, deadline):
        print(f"supervisor starting pid={process.pid}")
    else:
        print(f"supervisor starting (pid file not yet confirmed) pid={process.pid}")
    print(f"workspace: {paths.root}")
    return 0


def cmd_supervisor_run(args) -> int:
    try:
        return run_supervisor(
            args.config,
            workspace_root=getattr(args, "workspace_root", None),
            heartbeat_timeout=getattr(args, "heartbeat_timeout", 120.0),
            check_interval=getattr(args, "check_interval", 10.0),
            max_restart_attempts=getattr(args, "max_restart_attempts", 5),
            restart_cooldown=getattr(args, "restart_cooldown", 30.0),
        )
    except Exception as exc:
        print(f"supervisor error: {exc}", file=sys.stderr)
        return 2


def cmd_supervisor_status(args) -> int:
    agent = make_agent(args)
    paths = gateway_paths(agent)
    supervisor_pid_path = paths.root / "supervisor.pid"

    if not supervisor_pid_path.exists():
        print("supervisor: 未运行")
    else:
        try:
            pid = int(supervisor_pid_path.read_text(encoding="utf-8").strip())
            import os as os_module
            os_module.kill(pid, 0)
            print(f"supervisor: 运行中 pid={pid}")
        except (OSError, ValueError):
            print("supervisor: 疑似已崩溃（PID 文件残留）")

    # Show gateway status
    gpid, galive = gateway_running(paths)
    if gpid:
        record = read_pid_record(paths.pid)
        print(f"gateway: 运行中 pid={gpid} alive={galive}")
        if record:
            print(f"  start_time: {record.get('start_time', 'unknown')}")
    else:
        print("gateway: 未运行")

    print(f"workspace: {paths.root}")
    return 0


def cmd_supervisor_stop(args) -> int:
    agent = make_agent(args)
    paths = gateway_paths(agent)

    if stop_supervisor(args.config, timeout=args.timeout or 10.0):
        print("supervisor 已停止")
        return 0
    else:
        print("supervisor 停止超时", file=sys.stderr)
        return 2


def cmd_start_all(args) -> int:
    agent = make_agent(args)
    paths = gateway_paths(agent)
    paths.root.mkdir(parents=True, exist_ok=True)
    adapter_channel = getattr(args, "adapter_channel", getattr(args, "adapter", "all"))

    if is_supervisor_running(args.config):
        print("supervisor 已在运行")
    else:
        cmd = _supervisor_daemon_cmd(args.config)
        _spawn_daemon(cmd, paths.log)
        print("supervisor 启动中...")

    result = _wait_for_gateway_ready(paths)
    if result is None:
        print("gateway 启动超时，请检查日志", file=sys.stderr)
        return 2
    gpid, _ = result
    print(f"gateway 就绪: pid={gpid}")

    if adapter_channel not in (None, 'none'):
        print(f"启动通道适配器: {adapter_channel}")
        adapter_cmd = [sys.executable, "-m", "agent_py_agent", "--config", args.config,
                       "adapter", "start", "--channel", adapter_channel, "--daemon"]
        _spawn_daemon(adapter_cmd, paths.log)
        print("适配器启动中...")

    print("start-all 完成。运行 `my-agent gateway status` 查看状态。")
    return 0
