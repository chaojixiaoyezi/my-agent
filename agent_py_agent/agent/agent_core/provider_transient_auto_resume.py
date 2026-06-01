# LLM: Provider transient auto-resume retries only the failed model turn, not the whole run.
# 模块用途: 模型网关 429/5xx/临时断连时按配置短暂等待并重试当前模型回合，避免重复执行已完成工具。

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from ..backends import is_provider_transient_error
from ..settings.runtime_guard_config import runtime_guard_data

_T = TypeVar("_T")

DEFAULT_PROVIDER_TRANSIENT_RETRY_DELAYS_SECONDS = (10.0, 25.0, 45.0, 100.0, 180.0)


# LLM: _RetryNotice bundles one provider retry notice for streaming and sleeping.
# 类用途: 保存本次重试序号、总次数、等待秒数和原始 provider 异常。
@dataclass(frozen=True)
class _RetryNotice:
    attempt: int
    total: int
    delay: float
    error: BaseException


# LLM: provider_transient_retry_delays is config-backed so ops can tune provider flake cadence.
# 函数用途: 读取 provider 临时错误自动续跑等待表；空列表表示不自动续跑。
def provider_transient_retry_delays() -> tuple[float, ...]:
    value = runtime_guard_data().get(
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


# LLM: _parsed_delays accepts open YAML numeric forms without failing the whole schedule.
# 函数用途: 将配置列表里的可解析项转成浮点秒数，跳过坏项。
def _parsed_delays(value: list | tuple) -> tuple[float, ...]:
    parsed: list[float] = []
    for item in value:
        try:
            parsed.append(float(item))
        except (TypeError, ValueError):
            continue
    return tuple(parsed)


# LLM: run_with_provider_transient_auto_resume wraps one model call and preserves tool-loop state.
# 函数用途: 对当前模型回合做 provider 临时错误重试；重试耗尽后原异常继续上抛给 CLI/父级恢复。
def run_with_provider_transient_auto_resume(
    operation: Callable[[], _T],
    *,
    on_chunk: Callable[[str], object] | None = None,
) -> _T:
    delays = provider_transient_retry_delays()
    for attempt, delay in enumerate(delays, start=1):
        try:
            return operation()
        except Exception as exc:
            _raise_unless_provider_transient(exc)
            _wait_before_retry(on_chunk, _RetryNotice(attempt, len(delays), delay, exc))
    return operation()


# LLM: _raise_unless_provider_transient limits auto-resume to typed provider flakes only.
# 函数用途: 非 provider 临时错误立即原样抛出，避免吞业务异常。
def _raise_unless_provider_transient(exc: Exception) -> None:
    if not is_provider_transient_error(exc):
        raise exc


# LLM: _wait_before_retry emits an operator-visible notice before the configured sleep.
# 函数用途: 输出自动续跑提示并按本次等待秒数暂停。
def _wait_before_retry(on_chunk: Callable[[str], object] | None, notice: _RetryNotice) -> None:
    _emit_retry_notice(on_chunk, notice)
    time.sleep(notice.delay)


# LLM: _emit_retry_notice makes provider auto-resume visible in tail logs.
# 函数用途: 将当前重试次数、等待时间和错误写到流式输出回调。
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


# LLM: _format_delay keeps retry notices readable for integer and decimal schedules.
# 函数用途: 把等待秒数渲染成短文本。
def _format_delay(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}".rstrip("0").rstrip(".")


__all__ = [
    "DEFAULT_PROVIDER_TRANSIENT_RETRY_DELAYS_SECONDS",
    "provider_transient_retry_delays",
    "run_with_provider_transient_auto_resume",
]
