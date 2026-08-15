
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


def retry_on_conflict(
    max_retries: int = 3,
    min_backoff: float = 0.1,
    max_backoff: float = 0.5,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    return _ConflictRetryPolicy(max_retries, min_backoff, max_backoff).decorator


@dataclass(frozen=True)
class _ConflictRetryPolicy:
    max_retries: int
    min_backoff: float
    max_backoff: float

    def decorator(self, func: Callable[P, T]) -> Callable[P, T]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            # Decorator boundary: ParamSpec preserves the wrapped callable's own
            # explicit signature, so this is the narrow transparent-forwarding
            # exception rather than product code unpacking arbitrary options.
            return self.run(func, *args, **kwargs)

        return wrapper

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

    def _sleep_before_retry(self, attempt: int) -> None:
        if attempt >= self.max_retries:
            return
        time.sleep(random.uniform(self.min_backoff, self.max_backoff))


__all__ = ["retry_on_conflict"]


# LLM: jittered 指数退避(参数原样:
#   base 5s/上限 120s/抖动 0-50%)。分布式友好:多 worker 同时撞限频时抖动
#   错峰,避免雪崩式同步重试。纯函数,provider 重试链与未来一切退避统一用它。
# 函数用途: 第 N 次重试该睡多久——指数翻倍封顶两分钟,再加一点随机错峰。
def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    delay = min(base_delay * (2 ** max(0, attempt - 1)), max_delay)
    return apply_retry_jitter(delay, jitter_ratio=jitter_ratio)


# 函数用途: 给一个既定延迟叠加 0~ratio 的随机抖动(配置阶梯保持可调,
#   抖动只负责错峰——provider 重试链等"延迟序列来自配置"的场景用这个)。
def apply_retry_jitter(delay: float, *, jitter_ratio: float = 0.5) -> float:
    base = max(0.0, float(delay))
    return base + random.uniform(0, base * max(0.0, jitter_ratio))
