"""SQLite 探针只查路径元数据，由 SQLite 自己管理文件描述符和进程锁。"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from ..path_access_policy import SQLITE_SIDECAR_SUFFIXES


def sqlite_readonly_probe_uri(db: Path) -> str:
    """保留 SQLite 的锁与 WAL 语义，不用 immutable 绕过并发写者。"""
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
