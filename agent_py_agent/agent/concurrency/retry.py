
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
