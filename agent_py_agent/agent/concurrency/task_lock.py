
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from ..settings.defaults import default_config_int

if TYPE_CHECKING:
    from ..settings.config import AgentConfig as AgentConfigType


_DEFAULT_TASK_LOCK_TIMEOUT_SECONDS = default_config_int("task_lock_timeout_seconds")


class TaskLockManager:

    def __init__(self, config: AgentConfigType | None = None):
        self.enabled = _lock_enabled(config)
        self._read_locks: dict[str, threading.RLock] = {}
        self._write_lock = threading.RLock()
        self._lock_creation_time: dict[str, float] = {}
        self._cleanup_timeout = _cleanup_timeout(config)

    def _get_read_lock(self, task_id: str) -> tuple[threading.RLock, float]:
        with self._write_lock:
            if task_id not in self._read_locks:
                self._read_locks[task_id] = threading.RLock()
                self._lock_creation_time[task_id] = time.time()
            else:
                self._lock_creation_time[task_id] = time.time()
            return self._read_locks[task_id], self._lock_creation_time[task_id]

    def acquire_read(self, task_id: str) -> None:
        if not self.enabled:
            return
        lock, _ = self._get_read_lock(task_id)
        lock.acquire()

    def release_read(self, task_id: str) -> None:
        if not self.enabled:
            return
        with self._write_lock:
            lock = self._read_locks.get(task_id)
            if lock is not None:
                _release_lock_once(lock)

    def acquire_write(self, task_id: str) -> None:
        if not self.enabled:
            return
        with self._write_lock:
            # 先获取读锁，再升级为写锁
            lock, _ = self._get_read_lock(task_id)
            lock.acquire()
            lock.acquire()  # 再获取一次，变成独占写锁

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
