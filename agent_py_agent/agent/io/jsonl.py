
from __future__ import annotations

"""Small locked file helpers for append-only local ledgers."""

import json
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, TextIO

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows import guard.
    fcntl = None


class _PathLockEntry:
    """带引用计数的路径锁；归零且超出容量水位时回收，长驻进程不再无限增长。"""

    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.refs = 0


_LOCKS: dict[str, _PathLockEntry] = {}
_LOCKS_GUARD = threading.Lock()
_LOCKS_CAPACITY_WATERMARK = 512


def append_jsonl(path: str | Path, payload: dict[str, Any], *, sort_keys: bool = False) -> None:
    """Append one JSONL record with a per-file lock.

    In plain terms: JSONL is our local ledger, one event per line. When two
    runners write at the same time, this helper makes them line up at the door
    so a record is written as a complete line instead of being mixed with
    another record."""

    line = json.dumps(payload, ensure_ascii=False, sort_keys=sort_keys)
    append_line_locked(path, line)


def append_line_locked(path: str | Path, line: str) -> None:
    """Append one text line while holding a lock beside the target file.

    The caller passes the line content without the trailing newline. This keeps
    every append operation shaped the same way and avoids half-written JSONL
    rows when local workers run concurrently."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _locked_text_file(target) as handle:
        handle.write(line.rstrip("\n") + "\n")
        handle.flush()


@contextmanager
def _locked_text_file(path: Path) -> Iterator[TextIO]:
    """Open an append target after taking both thread and OS file locks."""

    resolved = str(path.resolve())
    entry = _acquire_lock_entry(resolved)
    try:
        with entry.lock, ExitStack() as stack:
            lock_path = path.with_name(path.name + ".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_handle = stack.enter_context(lock_path.open("a+", encoding="utf-8"))
            _lock_os_file(lock_handle)
            stack.callback(_unlock_os_file, lock_handle)
            target = stack.enter_context(path.open("a", encoding="utf-8"))
            yield target
    finally:
        _release_lock_entry(resolved, entry)


def _acquire_lock_entry(resolved: str) -> _PathLockEntry:
    with _LOCKS_GUARD:
        entry = _LOCKS.get(resolved)
        if entry is None:
            entry = _PathLockEntry()
            _LOCKS[resolved] = entry
        entry.refs += 1
        return entry


def _release_lock_entry(resolved: str, entry: _PathLockEntry) -> None:
    with _LOCKS_GUARD:
        entry.refs -= 1
        if entry.refs <= 0 and len(_LOCKS) > _LOCKS_CAPACITY_WATERMARK and _LOCKS.get(resolved) is entry:
            del _LOCKS[resolved]


def _lock_os_file(handle: TextIO) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    else:
        from ..common.file_lock_support import warn_file_lock_unavailable_once
        warn_file_lock_unavailable_once()


def _unlock_os_file(handle: TextIO) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
