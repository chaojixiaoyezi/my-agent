"""并发控制异常。

定义并发相关的异常类型。
"""
from __future__ import annotations


class ConcurrencyConflictError(Exception):
    """乐观锁冲突异常。

    当多个终端同时修改同一任务，导致版本号不匹配时抛出。
    """

    def __init__(self, task_id: str, expected_version: int, actual_version: int):
        """初始化异常。

        Args:
            task_id: 任务 ID
            expected_version: 期望的版本号
            actual_version: 实际的版本号
        """
        self.task_id = task_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"任务 {task_id} 并发冲突：期望版本 {expected_version}，实际版本 {actual_version}"
        )


class LockAcquisitionError(Exception):
    """锁获取失败异常。

    当无法获取任务锁时抛出（通常是死锁或超时）。
    """

    def __init__(self, task_id: str, lock_type: str):
        """初始化异常。

        Args:
            task_id: 任务 ID
            lock_type: 锁类型（read/write）
        """
        self.task_id = task_id
        self.lock_type = lock_type
        super().__init__(f"无法获取任务 {task_id} 的 {lock_type} 锁")


class AuditLogError(Exception):
    """审计日志错误。

    当审计日志写入失败时抛出。
    """

    pass


__all__ = [
    "ConcurrencyConflictError",
    "LockAcquisitionError",
    "AuditLogError",
]
