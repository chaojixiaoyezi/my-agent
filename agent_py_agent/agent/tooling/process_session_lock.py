# LLM: 目录锁只串行本版进程 Store；固定锁文件不可替换或删除，旧 v1 写入仍须按记录取原锁。
# 模块用途: 用标准库在线程和独立进程之间互斥后台记录事务；没有系统锁时拒绝继续写权威数据。
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

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows 使用下面的字节锁。
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None


# LLM: 只保存锁，不缓存记录或取消状态；等待者的强引用保证弱表中的锁不会被提前回收。
# 类用途: 为同一后台目录提供可弱引用的线程锁容器。
@dataclass
class _StoreMutex:
    lock: threading.Lock = field(default_factory=threading.Lock)


_MUTEXES: weakref.WeakValueDictionary[str, _StoreMutex] = weakref.WeakValueDictionary()
_MUTEX_GUARD = threading.Lock()


# LLM: 调用方必须传规范目录；锁顺序固定为目录线程锁、目录系统锁、旧记录锁，不支持重入。
# 函数用途: 在整个读改写或恢复期间锁住同一 Store，创建私有目录及永久锁文件。
@contextmanager
def locked_process_sessions(root: Path) -> Iterator[None]:
    if fcntl is None and msvcrt is None:
        raise RuntimeError("managed process store requires an OS file lock")
    with _MUTEX_GUARD:
        mutex = _MUTEXES.get(str(root))
        if mutex is None:
            mutex = _StoreMutex()
            _MUTEXES[str(root)] = mutex
    with mutex.lock:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        descriptor = os.open(root / ".process-sessions.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _acquire_file_lock(descriptor)
            try:
                yield
            finally:
                _release_file_lock(descriptor)
        finally:
            os.close(descriptor)


# LLM: Windows 在固定偏移锁一字节；只重试锁冲突，中断和权限错误必须离开临界区。
# 函数用途: 获取当前系统支持的文件排他锁，防止两个独立 Gateway/runner 同时提交进程记录。
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
