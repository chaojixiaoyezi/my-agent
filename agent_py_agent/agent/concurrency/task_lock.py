
from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from ..settings.defaults import default_config_int
from .exceptions import LockAcquisitionError

if TYPE_CHECKING:
    from ..settings.config import AgentConfig as AgentConfigType


_LOG = logging.getLogger("agent.concurrency")
_DEFAULT_TASK_LOCK_TIMEOUT_SECONDS = default_config_int("task_lock_timeout_seconds")


class TaskLockManager:

    def __init__(self, config: AgentConfigType | None = None):
        self.enabled = _lock_enabled(config)
        self._read_locks: dict[str, threading.RLock] = {}
        self._write_lock = threading.RLock()
        self._lock_creation_time: dict[str, float] = {}
        self._cleanup_timeout = _cleanup_timeout(config)
        # 死锁检测兜底超时(复用 task_lock_timeout_seconds):正常持锁远短于此,超时即判
        # 疑似死锁/锁泄漏,放弃并抛 LockAcquisitionError 而非无限挂死(值守进程不被单锁拖垮)。
        self._acquire_timeout = self._cleanup_timeout

    def _get_read_lock(self, task_id: str) -> threading.RLock:
        # 只在 _write_lock 短临界区内取/建 lock 并刷新活跃时间;绝不在持 _write_lock 时
        # 阻塞 acquire task 锁——根治锁顺序反转死锁(旧 acquire_write 持 _write_lock 等 task
        # 锁,与 release_* 的 with _write_lock 互等)。
        with self._write_lock:
            lock = self._read_locks.get(task_id)
            if lock is None:
                lock = threading.RLock()
                self._read_locks[task_id] = lock
            self._lock_creation_time[task_id] = time.time()
            return lock

    def _acquire_guarded(self, lock: threading.RLock, task_id: str, lock_type: str) -> None:
        # 带超时获取(死锁检测兜底):写锁=RLock 重入×2 独占,读锁×1 共享。超时即回滚半获取
        # +告警+抛 LockAcquisitionError,不悬挂持有、不挂死。
        times = 2 if lock_type == "write" else 1
        acquired = _acquire_rlock_times(lock, times, self._acquire_timeout)
        if acquired >= times:
            return
        for _ in range(acquired):
            _release_lock_once(lock)
        _warn_lock_timeout(task_id, lock_type, self._acquire_timeout)
        raise LockAcquisitionError(task_id, lock_type)

    def acquire_read(self, task_id: str) -> None:
        if not self.enabled:
            return
        lock = self._get_read_lock(task_id)  # 字典访问已被 _write_lock 保护
        self._acquire_guarded(lock, task_id, "read")  # 在 _write_lock 外 acquire,不反转

    def release_read(self, task_id: str) -> None:
        if not self.enabled:
            return
        with self._write_lock:
            lock = self._read_locks.get(task_id)
        if lock is not None:  # release 在 _write_lock 外,与 acquire 锁顺序一致
            _release_lock_once(lock)

    def acquire_write(self, task_id: str) -> None:
        if not self.enabled:
            return
        lock = self._get_read_lock(task_id)
        self._acquire_guarded(lock, task_id, "write")  # RLock 重入×2=独占写锁

    def release_write(self, task_id: str) -> None:
        if not self.enabled:
            return
        with self._write_lock:
            lock = self._read_locks.get(task_id)
        if lock is not None:
            _release_lock_twice(lock)

    def release(self, task_id: str) -> None:
        self.release_read(task_id)

    def acquire(self, task_id: str, write: bool = False) -> None:
        if write:
            self.acquire_write(task_id)
        else:
            self.acquire_read(task_id)

    def with_read_lock(self, task_id: str, func: callable) -> any:
        self.acquire_read(task_id)
        try:
            return func()
        finally:
            self.release_read(task_id)

    def with_write_lock(self, task_id: str, func: callable) -> any:
        self.acquire_write(task_id)
        try:
            return func()
        finally:
            self.release_write(task_id)

    def cleanup(self) -> int:
        if not self.enabled:
            return 0
        with self._write_lock:
            now = time.time()
            to_remove = [
                task_id
                for task_id, create_time in self._lock_creation_time.items()
                if now - create_time > self._cleanup_timeout
                and task_id in self._read_locks
            ]

            for task_id in to_remove:
                self._read_locks.pop(task_id, None)
                self._lock_creation_time.pop(task_id, None)

            return len(to_remove)

    def get_active_locks(self) -> list[str]:
        with self._write_lock:
            return list(self._read_locks.keys())


# 全局锁管理器实例
_global_lock_manager: TaskLockManager | None = None


def _release_lock_once(lock: threading.RLock) -> None:
    try:
        lock.release()
    except RuntimeError:
        pass


def _release_lock_twice(lock: threading.RLock) -> None:
    try:
        lock.release()
        lock.release()
    except RuntimeError:
        pass


def _acquire_rlock_times(lock: threading.RLock, times: int, timeout: int) -> int:
    """带超时获取同一 RLock times 次(写锁=2 次重入独占);返回实际获取次数。
    某次超时即在该处停,调用方据返回值回滚已获取的次数,避免悬挂持有。"""
    acquired = 0
    for _ in range(times):
        if not lock.acquire(timeout=timeout):
            return acquired
        acquired += 1
    return acquired


def _warn_lock_timeout(task_id: str, lock_type: str, timeout: int) -> None:
    _LOG.warning(
        "task 锁疑似死锁/未释放:task=%s 类型=%s 等待超过 %ss 未获取——已放弃并抛错(不挂死)。",
        task_id, lock_type, timeout,
    )


def _lock_enabled(config: AgentConfig | None) -> bool:
    raw_value = getattr(config, "concurrency_lock_enabled", True)
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"false", "0"}:
            return False
        if normalized in {"true", "1"}:
            return True
        return True
    return bool(raw_value)


def _cleanup_timeout(config: AgentConfigType | None) -> int:
    raw_value = getattr(config, "task_lock_timeout_seconds", _DEFAULT_TASK_LOCK_TIMEOUT_SECONDS)
    try:
        timeout = int(raw_value)
    except (TypeError, ValueError):
        return _DEFAULT_TASK_LOCK_TIMEOUT_SECONDS
    return max(1, timeout)


def get_task_lock_manager(config: AgentConfig | None = None) -> TaskLockManager:
    global _global_lock_manager
    if _global_lock_manager is None:
        _global_lock_manager = TaskLockManager(config)
    return _global_lock_manager


__all__ = ["TaskLockManager", "get_task_lock_manager"]
