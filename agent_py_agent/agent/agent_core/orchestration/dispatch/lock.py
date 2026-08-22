from __future__ import annotations

# LLM: Dispatch supervision needs thread-and-process exclusion, so lock authority is the kernel
# advisory lock held by an open descriptor. JSON is observability metadata only; empty, malformed,
# stale, or PID-reused file contents must never create or release ownership.
# 模块用途: 给派工巡查和孤儿恢复提供跨线程、跨进程互斥；进程崩溃后内核会自动释放锁。
"""Kernel-backed file lock for dispatch watch loops."""

import json
import os
import time
import uuid
from pathlib import Path
from typing import TextIO


# LLM: Non-blocking acquisition keeps supervision ticks cheap. POSIX uses flock like 会话运行时's
# reservation locks; Windows locks the first byte with msvcrt and seeds that byte if necessary.
# 函数用途: 尝试立即锁住已经打开的文件；别人持有时返回 False，不等待也不删文件。
def _try_advisory_lock(handle: TextIO) -> bool:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(" ")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


# LLM: Unlock mirrors _try_advisory_lock and is best-effort during unwinding; descriptor close is
# the final authority and also releases the lock after process death.
# 函数用途: 主动释放文件锁；即使显式解锁失败，随后关闭描述符仍会让内核回收。
def _release_advisory_lock(handle: TextIO) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


# LLM: Metadata is rewritten only after kernel ownership is acquired. A crash during this write
# may leave partial JSON, but the next owner can still lock and replace it safely.
# 函数用途: 把当前持有者信息写进锁文件供排障查看；JSON 内容不参与锁归属判断。
def _write_lock_metadata(handle: TextIO, payload: dict[str, object]) -> None:
    handle.seek(0)
    json.dump(payload, handle, ensure_ascii=False)
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())


# LLM: Keep the public context-manager shape used by watch_service and orphan supervision. `force`
# no longer steals a live kernel lock; it is retained for CLI compatibility and only affects the
# contention message. Never unlink this path while a descriptor may hold its inode.
# 类用途: 在 with 期间独占一条派工巡查链；退出或进程崩溃都会释放，锁文件可长期保留作诊断。
class _DispatchWatchLock:

    # LLM: Construction has no filesystem side effects; acquisition belongs to __enter__.
    # 函数用途: 记录锁路径和本次唯一 token，真正抢锁要等进入 with。
    def __init__(self, path: Path, *, force: bool = False):
        self.path = path
        self.force = force
        self.token = uuid.uuid4().hex
        self.acquired = False
        self._handle: TextIO | None = None

    # LLM: Open/create the stable inode, acquire the kernel lock, then publish metadata. Failure to
    # acquire is ordinary contention and must not inspect/delete stale JSON or bypass a live owner.
    # 函数用途: 非阻塞抢锁；成功后写持有者信息，失败就抛出可由监督器识别的占用异常。
    def __enter__(self) -> _DispatchWatchLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        if not _try_advisory_lock(handle):
            handle.close()
            suffix = "; --force-lock 不能抢占仍在运行的持有者" if self.force else ""
            raise RuntimeError(f"dispatch watch lock already held: {self.path}{suffix}")
        try:
            _write_lock_metadata(handle, self._lock_payload())
        except Exception:
            _release_advisory_lock(handle)
            handle.close()
            raise
        self._handle = handle
        self.acquired = True
        return self

    # LLM: Release by descriptor ownership, never by comparing mutable JSON tokens or unlinking a
    # shared path. This remains correct if another component corrupts the metadata while held.
    # 函数用途: 离开 with 时释放内核锁并关闭文件；没有成功获取过则什么也不做。
    def __exit__(self, exc_type, exc, tb) -> None:
        handle = self._handle
        self._handle = None
        self.acquired = False
        if handle is None:
            return
        _release_advisory_lock(handle)
        handle.close()

    # LLM: Payload fields are diagnostic only. Token distinguishes sequential owners in logs;
    # created_at helps humans inspect duration but does not drive stale-lock decisions.
    # 函数用途: 生成当前锁持有者的可观测信息。
    def _lock_payload(self) -> dict[str, object]:
        return {
            "schema_version": "dispatch-watch-lock.v2",
            "token": self.token,
            "pid": os.getpid(),
            "created_at": time.time(),
        }
