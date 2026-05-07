# LLM: Low-level IO module; keep atomic/locked file-write behavior stable across platforms.
# 模块用途: 提供 JSONL 和加锁文件写入等底层文件能力。

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
except ImportError:  # pragma: no cover - Windows fallback.
    fcntl = None


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


# LLM: append_jsonl belongs to 底层文件 IO; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: Append one JSONL record with a per-file lock. In plain terms: JSONL is our local ledger, one event per line. When two runners write at the s；会读写 JSON 结构，改字段时要保持兼容。
def append_jsonl(path: str | Path, payload: dict[str, Any], *, sort_keys: bool = False) -> None:
    """Append one JSONL record with a per-file lock.

    In plain terms: JSONL is our local ledger, one event per line. When two
    runners write at the same time, this helper makes them line up at the door
    so a record is written as a complete line instead of being mixed with
    another record."""

    line = json.dumps(payload, ensure_ascii=False, sort_keys=sort_keys)
    append_line_locked(path, line)


# LLM: append_line_locked belongs to 底层文件 IO; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: Append one text line while holding a lock beside the target file. The caller passes the line content without the trailing newline. This keep；会写入或调整文件，改动时要确认路径、安全边界和失败恢复。
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


# LLM: _locked_text_file belongs to 底层文件 IO; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: Open an append target after taking both thread and OS file locks.；会写入或调整文件，改动时要确认路径、安全边界和失败恢复。
@contextmanager
def _locked_text_file(path: Path) -> Iterator[TextIO]:
    """Open an append target after taking both thread and OS file locks."""

    process_lock = _thread_lock_for(path)
    with process_lock, ExitStack() as stack:
        lock_path = path.with_name(path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_handle = stack.enter_context(lock_path.open("a+", encoding="utf-8"))
        _lock_os_file(lock_handle)
        stack.callback(_unlock_os_file, lock_handle)
        target = stack.enter_context(path.open("a", encoding="utf-8"))
        yield target


# LLM: _thread_lock_for belongs to 底层文件 IO; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 完成 底层文件 IO 里的 _thread_lock_for 步骤，保持现有返回值、异常和副作用语义。
def _thread_lock_for(path: Path) -> threading.Lock:
    resolved = str(path.resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(resolved)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[resolved] = lock
        return lock


# LLM: _lock_os_file belongs to 底层文件 IO; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 完成 底层文件 IO 里的 _lock_os_file 步骤，保持现有返回值、异常和副作用语义。
def _lock_os_file(handle: TextIO) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


# LLM: _unlock_os_file belongs to 底层文件 IO; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 完成 底层文件 IO 里的 _unlock_os_file 步骤，保持现有返回值、异常和副作用语义。
def _unlock_os_file(handle: TextIO) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
