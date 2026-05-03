from __future__ import annotations

"""Small locked file helpers for append-only local ledgers."""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback.
    fcntl = None


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def append_jsonl(path: str | Path, payload: dict[str, Any], *, sort_keys: bool = False) -> None:
    """Append one JSONL record with a per-file lock.

    In plain terms: JSONL is our local ledger, one event per line. When two
    runners write at the same time, this helper makes them line up at the door
    so a record is written as a complete line instead of being mixed with
    another record.
    """

    line = json.dumps(payload, ensure_ascii=False, sort_keys=sort_keys)
    append_line_locked(path, line)


def append_line_locked(path: str | Path, line: str) -> None:
    """Append one text line while holding a lock beside the target file.

    The caller passes the line content without the trailing newline. This keeps
    every append operation shaped the same way and avoids half-written JSONL
    rows when local workers run concurrently.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _locked_text_file(target) as handle:
        handle.write(line.rstrip("\n") + "\n")
        handle.flush()


@contextmanager
def _locked_text_file(path: Path) -> Iterator[TextIO]:
    """Open an append target after taking both thread and OS file locks."""

    process_lock = _thread_lock_for(path)
    with process_lock:
        lock_path = path.with_name(path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            _lock_os_file(lock_handle)
            try:
                with path.open("a", encoding="utf-8") as target:
                    yield target
            finally:
                _unlock_os_file(lock_handle)


def _thread_lock_for(path: Path) -> threading.Lock:
    resolved = str(path.resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(resolved)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[resolved] = lock
        return lock


def _lock_os_file(handle: TextIO) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_os_file(handle: TextIO) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
