
from __future__ import annotations

"""File lock for dispatch watch loops."""

import json
import os
import time
import uuid
from pathlib import Path

from ....gateway_parts.process_control import is_pid_alive


class _DispatchWatchLock:

    def __init__(self, path: Path, *, force: bool = False):
        self.path = path
        self.force = force
        self.token = uuid.uuid4().hex
        self.acquired = False

    def __enter__(self) -> _DispatchWatchLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prepare_existing_lock()
        payload = self._lock_payload()
        self._write_lock_file(payload)
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.acquired or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("token") == self.token:
            self.path.unlink()

    def _prepare_existing_lock(self) -> None:
        if self.force and self.path.exists():
            self.path.unlink()
            return
        if self.path.exists():
            self._remove_stale_lock()

    def _lock_payload(self) -> dict[str, object]:
        return {"token": self.token, "pid": os.getpid(), "created_at": time.time()}

    def _write_lock_file(self, payload: dict[str, object]) -> None:
        try:
            self._write_lock_file_exclusive(payload)
        except FileExistsError as exc:
            self._retry_write_after_stale_lock(payload, exc)

    def _write_lock_file_exclusive(self, payload: dict[str, object]) -> None:
        with self.path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))

    def _retry_write_after_stale_lock(self, payload: dict[str, object], exc: FileExistsError) -> None:
        if not self._remove_stale_lock():
            raise RuntimeError(
                f"dispatch watch lock already exists: {self.path}; "
                "confirm no parent agent is running, then use --force-lock."
            ) from exc
        self._write_lock_file_exclusive(payload)

    def _remove_stale_lock(self) -> bool:
        pid = self._existing_lock_pid()
        if pid is None or is_pid_alive(pid):
            return False
        try:
            self.path.unlink()
        except OSError:
            return False
        return True

    def _existing_lock_pid(self) -> int | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return int(payload.get("pid", 0))
        except (TypeError, ValueError):
            return None
