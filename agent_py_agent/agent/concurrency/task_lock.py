# LLM: Concurrency module; keep lock/conflict semantics stable around task mutations.
# 模块用途: 提供任务锁、乐观锁和冲突重试，保护并发写入。

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..settings.config import AgentConfig


# LLM: TaskLockManager is a 并发和冲突重试 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 集中封装 并发和冲突重试 中和 TaskLockManager 相关的状态与行为，新增职责前先确认是否该拆到相邻服务。
class TaskLockManager:

    # LLM: TaskLockManager.__init__ belongs to 并发和冲突重试; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 初始化实例依赖和字段，不应在构造阶段做难以回滚的重副作用；它是 TaskLockManager 的方法，通常依赖实例字段。
    def __init__(self, config: AgentConfig | None = None):
        self.enabled = _lock_enabled(config)
        self._read_locks: dict[str, threading.RLock] = {}
        self._write_lock = threading.RLock()
        self._lock_creation_time: dict[str, float] = {}
        self._cleanup_timeout = _cleanup_timeout(config)

    # LLM: TaskLockManager._get_read_lock belongs to 并发和冲突重试; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 查询已有记录、索引或配置并返回给上层调用方，返回结构需要保持稳定；它是 TaskLockManager 的方法，通常依赖实例字段。
    def _get_read_lock(self, task_id: str) -> tuple[threading.RLock, float]:
        with self._write_lock:
            if task_id not in self._read_locks:
                self._read_locks[task_id] = threading.RLock()
                self._lock_creation_time[task_id] = time.time()
            else:
                self._lock_creation_time[task_id] = time.time()
            return self._read_locks[task_id], self._lock_creation_time[task_id]

    # LLM: TaskLockManager.acquire_read belongs to 并发和冲突重试; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 完成 并发和冲突重试 里的 acquire_read 步骤，保持现有返回值、异常和副作用语义；会读取实例字段。
    def acquire_read(self, task_id: str) -> None:
        if not self.enabled:
            return
        lock, _ = self._get_read_lock(task_id)
        lock.acquire()

    # LLM: TaskLockManager.release_read belongs to 并发和冲突重试; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 完成 并发和冲突重试 里的 release_read 步骤，保持现有返回值、异常和副作用语义；会读取实例字段。
    def release_read(self, task_id: str) -> None:
        if not self.enabled:
            return
        with self._write_lock:
            lock = self._read_locks.get(task_id)
            if lock is not None:
                _release_lock_once(lock)

    # LLM: TaskLockManager.acquire_write belongs to 并发和冲突重试; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 完成 并发和冲突重试 里的 acquire_write 步骤，保持现有返回值、异常和副作用语义；会读取实例字段。
    def acquire_write(self, task_id: str) -> None:
        if not self.enabled:
            return
        with self._write_lock:
            # 先获取读锁，再升级为写锁
            lock, _ = self._get_read_lock(task_id)
            lock.acquire()
            lock.acquire()  # 再获取一次，变成独占写锁

    # LLM: TaskLockManager.release_write belongs to 并发和冲突重试; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 完成 并发和冲突重试 里的 release_write 步骤，保持现有返回值、异常和副作用语义；会读取实例字段。
    def release_write(self, task_id: str) -> None:
        if not self.enabled:
            return
        with self._write_lock:
            lock = self._read_locks.get(task_id)
            if lock is not None:
                _release_lock_twice(lock)

    # LLM: TaskLockManager.release belongs to 并发和冲突重试; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 完成 并发和冲突重试 里的 release 步骤，保持现有返回值、异常和副作用语义；会读取实例字段。
    def release(self, task_id: str) -> None:
        self.release_read(task_id)

    # LLM: TaskLockManager.acquire belongs to 并发和冲突重试; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 完成 并发和冲突重试 里的 acquire 步骤，保持现有返回值、异常和副作用语义；会读取实例字段。
    def acquire(self, task_id: str, write: bool = False) -> None:
        if write:
            self.acquire_write(task_id)
        else:
            self.acquire_read(task_id)

    # LLM: TaskLockManager.with_read_lock belongs to 并发和冲突重试; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 完成 并发和冲突重试 里的 with_read_lock 步骤，保持现有返回值、异常和副作用语义；会读取实例字段。
    def with_read_lock(self, task_id: str, func: callable) -> any:
        self.acquire_read(task_id)
        try:
            return func()
        finally:
            self.release_read(task_id)

    # LLM: TaskLockManager.with_write_lock belongs to 并发和冲突重试; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 完成 并发和冲突重试 里的 with_write_lock 步骤，保持现有返回值、异常和副作用语义；会读取实例字段。
    def with_write_lock(self, task_id: str, func: callable) -> any:
        self.acquire_write(task_id)
        try:
            return func()
        finally:
            self.release_write(task_id)

    # LLM: TaskLockManager.cleanup belongs to 并发和冲突重试; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 完成 并发和冲突重试 里的 cleanup 步骤，保持现有返回值、异常和副作用语义；会读取实例字段。
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

    # LLM: TaskLockManager.get_active_locks belongs to 并发和冲突重试; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 查询已有记录、索引或配置并返回给上层调用方，返回结构需要保持稳定；它是 TaskLockManager 的方法，通常依赖实例字段。
    def get_active_locks(self) -> list[str]:
        with self._write_lock:
            return list(self._read_locks.keys())


# 全局锁管理器实例
_global_lock_manager: TaskLockManager | None = None


# LLM: _release_lock_once belongs to 并发和冲突重试; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 完成 并发和冲突重试 里的 _release_lock_once 步骤，保持现有返回值、异常和副作用语义。
def _release_lock_once(lock: threading.RLock) -> None:
    try:
        lock.release()
    except RuntimeError:
        pass


# LLM: _release_lock_twice belongs to 并发和冲突重试; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 完成 并发和冲突重试 里的 _release_lock_twice 步骤，保持现有返回值、异常和副作用语义。
def _release_lock_twice(lock: threading.RLock) -> None:
    try:
        lock.release()
        lock.release()
    except RuntimeError:
        pass


# LLM: _lock_enabled mirrors config normalization for small config stubs passed directly in tests or integrations.
# 函数用途: 判断任务锁是否开启；兼容未完整归一化的轻量配置对象。
def _lock_enabled(config: AgentConfig | None) -> bool:
    raw_value = getattr(config, "concurrency_lock_enabled", True)
    if isinstance(raw_value, str):
        return raw_value.strip().lower() not in {"false", "no", "off", "0"}
    return bool(raw_value)


# LLM: _cleanup_timeout makes task_lock_timeout_seconds the single knob for stale lock cleanup.
# 函数用途: 从配置读取任务锁清理超时；没有配置对象时保持旧的 300 秒默认。
def _cleanup_timeout(config: AgentConfig | None) -> int:
    raw_value = getattr(config, "task_lock_timeout_seconds", 300)
    try:
        timeout = int(raw_value)
    except (TypeError, ValueError):
        return 300
    return max(1, timeout)


# LLM: get_task_lock_manager belongs to 并发和冲突重试; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 查询已有记录、索引或配置并返回给上层调用方，返回结构需要保持稳定。
def get_task_lock_manager(config: AgentConfig | None = None) -> TaskLockManager:
    global _global_lock_manager
    if _global_lock_manager is None:
        _global_lock_manager = TaskLockManager(config)
    return _global_lock_manager


__all__ = ["TaskLockManager", "get_task_lock_manager"]
