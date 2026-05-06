from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..settings.config import AgentConfig


class TaskLockManager:

    def __init__(self, config: AgentConfig | None = None):
        self._read_locks: dict[str, threading.RLock] = {}
        self._write_lock = threading.RLock()
        self._lock_creation_time: dict[str, float] = {}
        self._cleanup_timeout = 300  # 5 分钟无访问则清理

    def _get_read_lock(self, task_id: str) -> tuple[threading.RLock, float]:
        with self._write_lock:
            if task_id not in self._read_locks:
                self._read_locks[task_id] = threading.RLock()
                self._lock_creation_time[task_id] = time.time()
            else:
                self._lock_creation_time[task_id] = time.time()
            return self._read_locks[task_id], self._lock_creation_time[task_id]

    def acquire_read(self, task_id: str) -> None:
        lock, _ = self._get_read_lock(task_id)
        lock.acquire()

    def release_read(self, task_id: str) -> None:
        with self._write_lock:
            if task_id in self._read_locks:
                lock = self._read_locks[task_id]
                try:
                    lock.release()
                except RuntimeError:
                    # 锁未持有
                    pass

    def acquire_write(self, task_id: str) -> None:
        with self._write_lock:
            # 先获取读锁，再升级为写锁
            lock, _ = self._get_read_lock(task_id)
            lock.acquire()
            lock.acquire()  # 再获取一次，变成独占写锁

    def release_write(self, task_id: str) -> None:
        with self._write_lock:
            if task_id in self._read_locks:
                lock = self._read_locks[task_id]
                try:
                    lock.release()
                    lock.release()
                except RuntimeError:
                    pass

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


def get_task_lock_manager() -> TaskLockManager:
    global _global_lock_manager
    if _global_lock_manager is None:
        _global_lock_manager = TaskLockManager()
    return _global_lock_manager


__all__ = ["TaskLockManager", "get_task_lock_manager"]
