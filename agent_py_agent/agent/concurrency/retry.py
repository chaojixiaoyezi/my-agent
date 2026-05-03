"""重试装饰器。

用于处理乐观锁冲突时的自动重试。
"""
from __future__ import annotations

import random
import time
from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, TypeVar

from .exceptions import ConcurrencyConflictError

if TYPE_CHECKING:
    pass

T = TypeVar("T")


def retry_on_conflict(
    max_retries: int = 3,
    min_backoff: float = 0.1,
    max_backoff: float = 0.5,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """重试装饰器。

    当函数抛出 ConcurrencyConflictError 时，自动重试：
    - 重试最多 max_retries 次
    - 重试间加随机退避，避免惊群效应
    - 退避时间在 [min_backoff, max_backoff] 范围内

    Args:
        max_retries: 最大重试次数
        min_backoff: 最小退避时间（秒）
        max_backoff: 最大退避时间（秒）

    Returns:
        装饰器函数
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_error: ConcurrencyConflictError | None = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except ConcurrencyConflictError as e:
                    last_error = e
                    if attempt < max_retries:
                        # 随机退避
                        backoff = random.uniform(min_backoff, max_backoff)
                        time.sleep(backoff)
                    # 如果不是 ConcurrencyConflictError，继续抛出

            # 所有重试都失败，抛出最后一次错误
            if last_error is not None:
                raise last_error
            # 理论上不会到这里
            raise ConcurrencyConflictError(
                task_id="unknown",
                expected_version=0,
                actual_version=0,
            )

        return wrapper
    return decorator


__all__ = ["retry_on_conflict"]
