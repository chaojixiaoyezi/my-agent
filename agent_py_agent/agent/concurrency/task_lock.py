"""任务级读写锁。

基于 threading.Lock 的任务级并发控制：
- 按 task_id 粒度加锁
- 共享读锁 / 独占写锁
- 自动清理不再使用的锁
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..settings.config import AgentConfig


class TaskLockManager:
    """任务锁管理器。

    使用 threading.Lock 实现任务级读写锁：
    - read_lock: 共享读锁，多个读操作可以并发
    - write_lock: 独占写锁，写操作独占
    """

    def __init__(self, config: AgentConfig | None = None):
        """初始化任务锁管理器。

        Args:
            config: 智能体配置对象（可选，用于未来配置）
        """
        self._read_locks: dict[str, threading.RLock] = {}
        self._write_lock = threading.RLock()
        self._lock_creation_time: dict[str, float] = {}
        self._cleanup_timeout = 300  # 5 分钟无访问则清理

    def _get_read_lock(self, task_id: str) -> tuple[threading.RLock, float]:
        """获取或创建读锁。

        Args:
            task_id: 任务 ID

        Returns:
            (读锁对象, 创建时间)
        """
        with self._write_lock:
            if task_id not in self._read_locks:
                self._read_locks[task_id] = threading.RLock()
                self._lock_creation_time[task_id] = time.time()
            else:
                self._lock_creation_time[task_id] = time.time()
            return self._read_locks[task_id], self._lock_creation_time[task_id]

    def acquire_read(self, task_id: str) -> None:
        """获取读锁。

        Args:
            task_id: 任务 ID
        """
        lock, _ = self._get_read_lock(task_id)
        lock.acquire()

    def release_read(self, task_id: str) -> None:
        """释放读锁。

        Args:
            task_id: 任务 ID
        """
        with self._write_lock:
            if task_id in self._read_locks:
                lock = self._read_locks[task_id]
                try:
                    lock.release()
                except RuntimeError:
                    # 锁未持有
                    pass

    def acquire_write(self, task_id: str) -> None:
        """获取写锁。

        Args:
            task_id: 任务 ID
        """
        with self._write_lock:
            # 先获取读锁，再升级为写锁
            lock, _ = self._get_read_lock(task_id)
            lock.acquire()
            lock.acquire()  # 再获取一次，变成独占写锁

    def release_write(self, task_id: str) -> None:
        """释放写锁。

        Args:
            task_id: 任务 ID
        """
        with self._write_lock:
            if task_id in self._read_locks:
                lock = self._read_locks[task_id]
                try:
                    lock.release()
                    lock.release()
                except RuntimeError:
                    pass

    def release(self, task_id: str) -> None:
        """释放锁（通用）。

        尝试释放读锁和写锁。

        Args:
            task_id: 任务 ID
        """
        self.release_read(task_id)

    def acquire(self, task_id: str, write: bool = False) -> None:
        """获取锁。

        Args:
            task_id: 任务 ID
            write: 是否获取写锁
        """
        if write:
            self.acquire_write(task_id)
        else:
            self.acquire_read(task_id)

    def with_read_lock(self, task_id: str, func: callable) -> any:
        """在读锁保护下执行函数。

        Args:
            task_id: 任务 ID
            func: 要执行的函数

        Returns:
            函数返回值
        """
        self.acquire_read(task_id)
        try:
            return func()
        finally:
            self.release_read(task_id)

    def with_write_lock(self, task_id: str, func: callable) -> any:
        """在写锁保护下执行函数。

        Args:
            task_id: 任务 ID
            func: 要执行的函数

        Returns:
            函数返回值
        """
        self.acquire_write(task_id)
        try:
            return func()
        finally:
            self.release_write(task_id)

    def cleanup(self) -> int:
        """清理不再使用的锁。

        清理创建时间超过 cleanup_timeout 且当前未被持有的锁。

        Returns:
            清理的锁数量
        """
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
        """获取当前所有锁的任务 ID。

        Returns:
            任务 ID 列表
        """
        with self._write_lock:
            return list(self._read_locks.keys())


# 全局锁管理器实例
_global_lock_manager: TaskLockManager | None = None


def get_task_lock_manager() -> TaskLockManager:
    """获取全局任务锁管理器。

    Returns:
        全局 TaskLockManager 实例
    """
    global _global_lock_manager
    if _global_lock_manager is None:
        _global_lock_manager = TaskLockManager()
    return _global_lock_manager


__all__ = ["TaskLockManager", "get_task_lock_manager"]
