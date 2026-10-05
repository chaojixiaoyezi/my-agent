"""兜底错误码映射：无码异常必须落到 error_taxonomy 登记码，两处调用点共用同一份。

背景（obsfix34 问题4）：请求失败的 error_code 曾出现 PROVIDERTRANSIENTERROR 这类
“异常类名大写”的未登记码；本文件钉住 runtime_errors.fallback_error_code 的
四类映射、未知兜底与“已有 error_code 原样保留”。
"""
from __future__ import annotations

from agent_py_agent.agent.backends.errors import (
    ProviderConnectionError,
    ProviderQuotaExhaustedError,
    ProviderStreamIncompleteError,
    ProviderTimeoutError,
    ProviderTransientError,
    ProviderUsageLimitError,
)
from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS
from agent_py_agent.agent.runtime_db.operations import RuntimeConflictError
from agent_py_agent.agent.runtime_errors import fallback_error_code

_FALLBACK_CODES = (
    "PROVIDER_TIMEOUT",
    "PROVIDER_QUOTA_EXHAUSTED",
    "TRANSIENT_ERROR",
    "RUNTIME_CONFLICT",
    "UNKNOWN_ERROR",
)


def test_provider_timeout_maps_to_registered_timeout_code() -> None:
    assert fallback_error_code(ProviderTimeoutError("首事件等待超时")) == "PROVIDER_TIMEOUT"


def test_provider_transient_family_maps_to_transient_error() -> None:
    for exc in (
        ProviderTransientError("overloaded"),
        ProviderStreamIncompleteError("stream ended before completion"),
        ProviderUsageLimitError("rate limited"),
    ):
        assert fallback_error_code(exc) == "TRANSIENT_ERROR"


def test_runtime_conflict_maps_to_runtime_conflict() -> None:
    assert fallback_error_code(RuntimeConflictError("CAS 失败")) == "RUNTIME_CONFLICT"


def test_unknown_exception_maps_to_unknown_error() -> None:
    assert fallback_error_code(ValueError("bad input")) == "UNKNOWN_ERROR"


def test_existing_error_code_is_preserved() -> None:
    # ProviderConnectionError 自带 error_code（类属性），必须原样保留、不走类型映射。
    assert fallback_error_code(ProviderConnectionError("no route")) == "PROVIDER_CONNECTION_FAILED"

    class _CodedError(RuntimeError):
        error_code = "CUSTOM_CODE"

    assert fallback_error_code(_CodedError("x")) == "CUSTOM_CODE"


def test_mapped_codes_are_registered_in_taxonomy() -> None:
    for code in _FALLBACK_CODES:
        assert code in ERROR_CONTRACTS, code


def test_provider_quota_exhausted_keeps_its_registered_code() -> None:
    # obsfix34b 验证：quota 异常自带 error_code，fallback 第一步原样保留——不是靠类型映射；
    # 若未来去掉该属性，需要补类型映射并让本用例继续通过。
    exc = ProviderQuotaExhaustedError("quota gone")
    assert exc.error_code == "PROVIDER_QUOTA_EXHAUSTED"
    assert fallback_error_code(exc) == "PROVIDER_QUOTA_EXHAUSTED"
    assert ERROR_CONTRACTS["PROVIDER_QUOTA_EXHAUSTED"].retryable is False
