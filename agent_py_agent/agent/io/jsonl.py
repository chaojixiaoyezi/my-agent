
from __future__ import annotations

"""Small locked file helpers for append-only local ledgers."""

import json
import os
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, TextIO

from ..common.nofollow_fs import open_private_lock_beneath_tightened, split_existing_anchor

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
# 文件锁表容量水位；达到后清理不再使用的锁条目，防长跑进程锁表无界增长（无物理单位）。
_LOCKS_CAPACITY_WATERMARK = 512


def append_jsonl(path: str | Path, payload: dict[str, Any], *, sort_keys: bool = False) -> None:
    """Append one JSONL record with a per-file lock.

    In plain terms: JSONL is our local ledger, one event per line. When two
    runners write at the same time, this helper makes them line up at the door
    so a record is written as a complete line instead of being mixed with
    another record."""

    line = json.dumps(payload, ensure_ascii=False, sort_keys=sort_keys)
    target = Path(path)
    # 目录由下面的私有锁原语负责（缺失逐级按 0700 新建、已存在一律不动）；原来这行 mkdir 只保最后一级 0700
    # （pbfix 2026-10-04 删除无调用方的 append_line_locked 时一并去掉）。
    with _locked_text_file(target) as handle:
        handle.write(line.rstrip("\n") + "\n")
        handle.flush()


@contextmanager
def _locked_text_file(path: Path) -> Iterator[TextIO]:
    """Open an append target after taking both thread and OS file locks.

    锁文件与数据文件同口径私有：经 open_private_lock_beneath 拿 fd（文件 0600、缺失目录按 0700 新建、
    已存在的目录一律不动、不跟随符号链接），只 flock、不写内容；原来的 Path.open("a+")+mkdir 会按 umask
    落成 0644/0755，同机他用户能打开并 flock 抢锁卡住写入。已存在的 0644 锁在打开时收紧到 0600。"""

    resolved = str(path.resolve())
    entry = _acquire_lock_entry(resolved)
    try:
        with entry.lock, ExitStack() as stack:
            lock_path = path.with_name(path.name + ".lock")
            descriptor = _open_lock_descriptor(lock_path)
            stack.callback(os.close, descriptor)
            _lock_os_descriptor(descriptor)
            stack.callback(_unlock_os_descriptor, descriptor)
            target = stack.enter_context(path.open("a", encoding="utf-8"))
            yield target
    finally:
        _release_lock_entry(resolved, entry)


def _open_lock_descriptor(lock_path: Path) -> int:
    """Open one private lock descriptor, tightening an existing world-readable lock to 0600.

    把绝对锁路径拆成“已存在的最近父目录 + 其余段”，让缺失目录仍统一经 no-follow 原语创建（0700；
    已存在的目录一律不动）。"""

    lock_path = Path(os.path.abspath(lock_path))
    anchor, missing = split_existing_anchor(lock_path.parent)
    return open_private_lock_beneath_tightened(anchor, (*missing, lock_path.name))


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


def _lock_os_descriptor(descriptor: int) -> None:
    """Take the same OS exclusive lock as _lock_os_file, on the raw descriptor the private lock returns."""

    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    else:
        from ..common.file_lock_support import warn_file_lock_unavailable_once
        warn_file_lock_unavailable_once()


def _unlock_os_descriptor(descriptor: int) -> None:
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _unlock_os_file(handle: TextIO) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
