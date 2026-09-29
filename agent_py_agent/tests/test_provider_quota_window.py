"""429 按供应商声明的限额窗口区分「额度用完」和「临时限流」。

真机 2026-09-29：主模型套餐每周额度用完，供应商返回 429，错误体里只有
error.type=GoUsageLimitError 和 metadata.limitName=weekly，不命中硬额度错误码表，
被当成按分钟的临时限流，回合内重试约 20 分钟、回合外还按供应退避反复重试。
"""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest

from agent_py_agent.agent.backends.errors import (
    ProviderQuotaExhaustedError,
    ProviderTransientError,
    ProviderUsageLimitError,
)
from agent_py_agent.agent.backends.gateway_helpers import (
    GatewayRequest,
    _runtime_http_error,
    post_json,
)

# 真实错误体的脱敏版：workspace 换成占位值，其余键和值与生产日志里的结构化事件一致。
WEEKLY_LIMIT_BODY = {
    "type": "error",
    "error": {"type": "GoUsageLimitError", "message": "Go usage limit exceeded"},
    "metadata": {"workspace": "wrk_redacted", "limitName": "weekly"},
}
MINUTE_LIMIT_BODY = {"error": {"type": "rate_limit_error", "message": "rate limited"}}


def _request() -> GatewayRequest:
    return GatewayRequest(
        api_base="https://api.example.com",
        api_key="test-key",
        path="/v1/chat",
        payload={},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )


def _http_429(body: dict, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.example.com",
        429,
        "Too Many Requests",
        {"Content-Type": "application/json", **(headers or {})},
        BytesIO(json.dumps(body).encode()),
    )


@patch("agent_py_agent.agent.backends.gateway_helpers._provider_retry_wait")
@patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
def test_weekly_limit_body_is_quota_exhausted_without_transport_retry(mock_urlopen, mock_wait):
    mock_urlopen.side_effect = _http_429(WEEKLY_LIMIT_BODY)

    with pytest.raises(ProviderQuotaExhaustedError) as exc_info:
        post_json(_request())

    assert exc_info.value.error_code == "PROVIDER_QUOTA_EXHAUSTED"
    assert exc_info.value.details["provider_error"]["metadata"]["limitName"] == "weekly"
    assert mock_urlopen.call_count == 1
    mock_wait.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {"metadata": {"limitName": "daily"}},
        {"metadata": {"limit_name": "monthly"}},
        {"error": {"type": "rate_limit_error", "window": "5h"}},
        {"error": {"limit_window": "5_hour"}},
        {"window": "7d"},
        {"limits": [{"quota_period": "requests_per_day"}]},
    ],
)
def test_long_declared_windows_are_quota_exhausted(body):
    assert isinstance(_runtime_http_error(_http_429(body)), ProviderQuotaExhaustedError)


@pytest.mark.parametrize(
    "body",
    [
        MINUTE_LIMIT_BODY,
        {"metadata": {"limitName": "minute"}},
        {"metadata": {"limitName": "hourly"}},
        {"window": "1m"},
        # 单字母单位不带数字不算窗口（否则 "d" 会被当成一天）。
        {"window": "d"},
        # 认不出的写法保持瞬时默认，不能「不在表里就判额度用完」。
        {"metadata": {"limitName": "burst"}},
        # 同时声明多个窗口时分不清撞的是哪一个，按最短的算。
        {"limits": [{"window": "minute"}, {"window": "weekly"}]},
        # 时间词出现在非限额键上不参与判定。
        {"error": {"type": "rate_limit_error"}, "billing_period": "monthly"},
    ],
)
def test_short_or_unknown_windows_stay_transient_usage_limit(body):
    assert type(_runtime_http_error(_http_429(body))) is ProviderUsageLimitError


@patch("agent_py_agent.agent.backends.gateway_helpers._provider_retry_wait")
@patch("agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen")
def test_minute_limit_with_short_retry_after_still_retries(mock_urlopen, mock_wait):
    mock_urlopen.side_effect = [_http_429(MINUTE_LIMIT_BODY, {"Retry-After": "3"}) for _ in range(4)]

    with pytest.raises(ProviderUsageLimitError):
        post_json(_request())

    assert mock_urlopen.call_count == 4
    assert [call.args[0] for call in mock_wait.call_args_list] == [3.0, 3.0, 3.0]


# 回合级自动续跑只重试瞬时类：长窗口额度用完第一次就上抛，不再等 10/25/45/100/180 秒。
@patch("agent_py_agent.agent.agent_core.provider_transient_auto_resume.wait_interruptibly")
def test_turn_auto_resume_does_not_wait_on_weekly_limit(mock_wait):
    from agent_py_agent.agent.agent_core.provider_transient_auto_resume import (
        run_with_provider_transient_auto_resume,
    )

    calls = []

    def weekly_limited_call():
        calls.append(1)
        raise _runtime_http_error(_http_429(WEEKLY_LIMIT_BODY))

    with pytest.raises(ProviderQuotaExhaustedError):
        run_with_provider_transient_auto_resume(weekly_limited_call)

    assert len(calls) == 1
    mock_wait.assert_not_called()


@patch("agent_py_agent.agent.agent_core.provider_transient_auto_resume.wait_interruptibly")
def test_turn_auto_resume_still_retries_minute_limit(mock_wait):
    from agent_py_agent.agent.agent_core.provider_transient_auto_resume import (
        provider_transient_retry_delays,
        run_with_provider_transient_auto_resume,
    )

    calls = []

    def minute_limited_call():
        calls.append(1)
        raise _runtime_http_error(_http_429(MINUTE_LIMIT_BODY))

    with pytest.raises(ProviderTransientError):
        run_with_provider_transient_auto_resume(minute_limited_call)

    assert len(calls) == len(provider_transient_retry_delays()) + 1
    assert mock_wait.call_count == len(provider_transient_retry_delays())
