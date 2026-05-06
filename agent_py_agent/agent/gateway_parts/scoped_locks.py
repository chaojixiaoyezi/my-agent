from __future__ import annotations

"""Machine-local scoped locks for gateway identities."""

import json
import os
from pathlib import Path
from typing import Any

from .daemon_metadata import (
    _build_pid_record,
    _get_process_start_time,
    _read_json_file,
    _scope_hash,
    _utc_now_iso,
    _write_json_file,
)
from .process_control import is_pid_alive


def _get_lock_dir() -> Path:
    """Return the machine-local directory for scoped gateway locks."""
    state_home = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "my-agent" / "locks"


def _get_scope_lock_path(scope: str, identity: str) -> Path:
    return _get_lock_dir() / f"{scope}-{_scope_hash(identity)}.lock"


def _build_scope_lock_record(scope: str, identity: str, metadata: dict[str, Any] | None) -> dict:
    return {
        **_build_pid_record(),
        "scope": scope,
        "identity_hash": _scope_hash(identity),
        "metadata": metadata or {},
        "updated_at": _utc_now_iso(),
    }


def _lock_pid(record: dict | None) -> int | None:
    try:
        return int((record or {})["pid"])
    except (KeyError, TypeError, ValueError):
        return None


def _owns_lock(existing: dict, record: dict) -> bool:
    pid = _lock_pid(existing)
    return pid == os.getpid() and existing.get("start_time") == record.get("start_time")


def _lock_process_stale(existing: dict) -> bool:
    pid = _lock_pid(existing)
    if pid is None:
        return True
    # LLM: scoped locks share the gateway process liveness helper for Windows/mac parity.
    if not is_pid_alive(pid):
        return True
    current_start = _get_process_start_time(pid)
    recorded_start = existing.get("start_time")
    return recorded_start is not None and current_start is not None and current_start != recorded_start


def _remove_lock_file(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _create_lock_file(lock_path: Path, record: dict) -> bool:
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
    except Exception:
        _remove_lock_file(lock_path)
        raise
    return True


def acquire_scoped_lock(
    scope: str, identity: str, metadata: dict[str, Any] | None = None
) -> tuple[bool, dict | None]:
    """Acquire a machine-local lock keyed by scope + identity."""
    lock_path = _get_scope_lock_path(scope, identity)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    record = _build_scope_lock_record(scope, identity, metadata)
    existing = _read_json_file(lock_path)
    if existing and _owns_lock(existing, record):
        _write_json_file(lock_path, record)
        return True, existing
    if existing and not _lock_process_stale(existing):
        return False, existing
    if existing:
        _remove_lock_file(lock_path)
    if not _create_lock_file(lock_path, record):
        return False, _read_json_file(lock_path)
    return True, None


def release_scoped_lock(scope: str, identity: str) -> None:
    """Release a previously-acquired scope lock when owned by this process."""
    lock_path = _get_scope_lock_path(scope, identity)
    existing = _read_json_file(lock_path)
    if not existing or existing.get("pid") != os.getpid():
        return
    if existing.get("start_time") != _get_process_start_time(os.getpid()):
        return
    _remove_lock_file(lock_path)


def release_all_scoped_locks() -> int:
    """Remove all stale scoped lock files in the lock directory."""
    lock_dir = _get_lock_dir()
    if not lock_dir.exists():
        return 0
    removed = 0
    for lock_file in lock_dir.glob("*.lock"):
        if _release_lock_if_stale(lock_file):
            removed += 1
    return removed


def _release_lock_if_stale(lock_file: Path) -> bool:
    """Remove a lock file if its owning process is dead or PID was reused."""
    record = _read_json_file(lock_file)
    if not record:
        return False
    try:
        pid = int(record["pid"])
    except (ProcessLookupError, PermissionError, ValueError):
        lock_file.unlink()
        return True
    # LLM: stale lock cleanup must use the same cross-platform PID probe as gateway status.
    if not is_pid_alive(pid):
        lock_file.unlink()
        return True
    if _get_process_start_time(pid) != record.get("start_time"):
        lock_file.unlink()
        return True
    return False
