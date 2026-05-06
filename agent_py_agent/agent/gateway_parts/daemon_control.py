from __future__ import annotations

"""LLM: daemon control - fork to background, PID file management, graceful shutdown, scoped locks.

缁欎汉鐪嬬殑瑙ｉ噴锛?
杩欎釜鏂囦欢澶勭悊 daemon 灞傞潰鐨勬帶鍒讹細鎬庝箞 fork 鍒板悗鍙般€佹€庝箞妫€娴嬮噸澶嶅惎鍔ㄣ€佹€庝箞浼橀泤鍏抽棴銆?
杩樺寘鍚?长期助手 椋庢牸鐨勫姛鑳斤細start time tracking 妫€娴?PID 閲嶇敤銆乻coped locks 闃叉澶氬疄渚嬪啿绐併€?
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


# 鈹€鈹€ PID file management 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def read_pid_file(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        content = pid_path.read_text(encoding="utf-8").strip()
    except (ValueError, OSError):
        return None
    return _pid_from_file_content(pid_path, content)


def _pid_from_file_content(pid_path: Path, content: str) -> int | None:
    if not content:
        return None
    if content.startswith("{"):
        return _pid_from_record(_read_json_file(pid_path))
    try:
        return int(content)
    except ValueError:
        return None


def _pid_from_record(record: dict | None) -> int | None:
    if not record or "pid" not in record:
        return None
    try:
        return int(record["pid"])
    except (TypeError, ValueError):
        return None


def remove_pid_file(pid_path: Path) -> None:
    try:
        pid_path.unlink()
    except OSError:
        pass


def check_already_running(pid_path: Path) -> tuple[bool, int | None]:
    existing_pid = read_pid_file(pid_path)
    if existing_pid is None:
        return False, None
    if is_pid_alive(existing_pid):
        return True, existing_pid
    # Stale PID file - process is dead
    return False, None


# 鈹€鈹€ PID record with start time (长期助手 pattern) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def write_pid_record(pid_path: Path) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_file(pid_path, _build_pid_record())


def read_pid_record(pid_path: Path) -> dict | None:
    return _read_json_file(pid_path)


def get_running_pid(pid_path: Path, *, cleanup_stale: bool = True) -> int | None:
    record = read_pid_record(pid_path)
    if not record:
        _cleanup_stale_pid_file(pid_path, cleanup_stale)
        return None

    pid = _pid_from_record(record)
    if pid is None:
        _cleanup_stale_pid_file(pid_path, cleanup_stale)
        return None

    # LLM: use the shared process-control helper so Windows avoids os.kill(pid, 0)
    # while macOS/Linux keep the POSIX liveness path.
    if not is_pid_alive(pid):
        _cleanup_stale_pid_file(pid_path, cleanup_stale)
        return None

    # Check start_time to detect PID reuse
    recorded_start = record.get("start_time")
    current_start = _get_process_start_time(pid)
    if recorded_start is not None and current_start is not None and current_start != recorded_start:
        # PID was reused by another process
        _cleanup_stale_pid_file(pid_path, cleanup_stale)
        return None

    return pid


def _cleanup_stale_pid_file(pid_path: Path, cleanup_stale: bool) -> None:
    if not cleanup_stale:
        return
    try:
        pid_path.unlink()
    except OSError:
        pass


def remove_pid_file_if_owned(pid_path: Path) -> None:
    try:
        record = _read_json_file(pid_path)
        file_pid = _pid_from_record(record)
        if file_pid is not None and file_pid != os.getpid():
            return
        pid_path.unlink(missing_ok=True)
    except Exception:
        pass


# 鈹€鈹€ Scoped locks and runtime status are re-exported from focused modules. 鈹€鈹€鈹€


# 鈹€鈹€ Legacy API compatibility 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def write_pid_file(pid_path: Path, pid: int) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid), encoding="utf-8")


def daemonize(pid_path: Path) -> bool:
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
    return wait_for_pid_exit(pid, timeout)


def install_signal_handler(handler) -> None:
    if os.name == "nt":
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
    else:
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
