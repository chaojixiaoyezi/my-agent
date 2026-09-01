"""批3 错误分类器钉子(长期助手 24 类取 9 类,2-1 收口)。

钉死契约:typed 错误优先于文本特征;rate_limit/overloaded/server_error/timeout
可重试;auth/billing/format 不可重试(白试浪费);context_overflow 标记压缩
(交 ptl 链);unknown 保守不重试;transient_resume 经分类器扩展重试面。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.backends.errors import (
    ProviderContextWindowError,
    ProviderQuotaExhaustedError,
    ProviderTransientError,
)
from agent_py_agent.agent.contracts.provider_error_classifier import (
    ProviderFailureReason,
    classify_provider_error,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "text,reason,retryable",
    [
        ("HTTP 429 Too Many Requests", ProviderFailureReason.RATE_LIMIT, True),
        ("503 Service overloaded", ProviderFailureReason.OVERLOADED, True),
        ("Internal Server Error 500", ProviderFailureReason.SERVER_ERROR, True),
        ("request timed out after 240s", ProviderFailureReason.TIMEOUT, True),
        ("401 Unauthorized: invalid api key", ProviderFailureReason.AUTH, False),
        ("402 insufficient_quota", ProviderFailureReason.BILLING, False),
        ("400 Bad Request: invalid request body", ProviderFailureReason.FORMAT_ERROR, False),
        ("something exploded mysteriously", ProviderFailureReason.UNKNOWN, False),
    ],
)
def test_text_classification(text: str, reason: ProviderFailureReason, retryable: bool) -> None:
    result = classify_provider_error(RuntimeError(text))
    assert result.reason is reason
    assert result.retryable is retryable


def test_http_status_wins_over_dated_identifiers_inside_provider_message() -> None:
    """真实回归：web_search_20250305 中的 503 不是 provider overload。"""

    error = RuntimeError(
        "HTTP 400: invalid request: tools[0] unknown variant custom; "
        "expected web_search_20250305 or web_search_20260209"
    )

    result = classify_provider_error(error)

    assert result.status_code == 400
    assert result.reason is ProviderFailureReason.FORMAT_ERROR
    assert result.retryable is False


def test_typed_errors_take_priority() -> None:
    overflow = classify_provider_error(ProviderContextWindowError("prompt too large"))
    assert overflow.reason is ProviderFailureReason.CONTEXT_OVERFLOW
    assert overflow.should_compress is True and overflow.retryable is False
    transient = classify_provider_error(ProviderTransientError("weird wording no codes"))
    assert transient.retryable is True, "typed transient 无文本特征也按可重试"
    quota = classify_provider_error(ProviderQuotaExhaustedError("HTTP 429: rate_limit_error"))
    assert quota.reason is ProviderFailureReason.BILLING
    assert quota.retryable is False


def test_resume_chain_retries_classified_rate_limit() -> None:
    from agent_py_agent.agent.agent_core.provider_transient_auto_resume import (
        _raise_unless_provider_transient,
    )

    _raise_unless_provider_transient(RuntimeError("429 rate limit"))  # 不抛=进入重试
    with pytest.raises(RuntimeError):
        _raise_unless_provider_transient(RuntimeError("401 invalid api key"))


def test_retry_delays_get_jitter_but_stay_config_driven() -> None:
    import random

    from agent_py_agent.agent.concurrency.retry import apply_retry_jitter

    random.seed(7)
    samples = [apply_retry_jitter(10.0) for _ in range(50)]
    assert all(10.0 <= s <= 15.0 for s in samples), "抖动范围 [delay, delay*1.5]"
    assert len({round(s, 6) for s in samples}) > 1, "确实在抖,不是常数"
    assert apply_retry_jitter(0.0) == 0.0


def test_typed_timeout_retry_uses_structured_stage() -> None:
    """流式超时按 会话运行时 语义重连；墙钟和旧超时不得被文本误判为无限重试。"""
    import pytest

    from agent_py_agent.agent.agent_core.provider_transient_auto_resume import (
        _raise_unless_provider_transient,
    )
    from agent_py_agent.agent.backends.errors import ProviderTimeoutError

    _raise_unless_provider_transient(
        ProviderTimeoutError("等待首事件超时", stage="first_event")
    )
    _raise_unless_provider_transient(
        ProviderTimeoutError("流式响应空闲超时", stage="stream_idle")
    )
    for stage in ("wall_clock", "provider_declared", "provider_wall"):
        with pytest.raises(ProviderTimeoutError):
            _raise_unless_provider_transient(
                ProviderTimeoutError("模型接口请求超时", stage=stage)
            )
    # 裸异常(无 typed 形态)仍走文本分类器:rate_limit 放行重试
    _raise_unless_provider_transient(RuntimeError("429 rate limit"))
