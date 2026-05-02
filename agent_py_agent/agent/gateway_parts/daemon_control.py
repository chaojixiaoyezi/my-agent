from __future__ import annotations

"""LLM: daemon control - fork to background, PID file management, graceful shutdown.

给人看的解释：
这个文件处理 daemon 层面的控制：怎么 fork 到后台、怎么检测重复启动、怎么优雅关闭。
"""

import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

# Re-export from process_control for convenience
from .process_control import is_pid_alive, terminate_pid, wait_for_pid_exit


def read_pid_file(pid_path: Path) -> Optional[int]:
    """Read PID from a pid file path.

    Returns None if file doesn't exist or is empty.
    """
    if not pid_path.exists():
        return None
    try:
        content = pid_path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        return int(content)
    except (ValueError, OSError):
        return None


def write_pid_file(pid_path: Path, pid: int) -> None:
    """Write PID to a pid file."""
    pid_path.write_text(str(pid), encoding="utf-8")


def remove_pid_file(pid_path: Path) -> None:
    """Remove PID file if it exists."""
    try:
        pid_path.unlink()
    except OSError:
        pass


def check_already_running(pid_path: Path) -> tuple[bool, Optional[int]]:
    """Check if another instance is already running.

    Returns (is_running, existing_pid).
    """
    existing_pid = read_pid_file(pid_path)
    if existing_pid is None:
        return False, None
    if is_pid_alive(existing_pid):
        return True, existing_pid
    # Stale PID file - process is dead
    return False, None


def daemonize(pid_path: Path) -> bool:
    """Fork current process to daemonize.

    On Unix: fork, setsid, fork again, redirect stdio to /dev/null.
    On Windows: not supported, returns False.

    Returns True if we are the parent process (should exit),
           False if we are the daemon child.
    """
    if os.name == "nt":
        return False

    pid = os.fork()
    if pid > 0:
        # Parent - wait for child to confirm startup, then exit
        time.sleep(0.5)
        # Check if child wrote its PID
        child_pid = read_pid_file(pid_path)
        if child_pid and is_pid_alive(child_pid):
            return True  # Parent should exit
        return True

    # First child
    os.setsid()
    pid = os.fork()
    if pid > 0:
        os._exit(0)

    # Second child - daemon
    # Redirect stdin/stdout/stderr to /dev/null
    devnull = os.open("/dev/null", os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)

    return False  # Continue as daemon


def request_graceful_shutdown(pid_path: Path, stop_request_path: Path, reason: str = "user request") -> bool:
    """Request graceful shutdown by writing stop request file.

    Returns True if stop was requested, False if gateway not running.
    """
    existing_pid = read_pid_file(pid_path)
    if not existing_pid or not is_pid_alive(existing_pid):
        return False

    # Write stop request
    import json
    stop_request_path.parent.mkdir(parents=True, exist_ok=True)
    stop_request_path.write_text(
        json.dumps({"requested_at": time.time(), "reason": reason}, ensure_ascii=False),
        encoding="utf-8",
    )
    return True


def wait_for_shutdown(pid: int, timeout: float) -> bool:
    """Wait for a process to shut down gracefully.

    Returns True if process exited within timeout.
    """
    return wait_for_pid_exit(pid, timeout)


def install_signal_handler(handler) -> None:
    """Install signal handler for graceful shutdown."""
    if os.name == "nt":
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
    else:
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
