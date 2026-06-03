
from __future__ import annotations

"""Machine-local scoped locks for gateway identities."""

import json
import os
from pathlib import Path
from typing import Any

from ..runtime_errors import DataCorruptionError, runtime_error_report
from .daemon_metadata import (
    _build_pid_record,
    _get_process_start_time,
    _scope_hash,
    _utc_now_iso,
    _write_json_file,
)
from .process_control import is_pid_alive


def _get_lock_dir() -> Path:
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


def _read_lock_record_report(lock_path: Path) -> tuple[dict | None, dict | None]:
    if not lock_path.exists():
        return None, None
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        return None, _lock_load_error(lock_path, exc)
    if not raw:
        return None, _lock_load_error(lock_path, DataCorruptionError("scoped lock file is empty"))
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, _lock_load_error(lock_path, exc)
    if not isinstance(payload, dict):
        return None, _lock_load_error(
            lock_path,
            DataCorruptionError(f"scoped lock root is {type(payload).__name__}, expected object"),
        )
    return payload, None


def _lock_load_error(lock_path: Path, exc: BaseException) -> dict:
    report = runtime_error_report(exc, context="gateway.scoped_lock.read")
    report["path"] = str(lock_path)
    return {"lock_load_error": report}


def _read_lock_record_or_error(lock_path: Path) -> dict | None:
    record, load_error = _read_lock_record_report(lock_path)
    return load_error or record


def acquire_scoped_lock(
    scope: str, identity: str, metadata: dict[str, Any] | None = None
) -> tuple[bool, dict | None]:
    lock_path = _get_scope_lock_path(scope, identity)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    record = _build_scope_lock_record(scope, identity, metadata)
    existing, load_error = _read_lock_record_report(lock_path)
    if load_error is not None:
        return False, load_error
    if existing and _owns_lock(existing, record):
        _write_json_file(lock_path, record)
        return True, existing
    if existing and not _lock_process_stale(existing):
        return False, existing
    if existing:
        _remove_lock_file(lock_path)
    if not _create_lock_file(lock_path, record):
        return False, _read_lock_record_or_error(lock_path)
    return True, None


def release_scoped_lock(scope: str, identity: str) -> None:
    lock_path = _get_scope_lock_path(scope, identity)
    existing, load_error = _read_lock_record_report(lock_path)
    if load_error is not None:
        return
    if not existing or existing.get("pid") != os.getpid():
        return
    if existing.get("start_time") != _get_process_start_time(os.getpid()):
        return
    _remove_lock_file(lock_path)


def release_all_scoped_locks() -> int:
    lock_dir = _get_lock_dir()
    if not lock_dir.exists():
        return 0
    removed = 0
    for lock_file in lock_dir.glob("*.lock"):
        if _release_lock_if_stale(lock_file):
            removed += 1
    return removed


def _release_lock_if_stale(lock_file: Path) -> bool:
    record, load_error = _read_lock_record_report(lock_file)
    if load_error is not None:
        return False
    if not record:
        return False
    try:
        pid = int(record["pid"])
    except (ProcessLookupError, PermissionError, ValueError):
        lock_file.unlink()
        return True
    if not is_pid_alive(pid):
        lock_file.unlink()
        return True
    if _get_process_start_time(pid) != record.get("start_time"):
        lock_file.unlink()
        return True
    return False
