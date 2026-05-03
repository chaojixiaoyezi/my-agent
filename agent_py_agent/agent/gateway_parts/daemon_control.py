from __future__ import annotations

"""LLM: daemon control - fork to background, PID file management, graceful shutdown, scoped locks.

给人看的解释：
这个文件处理 daemon 层面的控制：怎么 fork 到后台、怎么检测重复启动、怎么优雅关闭。
还包含 长期助手 风格的功能：start time tracking 检测 PID 重用、scoped locks 防止多实例冲突。
"""

import hashlib
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Re-export from process_control for convenience
from .process_control import is_pid_alive, terminate_pid, wait_for_pid_exit

# Exit code to signal service manager should restart (长期助手: EX_TEMPFAIL = 75)
GATEWAY_SERVICE_RESTART_EXIT_CODE = 75


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_process_start_time(pid: int) -> int | None:
    """Return the kernel start time for a process when available (Linux only)."""
    if sys.platform == "win32":
        return None
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        # Field 22 in /proc/<pid>/stat is process start time (clock ticks)
        return int(stat_path.read_text().split()[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
        return None


def _scope_hash(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


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

def _build_pid_record() -> dict:
    """Build a PID record with metadata for start-time tracking."""
    return {
        "pid": os.getpid(),
        "kind": "my-agent-gateway",
        "argv": list(sys.argv),
        "start_time": _get_process_start_time(os.getpid()),
        "updated_at": _utc_now_iso(),
    }


def _read_json_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _write_json_file(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


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

    # Check process existence
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
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


# ── Scoped locks (长期助手 pattern) ───────────────────────────────────────────

def _get_lock_dir() -> Path:
    """Return the machine-local directory for scoped gateway locks."""
    state_home = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "my-agent" / "locks"


def _get_scope_lock_path(scope: str, identity: str) -> Path:
    return _get_lock_dir() / f"{scope}-{_scope_hash(identity)}.lock"


def acquire_scoped_lock(scope: str, identity: str, metadata: dict[str, Any] | None = None) -> tuple[bool, dict | None]:
    """Acquire a machine-local lock keyed by scope + identity.

    Used to prevent multiple gateways from using the same external identity
    at once (e.g. the same QQ bot token or Feishu app across different runs).

    Returns (acquired, existing) where:
    - acquired=True, existing=None: lock acquired successfully
    - acquired=False, existing=<record>: lock held by another process
    """
    lock_path = _get_scope_lock_path(scope, identity)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        **_build_pid_record(),
        "scope": scope,
        "identity_hash": _scope_hash(identity),
        "metadata": metadata or {},
        "updated_at": _utc_now_iso(),
    }

    existing = _read_json_file(lock_path)
    if existing:
        try:
            existing_pid = int(existing["pid"])
        except (KeyError, TypeError, ValueError):
            existing_pid = None

        # Check if we already own this lock (same PID + start_time)
        if existing_pid == os.getpid() and existing.get("start_time") == record.get("start_time"):
            _write_json_file(lock_path, record)
            return True, existing

        # Check if the existing process is still alive
        stale = existing_pid is None
        if not stale:
            try:
                os.kill(existing_pid, 0)
            except (ProcessLookupError, PermissionError):
                stale = True
            else:
                # Check start_time for PID reuse
                current_start = _get_process_start_time(existing_pid)
                if (
                    existing.get("start_time") is not None
                    and current_start is not None
                    and current_start != existing.get("start_time")
                ):
                    stale = True
        if stale:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            return False, existing

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False, _read_json_file(lock_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
    except Exception:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return True, None


def release_scoped_lock(scope: str, identity: str) -> None:
    """Release a previously-acquired scope lock when owned by this process."""
    lock_path = _get_scope_lock_path(scope, identity)
    existing = _read_json_file(lock_path)
    if not existing:
        return
    if existing.get("pid") != os.getpid():
        return
    if existing.get("start_time") != _get_process_start_time(os.getpid()):
        return
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def release_all_scoped_locks() -> int:
    """Remove all scoped lock files in the lock directory.

    Called during --replace to clean up stale locks left by stopped/killed
    gateway processes. Returns the number of lock files removed.
    """
    lock_dir = _get_lock_dir()
    removed = 0
    if lock_dir.exists():
        for lock_file in lock_dir.glob("*.lock"):
            try:
                # Only remove locks owned by dead processes
                record = _read_json_file(lock_file)
                if record:
                    try:
                        pid = int(record["pid"])
                        os.kill(pid, 0)
                    except (ProcessLookupError, PermissionError, ValueError):
                        # Process is dead, safe to remove
                        lock_file.unlink()
                        removed += 1
                        continue
                    # Process alive but different start_time (PID reused)
                    current_start = _get_process_start_time(pid)
                    if current_start != record.get("start_time"):
                        lock_file.unlink()
                        removed += 1
            except Exception:
                pass
    return removed


# ── Runtime status (长期助手 pattern) ────────────────────────────────────────

def write_runtime_status(
    status_path: Path,
    *,
    gateway_state: Any = None,
    exit_reason: Any = None,
    restart_requested: bool = False,
    active_agents: int = 0,
    platform: str | None = None,
    platform_state: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    **extra: Any,
) -> None:
    """Persist gateway runtime health information for diagnostics/status.

    This mirrors 长期助手's write_runtime_status() pattern.
    """
    payload = _read_json_file(status_path) or {
        "kind": "my-agent-gateway",
        "pid": os.getpid(),
        "start_time": _get_process_start_time(os.getpid()),
        "gateway_state": "unknown",
        "exit_reason": None,
        "restart_requested": False,
        "active_agents": 0,
        "platforms": {},
        "updated_at": _utc_now_iso(),
    }
    payload.setdefault("platforms", {})
    payload["pid"] = os.getpid()
    payload["start_time"] = _get_process_start_time(os.getpid())
    payload["updated_at"] = _utc_now_iso()

    if gateway_state is not None:
        payload["gateway_state"] = gateway_state
    if exit_reason is not None:
        payload["exit_reason"] = exit_reason
    payload["restart_requested"] = restart_requested
    payload["active_agents"] = max(0, int(active_agents))
    for k, v in extra.items():
        if v is not None:
            payload[k] = v

    if platform is not None:
        platform_payload = payload["platforms"].get(platform, {})
        if platform_state is not None:
            platform_payload["state"] = platform_state
        if error_code is not None:
            platform_payload["error_code"] = error_code
        if error_message is not None:
            platform_payload["error_message"] = error_message
        platform_payload["updated_at"] = _utc_now_iso()
        payload["platforms"][platform] = platform_payload

    _write_json_file(status_path, payload)


def read_runtime_status(status_path: Path) -> dict | None:
    """Read the persisted gateway runtime health/status information."""
    return _read_json_file(status_path)


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


def request_graceful_shutdown(pid_path: Path, stop_request_path: Path, reason: str = "user request") -> bool:
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
