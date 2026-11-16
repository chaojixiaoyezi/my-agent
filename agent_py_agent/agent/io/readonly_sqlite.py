"""在可信目录中只读 SQLite；原生 WAL/SHM 协调可创建辅助文件，不迁移业务数据。"""
from __future__ import annotations

import errno
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from ..common.json_io import check_io_deadline


def _windows_sqlite_platform():
    return os.name == "nt"


def _reject_reparse(info):
    if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise ValueError("readonly SQLite path contains a reparse point")


@contextmanager
def _portable_read_directory(path):
    """复用仓内Windows逐层nofollow检查；复核身份，不冒充POSIX dirfd原子保证或ACL校验。"""
    from ..common.nofollow_fs import _portable_directory

    anchor, parts = Path(path.anchor), path.parent.parts[1:]
    _portable_directory(anchor, parts, create=False)

    def snapshot():
        result = []
        for depth in range(len(parts) + 1):
            directory = anchor.joinpath(*parts[:depth])
            info = directory.lstat()
            _reject_reparse(info)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ValueError("readonly SQLite parent is not a regular directory")
            result.append((info.st_dev, info.st_ino, info.st_mode))
        return tuple(result)

    expected = snapshot()
    def verify():
        _portable_directory(anchor, parts, create=False)
        if snapshot() != expected:
            raise ValueError("readonly SQLite parent identity changed")
    verify()
    yield None, verify


# LLM: 投影数据库必须在私有、无链接目录内；连接前后核同一普通文件，侧车不能沿链接产生副作用。
# 函数用途: 拒绝链接、硬链接和特殊文件，取得已存在文件身份。
def _regular_identity(path: Path, *, require_owner=False) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    _reject_reparse(info)
    if require_owner and not _windows_sqlite_platform() and info.st_uid != os.geteuid():
        raise PermissionError("readonly SQLite file must belong to current identity")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"projection requires a single-link regular file: {path}")
    return info.st_dev, info.st_ino


# LLM: 构造与复核共用同一信任门；仅以已打开FD收紧当前用户的.my-agent子树，不改普通私有组祖先。
# 函数用途: 拒绝非可信属主和非sticky公共可写祖先，兼容当前用户的私有组目录。
def _ensure_trusted_ancestor(descriptor, current_path: Path, parent_stat, *, read_only=False):
    owner = os.geteuid()
    if parent_stat.st_uid not in {0, owner}:
        raise PermissionError("projection ancestor is not a trusted directory")
    if parent_stat.st_mode & stat.S_ISVTX:
        return parent_stat
    if parent_stat.st_mode & 0o002:
        raise PermissionError("projection ancestor is not a trusted directory")
    if parent_stat.st_mode & 0o020:
        if parent_stat.st_uid == owner and ".my-agent" in current_path.parts:
            if read_only:
                raise PermissionError("projection ancestor requires permission repair")
            os.fchmod(descriptor, parent_stat.st_mode & ~0o077)
            return os.fstat(descriptor)
        if parent_stat.st_uid != owner or parent_stat.st_gid != os.getegid():
            raise PermissionError("projection ancestor is not a trusted directory")
    return parent_stat


