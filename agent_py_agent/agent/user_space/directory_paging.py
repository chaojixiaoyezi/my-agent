"""在原owner路径边界内分页目录；原生续点可持久化，其他平台有界重放。"""
from __future__ import annotations

import ctypes
import os
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from threading import RLock

from ..common.nofollow_fs import open_directory_beneath

_SCAN_STREAMS: OrderedDict = OrderedDict()
_SCAN_LOCK = RLock()


def _native_directory_supported():
    return os.name != 'nt' and bool(getattr(os, 'O_NOFOLLOW', 0) and getattr(os, 'O_DIRECTORY', 0))


def _require_entry_name(name):
    if type(name) is not str or name in {'', '.', '..'} or any(value in name for value in ('/', '\\', '\x00')):
        raise ValueError('invalid_directory_entry')


def directory_page(root: Path, parts: tuple, cursor: dict, limit: int, *,
                   require_dir_fd: bool = True) -> tuple[list[str], dict, bool]:
    # 复用现有配额枚举器的惰性dirent能力，不调用配额账、不改其锁或持久状态。
    from ..user_space.owner_quota import _quota_scandir

    if limit <= 0:
        return [], cursor, False
    if not _native_directory_supported() and not require_dir_fd:
        from ..common.nofollow_fs import (
            NoFollowPathError,
            _portable_directory,
            _validate_relative_parts,
        )

        _validate_relative_parts(parts, allow_empty=True)

        try:
            directory = _portable_directory(root, parts, create=False)
            info = directory.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return [], {}, True
        identity = [info.st_dev, info.st_ino]
        state = deepcopy(cursor) if cursor.get('directory_identity') == identity else {}
        key = (os.getpid(), str(root), parts, *identity)
        result = _portable_page(key, os.scandir(directory), state, identity, limit)
        latest = _portable_directory(root, parts, create=False).lstat()
        if (latest.st_dev, latest.st_ino) != tuple(identity):
            with _SCAN_LOCK:
                cached = _SCAN_STREAMS.pop(key, None)
                if cached is not None:
                    cached[0].close()
            raise NoFollowPathError('directory changed during portable enumeration')
        return result
    try:
        descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    except (FileNotFoundError, NotADirectoryError):
        return [], {}, True
    try:
        info = os.fstat(descriptor)
        identity = [info.st_dev, info.st_ino]
        state = deepcopy(cursor) if cursor.get("directory_identity") == identity else {}
        offset = state.get("offset", 0)
        if type(offset) is not int or offset < 0:
            raise ValueError("invalid_directory_offset")
        os.lseek(descriptor, offset, os.SEEK_SET)
        names, complete = [], False
        entries = _quota_scandir(descriptor)
        if not callable(getattr(entries, "checkpoint", None)):
            return _portable_page((os.getpid(), str(root), parts, *identity), entries, state, identity, limit)
        # 续点要放入16KiB维护回执；每次原生块限制4KiB，跨进程不重放前缀。
        entries.buffer = ctypes.create_string_buffer(4096)
        with entries:
            if state.get("pending"):
                entries.restore(state["pending"])
            for _ in range(limit):
                try:
                    entry = next(entries)
                except StopIteration:
                    complete = True
                    break
                _require_entry_name(entry.name)
                names.append(entry.name)
            saved = entries.checkpoint()
        return names, {"directory_identity": identity, "last_scanned": len(names), **saved}, complete
    finally:
        os.close(descriptor)


def _portable_page(key, opened, state, identity, limit):
    """最多32个惰性目录流；冷恢复逐片有界重放，原生后端直接持久化checkpoint。"""
    target = state.get("visited", 0)
    if type(target) is not int or target < 0:
        opened.close()
        raise ValueError("invalid_directory_replay")
    with _SCAN_LOCK:
        cached = _SCAN_STREAMS.pop(key, None)
        if cached is None or cached[1] != target:
            if cached is not None:
                cached[0].close()
            cached = [opened, target, target]
        else:
            opened.close()
        names, scanned, complete = [], 0, False
        try:
            for _ in range(limit):
                try:
                    entry = next(cached[0])
                except StopIteration:
                    complete = True
                    break
                scanned += 1
                if cached[2]:
                    cached[2] -= 1
                    continue
                _require_entry_name(entry.name)
                names.append(entry.name)
                cached[1] += 1
        except BaseException:
            cached[0].close()
            raise
        if complete:
            cached[0].close()
        else:
            _SCAN_STREAMS[key] = cached
        while len(_SCAN_STREAMS) > 32:
            _old_key, old = _SCAN_STREAMS.popitem(last=False)
            old[0].close()
        return names, {"directory_identity": identity, "offset": 0, "visited": cached[1],
                       "last_scanned": scanned}, complete
