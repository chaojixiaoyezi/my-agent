
from __future__ import annotations

"""Gateway daemon metadata helpers shared by PID, lock, and status modules."""

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_process_start_time(pid: int) -> int | None:
    if sys.platform == "win32":
        return None
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        # Field 22 in /proc/<pid>/stat is process start time (clock ticks).
        return int(stat_path.read_text().split()[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
        return None


def _scope_hash(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _build_pid_record() -> dict:
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
