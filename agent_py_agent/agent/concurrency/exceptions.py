# LLM: Concurrency module; keep lock/conflict semantics stable around task mutations.
# 模块用途: 提供任务锁、乐观锁和冲突重试，保护并发写入。

from __future__ import annotations


# LLM: ConcurrencyConflictError is a 并发和冲突重试 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 表示 并发和冲突重试 的专用异常，让调用方能区分这类失败并给出清楚提示。
class ConcurrencyConflictError(Exception):

    # LLM: ConcurrencyConflictError.__init__ belongs to 并发和冲突重试; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 初始化实例依赖和字段，不应在构造阶段做难以回滚的重副作用；它是 ConcurrencyConflictError 的方法，通常依赖实例字段。
    def __init__(self, task_id: str, expected_version: int, actual_version: int):
        self.task_id = task_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"任务 {task_id} 并发冲突：期望版本 {expected_version}，实际版本 {actual_version}"
        )


# LLM: LockAcquisitionError is a 并发和冲突重试 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 表示 并发和冲突重试 的专用异常，让调用方能区分这类失败并给出清楚提示。
class LockAcquisitionError(Exception):

    # LLM: LockAcquisitionError.__init__ belongs to 并发和冲突重试; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 初始化实例依赖和字段，不应在构造阶段做难以回滚的重副作用；它是 LockAcquisitionError 的方法，通常依赖实例字段。
    def __init__(self, task_id: str, lock_type: str):
        self.task_id = task_id
        self.lock_type = lock_type
        super().__init__(f"无法获取任务 {task_id} 的 {lock_type} 锁")


# LLM: AuditLogError is a 并发和冲突重试 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 表示 并发和冲突重试 的专用异常，让调用方能区分这类失败并给出清楚提示。
class AuditLogError(Exception):

    pass


__all__ = [
    "ConcurrencyConflictError",
    "LockAcquisitionError",
    "AuditLogError",
]
