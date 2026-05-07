# LLM: Retry decorator for optimistic-lock conflicts; keep ParamSpec forwarding transparent for wrapped callables.
# 模块用途: 给可能遇到 ConcurrencyConflictError 的写入流程提供轻量重试，不了解具体业务状态。

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import ParamSpec, TypeVar

from .exceptions import ConcurrencyConflictError

P = ParamSpec("P")
T = TypeVar("T")


# LLM: Public decorator factory for conflict-prone mutations; do not change wrapped-call signature behavior.
# 函数用途: 给会抛 ConcurrencyConflictError 的函数套一层重试，调用方的参数和返回值保持原样。
def retry_on_conflict(
    max_retries: int = 3,
    min_backoff: float = 0.1,
    max_backoff: float = 0.5,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    return _ConflictRetryPolicy(max_retries, min_backoff, max_backoff).decorator


# LLM: Immutable retry settings plus the wrapper builder; only ConcurrencyConflictError is retried.
# 类用途: 保存重试次数和退避范围，并负责把这些策略应用到一个目标函数上。
@dataclass(frozen=True)
class _ConflictRetryPolicy:
    max_retries: int
    min_backoff: float
    max_backoff: float

    # LLM: Builds the actual decorator while preserving metadata through functools.wraps.
    # 函数用途: 接收一个目标函数并返回带冲突重试能力的同签名包装函数。
    def decorator(self, func: Callable[P, T]) -> Callable[P, T]:
        # LLM: Transparent forwarding boundary; ParamSpec is the intentional bundle-rule exception for decorators.
        # 函数用途: 把调用参数原样交给重试执行器，外部仍像调用原函数一样使用。
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            # Decorator boundary: ParamSpec preserves the wrapped callable's own
            # explicit signature, so this is the narrow transparent-forwarding
            # exception rather than product code unpacking arbitrary options.
            return self.run(func, *args, **kwargs)

        return wrapper

    # LLM: Executes the wrapped callable and retries only optimistic-lock conflicts until the policy is exhausted.
    # 函数用途: 反复执行目标函数，遇到并发冲突就短暂等待后重试，最后一次失败会抛回原异常。
    def run(self, func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        last_error: ConcurrencyConflictError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return func(*args, **kwargs)
            except ConcurrencyConflictError as exc:
                last_error = exc
                self._sleep_before_retry(attempt)
        if last_error is not None:
            raise last_error
        raise ConcurrencyConflictError(task_id="unknown", expected_version=0, actual_version=0)

    # LLM: Applies randomized backoff between attempts and skips sleeping after the final attempt.
    # 函数用途: 在下一次重试前等待一个随机短间隔，减少多个写入者同时再次冲突。
    def _sleep_before_retry(self, attempt: int) -> None:
        if attempt >= self.max_retries:
            return
        time.sleep(random.uniform(self.min_backoff, self.max_backoff))


__all__ = ["retry_on_conflict"]
