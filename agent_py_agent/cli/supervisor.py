from __future__ import annotations

"""LLM: implements gateway supervisor and start-all CLI commands.

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

from ..agent.gateway_parts.supervisor import (
    is_supervisor_running,
    run_supervisor,
    stop_supervisor,
)
from .common import make_agent, ROOT
from ..agent.gateway import gateway_paths, gateway_running
from ..agent.gateway_parts.daemon_control import get_running_pid, read_pid_record


def cmd_supervisor_start(args) -> int:
    """启动 gateway 看门狗进程（supervisor）。"""
    agent = make_agent(args)
    paths = gateway_paths(agent)

    if is_supervisor_running(args.config):
        print(f"supervisor 已在运行")
        return 0

    supervisor_pid_path = paths.root / "supervisor.pid"

    cmd = [
        sys.executable,
        "-m",
        "agent_py_agent",
        "--config",
        args.config,
        "gateway",
        "supervisor",
    ]

    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        start_new_session = True

    config_path = Path(args.config).resolve()
    with paths.log.open("ab") as log_file:
        process = subprocess.Popen(
            cmd,
            cwd=ROOT.parent,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )

    # Wait for supervisor to start
    deadline = time.time() + 10.0
    started = False
    while time.time() < deadline:
        if supervisor_pid_path.exists():
            try:
                pid = int(supervisor_pid_path.read_text(encoding="utf-8").strip())
                if pid and pid == process.pid:
                    started = True
                    break
            except (OSError, ValueError):
                pass
        time.sleep(0.2)

    print(f"supervisor starting pid={process.pid}")
    print(f"workspace: {paths.root}")
    return 0


def cmd_supervisor_run(args) -> int:
    """内部命令：前台运行 supervisor。"""
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
    """显示 supervisor 状态。"""
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
    """停止 supervisor。"""
    agent = make_agent(args)
    paths = gateway_paths(agent)

    if stop_supervisor(args.config, timeout=args.timeout or 10.0):
        print("supervisor 已停止")
        return 0
    else:
        print("supervisor 停止超时", file=sys.stderr)
        return 2


def cmd_start_all(args) -> int:
    """一键启动 gateway + 所有通道适配器（supervisor 模式）。"""
    agent = make_agent(args)
    paths = gateway_paths(agent)
    paths.root.mkdir(parents=True, exist_ok=True)

    # Determine adapter channel (default: all)
    adapter_channel = getattr(args, 'adapter_channel', 'all')

    # Start supervisor (which starts and monitors gateway)
    if is_supervisor_running(args.config):
        print("supervisor 已在运行")
    else:
        cmd = [
            sys.executable,
            "-m",
            "agent_py_agent",
            "--config",
            args.config,
            "supervisor",
            "run",
        ]
        creationflags = 0
        start_new_session = False
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            start_new_session = True

        config_path = Path(args.config).resolve()
        with paths.log.open("ab") as log_file:
            subprocess.Popen(
                cmd,
                cwd=ROOT.parent,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
        print("supervisor 启动中...")

    # Wait for gateway to be ready
    print("等待 gateway 就绪...")
    deadline = time.time() + 30.0
    while time.time() < deadline:
        gpid, galive = gateway_running(paths)
        if gpid and galive:
            break
        time.sleep(1.0)
    else:
        print("gateway 启动超时，请检查日志", file=sys.stderr)
        return 2

    print(f"gateway 就绪: pid={gpid}")

    # Start adapters if requested
    if adapter_channel not in (None, 'none'):
        print(f"启动通道适配器: {adapter_channel}")
        adapter_cmd = [
            sys.executable,
            "-m",
            "agent_py_agent",
            "--config",
            args.config,
            "adapter",
            "start",
            "--channel",
            adapter_channel,
            "--daemon",
        ]
        with paths.log.open("ab") as log_file:
            subprocess.Popen(
                adapter_cmd,
                cwd=ROOT.parent,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
        print(f"适配器启动中...")

    print("start-all 完成。运行 `my-agent gateway status` 查看状态。")
    return 0
