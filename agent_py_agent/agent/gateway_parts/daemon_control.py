from __future__ import annotations

"""LLM: daemon control - fork to background, PID file management, graceful shutdown, scoped locks.

给人看的解释：
这个文件处理 daemon 层面的控制：怎么 fork 到后台、怎么检测重复启动、怎么优雅关闭。
还包含 长期助手 风格的功能：start time tracking 检测 PID 重用、scoped locks 防止多实例冲突。
"""

import json
import os
import signal
import time
from pathlib import Path
from typing import Optional

from .daemon_metadata import (
    _build_pid_record,
    _get_process_start_time,
    _read_json_file,
    _scope_hash,
    _utc_now_iso,
    _write_json_file,
)

# Re-export from process_control for convenience
from .process_control import is_pid_alive, terminate_pid, wait_for_pid_exit
from .runtime_status import WriteRuntimeStatusParams, read_runtime_status, write_runtime_status
from .scoped_locks import (
    _get_lock_dir,
    _get_scope_lock_path,
    _release_lock_if_stale,
    acquire_scoped_lock,
    release_all_scoped_locks,
    release_scoped_lock,
)

# Exit code to signal service manager should restart (长期助手: EX_TEMPFAIL = 75)
GATEWAY_SERVICE_RESTART_EXIT_CODE = 75


# ── PID file management ──────────────────────────────────────────────────────


def read_pid_file(pid_path: Path) -> int | None:
    """Read PID from a pid file path.

    Supports both plain-text (just the number) and JSON record formats.
    Returns None if file doesn't exist or is empty.
    """
    if not pid_path.exists():
        return None
    try:
        content = pid_path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        # Try JSON format first (new 长期助手 record)
        if content.startswith("{"):
            record = _read_json_file(pid_path)
            if record and "pid" in record:
                return int(record["pid"])
            return None
        # Plain text format (legacy)
        return int(content)
    except (ValueError, OSError):
        return None


def remove_pid_file(pid_path: Path) -> None:
    """Remove PID file if it exists."""
    try:
        pid_path.unlink()
    except OSError:
        pass


def check_already_running(pid_path: Path) -> tuple[bool, int | None]:
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


# ── PID record with start time (长期助手 pattern) ─────────────────────────────


def write_pid_record(pid_path: Path) -> None:
    """Write current process PID and metadata to the PID file (长期助手)."""
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_file(pid_path, _build_pid_record())


def read_pid_record(pid_path: Path) -> dict | None:
    """Read PID record from file, returning None if missing or invalid."""
    return _read_json_file(pid_path)


def get_running_pid(pid_path: Path, *, cleanup_stale: bool = True) -> int | None:
    """Return the PID of a running gateway instance, or None.

    Checks the PID file and verifies the process is actually alive.
    Uses start_time to detect PID reuse (长期助手 pattern).
    Cleans up stale PID files automatically.
    """
    record = read_pid_record(pid_path)
    if not record:
        if cleanup_stale:
            try:
                pid_path.unlink()
            except OSError:
                pass
        return None

    try:
        pid = int(record["pid"])
    except (KeyError, TypeError, ValueError):
        if cleanup_stale:
            try:
                pid_path.unlink()
            except OSError:
                pass
        return None

    # LLM: use the shared process-control helper so Windows avoids os.kill(pid, 0)
    # while macOS/Linux keep the POSIX liveness path.
    if not is_pid_alive(pid):
        if cleanup_stale:
            try:
                pid_path.unlink()
            except OSError:
                pass
        return None

    # Check start_time to detect PID reuse
    recorded_start = record.get("start_time")
    current_start = _get_process_start_time(pid)
    if recorded_start is not None and current_start is not None and current_start != recorded_start:
        # PID was reused by another process
        if cleanup_stale:
            try:
                pid_path.unlink()
            except OSError:
                pass
        return None

    return pid


def remove_pid_file_if_owned(pid_path: Path) -> None:
    """Remove the PID file, but only if it belongs to this process (长期助手 pattern).

    During --replace handoffs, the old process's atexit handler can fire AFTER
    the new process has written its own PID file. Blindly removing the file
    would delete the new process's record.
    """
    try:
        record = _read_json_file(pid_path)
        if record is not None:
            try:
                file_pid = int(record["pid"])
            except (KeyError, TypeError, ValueError):
                file_pid = None
            if file_pid is not None and file_pid != os.getpid():
                # PID file belongs to a different process — leave it alone
                return
        pid_path.unlink(missing_ok=True)
    except Exception:
        pass


# ── Scoped locks and runtime status are re-exported from focused modules. ───


# ── Legacy API compatibility ────────────────────────────────────────────────


def write_pid_file(pid_path: Path, pid: int) -> None:
    """Write PID to a pid file as plain text (legacy API).

    For 长期助手 PID records with metadata, use write_pid_record() instead.
    """
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid), encoding="utf-8")


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


def request_graceful_shutdown(
    pid_path: Path, stop_request_path: Path, reason: str = "user request"
) -> bool:
    """Request graceful shutdown by writing stop request file.

    Returns True if stop was requested, False if gateway not running.
    """
    existing_pid = read_pid_file(pid_path)
    if not existing_pid or not is_pid_alive(existing_pid):
        return False

    # Write stop request
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
