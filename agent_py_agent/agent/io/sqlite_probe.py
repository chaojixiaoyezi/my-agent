"""普通探针只查元数据；换库只在连接已关闭的边界核对 SQLite 排他锁。"""
from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..path_access_policy import SQLITE_SIDECAR_SUFFIXES

# SQLite unix VFS 的固定文件格式边界，不是等待或容量参数。
_SQLITE_SHARED_FIRST, _SQLITE_SHARED_SIZE = 0x40000000 + 2, 510


def _require_no_local_database_handles(db: Path) -> None:
    if os.name == "nt":
        return  # Windows replace 自身拒绝仍打开的 SQLite 文件。
    expected = os.lstat(db)
    # 不打开原库：关闭任一同 inode 的新 FD 会破坏本进程已有 SQLite record lock。
    with os.scandir("/dev/fd") as entries:
        for entry in entries:
            try:
                info = os.fstat(int(entry.name))
            except (OSError, ValueError):
                continue
            if (info.st_dev, info.st_ino) == (expected.st_dev, expected.st_ino):
                raise BlockingIOError("runtime_db_local_reader_attached")


@contextmanager
def sqlite_connections_locked_out(db: Path) -> Iterator[None]:
    """本进程连接已排空后检查非托管执行体；持锁期间不能再开关 SQLite/原库 FD。

    POSIX record lock 随本进程任一同 inode FD 关闭而释放，因此此门只包最后的
    sidecar 摘除与 replace，不包校验/复制。Windows 由原 EX 门和 replace 拒绝打开文件。
    """
    _require_no_local_database_handles(db)
    descriptor = os.open(db, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened, current = os.fstat(descriptor), os.lstat(db)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise BlockingIOError("SQLite 文件身份已变化")
        if os.name != "nt":
            import fcntl

            fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB,
                        _SQLITE_SHARED_SIZE, _SQLITE_SHARED_FIRST)
        yield
    finally:
        os.close(descriptor)


def sqlite_readonly_probe_uri(db: Path) -> str:
    """保留 SQLite 的锁与 WAL 语义，不用 immutable 绕过并发写者。"""
    from ..runtime_db.recovery_guard import (
        RuntimeDatabaseDeferred,
        runtime_db_exclusive_owned,
        runtime_db_recovery_pending,
    )

    if runtime_db_recovery_pending(db) and not runtime_db_exclusive_owned(db):
        raise RuntimeDatabaseDeferred("recovery_pending")
    return Path(db).absolute().as_uri() + "?mode=ro"


def _opened_sqlite_header(db: Path) -> bytes:
    """仅在调用方持有 SQLite 连接时借读其 FD；不接管、不复制或关闭库描述符。"""
    expected = os.lstat(db)
    identity = expected.st_dev, expected.st_ino
    if not hasattr(os, "pread"):
        return b""
    try:
        # 只查本进程当前活 FD，不扫描历史目录；数据库的打开/关闭仍全由 SQLite 管理。
        with os.scandir("/dev/fd") as entries:
            for entry in entries:
                try:
                    descriptor = int(entry.name)
                    before = os.fstat(descriptor)
                    if (before.st_dev, before.st_ino) != identity:
                        continue
                    header = os.pread(descriptor, 20, 0)
                    after = os.fstat(descriptor)
                    if (after.st_dev, after.st_ino) == identity:
                        return header
                except (OSError, ValueError):
                    continue
    except OSError:
        pass
    return b""


def require_plain_sqlite_files(db: Path, *, beneath: Path | None = None) -> None:
    """仅用 lstat 拒绝链接及非普通库文件，避免原始 fd 关闭释放 SQLite 锁。"""
    db = Path(db)
    if beneath is not None:
        root = Path(beneath).absolute()
        parts = db.absolute().relative_to(root).parts
        if not parts or ".." in parts or ".." in root.parts:
            raise ValueError("SQLite 库必须位于指定根目录内")
        parent = root
        for part in ("", *parts[:-1]):
            parent = parent / part
            if not stat.S_ISDIR(os.lstat(parent).st_mode):
                raise ValueError("拒绝链接或非目录 SQLite 父路径")
    for suffix in ("", *SQLITE_SIDECAR_SUFFIXES):
        try:
            info = os.lstat(Path(str(db) + suffix))
        except FileNotFoundError:
            if not suffix:
                raise
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("拒绝链接或非普通单链接 SQLite 文件")
