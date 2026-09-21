# LLM: 每个权威目录必须选择固定锁名，锁文件不可替换或删除；本模块不读取状态，不支持重入，也不降级为仅线程锁。
# 模块用途: 复用原进程 Store 的标准库互斥，在同一临界区串行权威文件的线程与跨进程读写。
from __future__ import annotations

import errno
import os
import threading
import time
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .nofollow_fs import open_private_lock_beneath

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows 使用下面的字节锁。
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None


# LLM: 只保存锁，不缓存记录或取消状态；等待者的强引用保证弱表中的锁不会被提前回收。
# 类用途: 为同一目录和锁名提供可弱引用的线程锁容器。
@dataclass
class _StoreMutex:
    lock: threading.Lock = field(default_factory=threading.Lock)


_MUTEXES: weakref.WeakValueDictionary[str, _StoreMutex] = weakref.WeakValueDictionary()
_MUTEX_GUARD = threading.Lock()


# LLM: root 是受信锚点，relative_parts 是宿主固定地址；锁顺序保持线程、系统、领域记录，永久锁不替换。
# 函数用途: 在读改写或恢复期间锁住同一目录，逐段准备私有目录并拒绝链接锁文件。
@contextmanager
def locked_private_directory(
    root: Path,
    *,
    lock_name: str,
    relative_parts: tuple[str, ...] = (),
) -> Iterator[None]:
    if not lock_name or lock_name in {".", ".."} or Path(lock_name).name != lock_name:
        raise ValueError("directory lock name must be one path component")
    if fcntl is None and msvcrt is None:
        raise RuntimeError("authority store requires an OS file lock")
    root = Path(os.path.abspath(root))
    key = str(root.joinpath(*relative_parts, lock_name))
    with _MUTEX_GUARD:
        mutex = _MUTEXES.get(key)
        if mutex is None:
            mutex = _StoreMutex()
            _MUTEXES[key] = mutex
    with mutex.lock:
        anchor, parts = _existing_anchor(root, relative_parts)
        descriptor = open_private_lock_beneath(anchor, (*parts, lock_name))
        try:
            _acquire_file_lock(descriptor)
            try:
                yield
            finally:
                _release_file_lock(descriptor)
        finally:
            os.close(descriptor)


# LLM: 原进程 Store 可能首次使用尚不存在的私有根；只向上找已有锚点，不创建目录也不解析链接。
# 函数用途: 把缺失根的固定后缀并入相对路径，让创建仍统一经过 no-follow 原语。
def _existing_anchor(root: Path, parts: tuple[str, ...]) -> tuple[Path, tuple[str, ...]]:
    while not root.exists() and not root.is_symlink():
        parts = (root.name, *parts)
        root = root.parent
    return root, parts


# LLM: Windows 在固定偏移锁一字节；只重试锁冲突，中断和权限错误必须离开临界区。
# 函数用途: 获取当前系统支持的文件排他锁，防止两个独立写入者同时提交同一权威记录。
def _acquire_file_lock(descriptor: int) -> None:
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return
    while True:
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            time.sleep(0.02)


# LLM: 解锁使用取得锁时的同一描述符和字节区间；文件本身一直保留，不产生两把同名锁。
# 函数用途: 释放系统排他锁，允许下一位记录读写者继续。
def _release_file_lock(descriptor: int) -> None:
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    else:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
