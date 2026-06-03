
from __future__ import annotations


class ConcurrencyConflictError(Exception):

    def __init__(self, task_id: str, expected_version: int, actual_version: int):
        self.task_id = task_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"任务 {task_id} 并发冲突：期望版本 {expected_version}，实际版本 {actual_version}"
        )


class LockAcquisitionError(Exception):

    def __init__(self, task_id: str, lock_type: str):
        self.task_id = task_id
        self.lock_type = lock_type
        super().__init__(f"无法获取任务 {task_id} 的 {lock_type} 锁")


class AuditLogError(Exception):

    pass


__all__ = [
    "ConcurrencyConflictError",
    "LockAcquisitionError",
    "AuditLogError",
]
