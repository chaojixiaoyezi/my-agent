
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from ..backends import is_provider_transient_error
from ..settings.runtime_guard_config import RuntimeGuardPolicy, runtime_guard_data

_T = TypeVar("_T")

DEFAULT_PROVIDER_TRANSIENT_RETRY_DELAYS_SECONDS = (10.0, 25.0, 45.0, 100.0, 180.0)


@dataclass(frozen=True)
class _RetryNotice:
    attempt: int
    total: int
    delay: float
    error: BaseException


def provider_transient_retry_delays(policy: RuntimeGuardPolicy | None = None) -> tuple[float, ...]:
    value = runtime_guard_data(policy=policy).get(
        "provider_transient_auto_resume_delays_seconds",
        DEFAULT_PROVIDER_TRANSIENT_RETRY_DELAYS_SECONDS,
    )
    if not isinstance(value, list | tuple):
        value = DEFAULT_PROVIDER_TRANSIENT_RETRY_DELAYS_SECONDS
    return tuple(
        value
        for value in _parsed_delays(value)
        if value > 0
    )


def _parsed_delays(value: list | tuple) -> tuple[float, ...]:
    parsed: list[float] = []
    for item in value:
        try:
            parsed.append(float(item))
        except (TypeError, ValueError):
            continue
    return tuple(parsed)


def run_with_provider_transient_auto_resume(
    operation: Callable[[], _T],
    *,
    on_chunk: Callable[[str], object] | None = None,
    policy: RuntimeGuardPolicy | None = None,
) -> _T:
    delays = provider_transient_retry_delays(policy)
    for attempt, delay in enumerate(delays, start=1):
        try:
            return operation()
        except Exception as exc:
            _raise_unless_provider_transient(exc)
            _wait_before_retry(on_chunk, _RetryNotice(attempt, len(delays), delay, exc))
    return operation()


def _raise_unless_provider_transient(exc: Exception) -> None:
    if not is_provider_transient_error(exc):
        raise exc


def _wait_before_retry(on_chunk: Callable[[str], object] | None, notice: _RetryNotice) -> None:
    _emit_retry_notice(on_chunk, notice)
    time.sleep(notice.delay)


def _emit_retry_notice(on_chunk: Callable[[str], object] | None, notice: _RetryNotice) -> None:
    if not callable(on_chunk):
        return
    delay = _format_delay(notice.delay)
    on_chunk(
        "\n"
        f"[provider_transient_auto_resume attempt={notice.attempt}/{notice.total}; wait_seconds={delay}]\n"
        f"模型接口临时不可用或被限流，等待 {delay} 秒后自动重试当前模型回合。\n"
        f"error={notice.error}\n"
    )


def _format_delay(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}".rstrip("0").rstrip(".")


__all__ = [
    "DEFAULT_PROVIDER_TRANSIENT_RETRY_DELAYS_SECONDS",
    "provider_transient_retry_delays",
    "run_with_provider_transient_auto_resume",
]
