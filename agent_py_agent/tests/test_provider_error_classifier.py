"""批3 错误分类器钉子(长期助手 24 类取 9 类,2-1 收口)。

钉死契约:typed 错误优先于文本特征;rate_limit/overloaded/server_error/timeout
可重试;auth/billing/format 不可重试(白试浪费);context_overflow 标记压缩
(交 ptl 链);unknown 保守不重试;transient_resume 经分类器扩展重试面。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.backends.errors import ProviderContextWindowError, ProviderTransientError
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


def test_typed_errors_take_priority() -> None:
    overflow = classify_provider_error(ProviderContextWindowError("prompt too large"))
    assert overflow.reason is ProviderFailureReason.CONTEXT_OVERFLOW
    assert overflow.should_compress is True and overflow.retryable is False
    transient = classify_provider_error(ProviderTransientError("weird wording no codes"))
    assert transient.retryable is True, "typed transient 无文本特征也按可重试"


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


def test_typed_timeout_error_fast_fails_not_retried() -> None:
    """CI 回归钉子:typed ProviderTimeoutError 必须快速上抛,不被文本分类器
    误判 TIMEOUT/retryable 进入重试循环(my-agent 的 request_timeout 是回合
    超时,重试每次同样超时,只拖垮续航)。文本分类器只兜非 typed 裸异常。"""
    import pytest

    from agent_py_agent.agent.agent_core.provider_transient_auto_resume import (
        _raise_unless_provider_transient,
    )
    from agent_py_agent.agent.backends.errors import ProviderTimeoutError

    with pytest.raises(ProviderTimeoutError):
        _raise_unless_provider_transient(ProviderTimeoutError("模型接口请求超时: request_timeout=17s"))
    # 裸异常(无 typed 形态)仍走文本分类器:rate_limit 放行重试
    _raise_unless_provider_transient(RuntimeError("429 rate limit"))
