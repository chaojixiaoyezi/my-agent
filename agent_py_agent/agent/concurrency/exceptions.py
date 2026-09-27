
from __future__ import annotations


class ConcurrencyConflictError(Exception):

    def __init__(self, task_id: str, expected_version: int, actual_version: int):
        self.task_id = task_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"任务 {task_id} 并发冲突：期望版本 {expected_version}，实际版本 {actual_version}"
        )


class AuditLogError(Exception):

    pass


__all__ = [
    "ConcurrencyConflictError",
    "AuditLogError",
]