# LLM: 逐层dir_fd/no-follow保留父链；按共用信任门收紧.my-agent祖先，投影父目录始终要求本人私有。
# 函数用途: 在SQLite按路径打开之前建立私有父目录，并提供同一目录链的身份复核。
@contextmanager
def _projection_directory(path: Path, *, read_only=False, require_private_directory=True):
    if read_only and _windows_sqlite_platform():
        # Windows没有POSIX uid/mode和dir_fd；不建目录、不改ACL，沿原portable契约拒绝全部reparse。
        with _portable_read_directory(path) as state:
            yield state
        return
    if os.open not in os.supports_dir_fd:
        raise OSError(errno.ENOTSUP, "projection requires descriptor-relative directory access")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    chain = []
    current = Path(path.anchor)
    descriptor = os.open(current, flags)
    chain.append((current, descriptor))
    try:
        for part in path.parent.parts[1:]:
            _ensure_trusted_ancestor(descriptor, current, os.fstat(descriptor), read_only=read_only)
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if read_only:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=descriptor)
            current = current / part
            descriptor = child
            chain.append((current, descriptor))
        parent = _ensure_trusted_ancestor(descriptor, current, os.fstat(descriptor), read_only=read_only)
        if parent.st_uid != os.geteuid():
            raise PermissionError("projection directory must belong to the current identity")
        forbidden = 0o077 if require_private_directory else 0o022
        if parent.st_mode & forbidden:
            if read_only:
                raise PermissionError("projection directory requires permission repair")
            os.fchmod(descriptor, 0o700)

        # LLM: 复核所有已打开父节点及相同信任门；不以旧路径stat替代持有FD上的权限事实。
        # 函数用途: 在原锁等待后及SQLite连接前后拒绝父链换代，并自愈允许收紧的组写权限。
        def verify():
            for directory, held in chain:
                observed, opened = directory.lstat(), os.fstat(held)
                if (not stat.S_ISDIR(observed.st_mode)
                        or (observed.st_dev, observed.st_ino) != (opened.st_dev, opened.st_ino)):
                    raise ValueError("projection parent identity or trust changed")
                _ensure_trusted_ancestor(held, directory, opened, read_only=read_only)
            if os.fstat(descriptor).st_mode & forbidden:
                raise PermissionError("projection directory is no longer private")

        verify()
        yield descriptor, verify
    finally:
        for _directory, held in reversed(chain):
            os.close(held)


class ProjectionReadUnavailableError(OSError):
    """现存投影无法只读核实；调用方应保留 unknown，不初始化或修复。"""


@contextmanager
def readonly_sqlite_connection(path: Path, *, blocking: bool = True, deadline: float | None = None,
                               require_private_directory: bool = True):
    check_io_deadline(deadline)
    path = Path(path).absolute()
    db = None
    caller_error = False
    try:
        with _projection_directory(path, read_only=True, require_private_directory=require_private_directory) as (_parent_fd, verify):
            # POSIX 下关闭任一同 inode 裸 FD 会释放本进程全部 SQLite record locks。
            # 主库只能由 SQLite 打开/关闭；nofollow 检查仅用受信父链和 lstat 身份。
            identity = _regular_identity(path, require_owner=True)
            if identity is None:
                raise FileNotFoundError(errno.ENOENT, 'readonly SQLite database is missing')
            sidefiles = {suffix: _regular_identity(path.with_name(path.name + suffix), require_owner=True)
                         for suffix in ("-journal", "-wal", "-shm")}
            verify()
            timeout = max(0.0, deadline - time.monotonic()) if deadline is not None else 5.0
            db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                 timeout=timeout if blocking else 0.0)
            try:
                verify()
                if _regular_identity(path, require_owner=True) != identity:
                    raise ValueError("projection identity changed during read")
                for suffix, before in sidefiles.items():
                    observed = _regular_identity(path.with_name(path.name + suffix), require_owner=True)
                    if before is not None and observed != before:
                        raise ValueError("projection sidefile identity changed during read")
                # Schema is domain-specific; readers validate their own version and tables.
                try:
                    yield db
                except BaseException:
                    caller_error = True
                    raise
                check_io_deadline(deadline)
                verify()
                if _regular_identity(path, require_owner=True) != identity:
                    raise ValueError("projection identity changed after read")
                for suffix, before in sidefiles.items():
                    observed = _regular_identity(path.with_name(path.name + suffix), require_owner=True)
                    if before is not None and observed != before:
                        raise ValueError("projection sidefile identity changed after read")
            finally:
                db.close()
    except (OSError, ValueError, sqlite3.Error) as exc:
        if caller_error:
            raise
        reason = str(getattr(exc, 'strerror', None) or exc)
        reason = reason.replace(str(path), '<database>').replace(str(path.parent), '<directory>')
        reason = ' '.join(reason.split())[:160]
        raise ProjectionReadUnavailableError(
            f"PROJECTION_READ_UNAVAILABLE: {type(exc).__name__}: {reason}; effect_outcome=not_started; "
            "next_action=retain unknown and retry existing projection later") from exc


