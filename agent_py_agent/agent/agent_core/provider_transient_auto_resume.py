
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from ..backends import is_provider_transient_error
from ..concurrency.retry import apply_retry_jitter
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
            # 配置阶梯+随机抖动(批3):多实例同撞限流时错峰重试,防共振雪崩。
            _wait_before_retry(on_chunk, _RetryNotice(attempt, len(delays), apply_retry_jitter(delay), exc))
    return operation()


# LLM: 可重试判定升级(批3 2-1 收口):typed transient 之外,经分类器
#   (contracts/provider_error_classifier,长期助手 蓝本)判为 rate_limit/
#   overloaded/server_error/timeout 的裸异常同样进入重试;auth/billing/format/
#   context_overflow/unknown 照旧上抛(context_overflow 由上层 ptl_retry 链
#   接手压缩,unknown 保守快速浮出)。模型全程无感。
# 函数用途: 这个错值不值得原地重试?值得就放行去等待,不值得立刻抛给上层。
def _raise_unless_provider_transient(exc: Exception) -> None:
    if is_provider_transient_error(exc):
        return
    from ..contracts.provider_error_classifier import classify_provider_error

    if classify_provider_error(exc).retryable:
        return
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
