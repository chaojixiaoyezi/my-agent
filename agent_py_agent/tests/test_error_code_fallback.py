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


def test_host_actor_rejections_never_recommend_retry_or_change_owner() -> None:
    for code in (
        "CHANNEL_ACTOR_IDENTITY_REQUIRED", "CHANNEL_ACTOR_IDENTITY_INVALID",
        "CHANNEL_ACTOR_IDENTITY_CONFLICT", "CHANNEL_ACTOR_SCOPE_MISMATCH",
        "SCHEDULER_NOT_AUTHORIZED",
    ):
        contract = ERROR_CONTRACTS[code]
        assert contract.category == "permission"
        assert not contract.retryable
        assert contract.recommended_action == "report_blocker"


def test_receipt_identity_errors_do_not_automatically_replay_business_work() -> None:
    for code in (
        "MODEL_RESPONSE_PROVENANCE_MISSING", "MODEL_RESPONSE_PROVENANCE_MISMATCH",
        "MODEL_RESPONSE_PURPOSE_MISMATCH", "BACKGROUND_RECEIPT_CONTRACT_INVALID",
        "BACKGROUND_RECEIPT_PUBLIC_OBLIGATION", "BACKGROUND_RECEIPT_PUBLIC_UNAVAILABLE",
    ):
        assert not ERROR_CONTRACTS[code].retryable
        assert ERROR_CONTRACTS[code].recommended_action in {"report_blocker", "change_strategy"}


def test_provider_quota_exhausted_keeps_its_registered_code() -> None:
    # obsfix34b 验证：quota 异常自带 error_code，fallback 第一步原样保留——不是靠类型映射；
    # 若未来去掉该属性，需要补类型映射并让本用例继续通过。
    exc = ProviderQuotaExhaustedError("quota gone")
    assert exc.error_code == "PROVIDER_QUOTA_EXHAUSTED"
    assert fallback_error_code(exc) == "PROVIDER_QUOTA_EXHAUSTED"
    assert ERROR_CONTRACTS["PROVIDER_QUOTA_EXHAUSTED"].retryable is False
