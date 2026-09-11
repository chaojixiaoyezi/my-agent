# LLM: provider 错误分类器(批3 对照移植,长期助手 error_classifier 24 类取 9 类;
#   2-1 收口)。契约:①输入任意异常,输出 ClassifiedProviderError(reason +
#   恢复动作标志),决策全在框架层模型无感;②不推倒既有 typed 错误
#   (backends/errors 的 ProviderTransientError/ContextWindowError 等是事实源,
#   分类器在其上做语义归并 + HTTP 状态码/文本特征兜底);③动作映射:
#   rate_limit/overloaded/server_error/timeout → retryable(jittered backoff),
#   context_overflow → should_compress(交既有 ptl_retry 链),auth/billing/
#   format/unknown → 不自动重试(快速浮出,unknown 保守)。改动时同步检查
#   provider_transient_auto_resume 接线与 tests/test_provider_error_classifier.py。
# 模块用途: 模型接口出错时,框架先分清"这是哪种错、该怎么救",而不是一律
#   重试或一律崩——限频就退避、上下文爆就压缩、没权限就别白试。
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from ..backends.errors import (
    ProviderConfigurationError,
    is_provider_context_window_error,
    is_provider_quota_exhausted_error,
    is_provider_transient_error,
    provider_error_http_status,
)


class ProviderFailureReason(str, Enum):
    """九类失败原因(长期助手 24 类的高频子集,开放扩展)。"""

    AUTH = "auth"
    BILLING = "billing"
    RATE_LIMIT = "rate_limit"
    OVERLOADED = "overloaded"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    CONTEXT_OVERFLOW = "context_overflow"
    FORMAT_ERROR = "format_error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassifiedProviderError:
    """分类结果:原因 + 框架该采取的恢复动作标志。"""

    reason: ProviderFailureReason
    retryable: bool
    should_compress: bool = False
    status_code: int | None = None
    message: str = ""


# 文本特征兜底表(typed 错误优先;此表只兜没有 typed 形态的裸异常)。
_TEXT_RULES: tuple[tuple[ProviderFailureReason, re.Pattern[str]], ...] = (
    (ProviderFailureReason.AUTH, re.compile(r"\b(?:401|403)\b|unauthorized|invalid[ _]api[ _]key|authentication", re.I)),
    (ProviderFailureReason.BILLING, re.compile(r"\b402\b|insufficient[ _]quota|billing|balance", re.I)),
    (ProviderFailureReason.RATE_LIMIT, re.compile(r"\b429\b|rate[ _-]?limit|too many requests", re.I)),
    (ProviderFailureReason.OVERLOADED, re.compile(r"\b(?:503|529)\b|overloaded|capacity", re.I)),
    (ProviderFailureReason.SERVER_ERROR, re.compile(r"\b50[024]\b|internal server error|bad gateway", re.I)),
    (ProviderFailureReason.TIMEOUT, re.compile(r"time[d]?[ _-]?out|deadline", re.I)),
    (ProviderFailureReason.CONTEXT_OVERFLOW, re.compile(r"context[ _]length|maximum context|too long|prompt is too large", re.I)),
    (ProviderFailureReason.FORMAT_ERROR, re.compile(r"\b400\b|bad request|invalid request", re.I)),
)

_RETRYABLE = frozenset(
    {
        ProviderFailureReason.RATE_LIMIT,
        ProviderFailureReason.OVERLOADED,
        ProviderFailureReason.SERVER_ERROR,
        ProviderFailureReason.TIMEOUT,
    }
)


# LLM: 分类唯一入口。优先级:typed 错误和 status_code > 裸异常文本 > unknown；配置/请求拒绝绝不被正文升级为重试或压缩。
#   unknown 保守不重试(掩盖真实故障比多失败一次更糟)。
# 函数用途: 给一个 provider 异常定性:什么错、能不能重试、要不要先压缩。
def classify_provider_error(exc: BaseException) -> ClassifiedProviderError:
    message = str(exc or "")[:500]
    if isinstance(exc, ProviderConfigurationError):
        status = provider_error_http_status(exc)
        return ClassifiedProviderError(
            _reason_from_status(status) or ProviderFailureReason.UNKNOWN,
            retryable=False,
            status_code=status,
            message=message,
        )
    if is_provider_quota_exhausted_error(exc):
        return ClassifiedProviderError(
            ProviderFailureReason.BILLING, retryable=False, status_code=_status_code(message), message=message
        )
    if is_provider_context_window_error(exc):
        return ClassifiedProviderError(
            ProviderFailureReason.CONTEXT_OVERFLOW, retryable=False, should_compress=True, message=message
        )
    status_code = _status_code(message)
    reason = _reason_from_status(status_code) or _reason_from_text(message)
    if reason is None and is_provider_transient_error(exc):
        # typed transient 但文本无特征:按服务端临时故障处理(可重试)。
        reason = ProviderFailureReason.SERVER_ERROR
    if reason is None:
        reason = ProviderFailureReason.UNKNOWN
    return ClassifiedProviderError(
        reason,
        retryable=reason in _RETRYABLE,
        should_compress=reason is ProviderFailureReason.CONTEXT_OVERFLOW,
        status_code=status_code,
        message=message,
    )


def _reason_from_status(status_code: int | None) -> ProviderFailureReason | None:
    """Prefer the provider HTTP status over unrelated numbers inside its message.

    Provider payloads can contain dated API identifiers such as
    ``web_search_20250305``.  Substring matching ``503`` inside that identifier
    must never turn a leading HTTP 400 into a retryable overload.
    """

    if status_code in {401, 403}:
        return ProviderFailureReason.AUTH
    if status_code == 402:
        return ProviderFailureReason.BILLING
    if status_code == 429:
        return ProviderFailureReason.RATE_LIMIT
    if status_code in {503, 529}:
        return ProviderFailureReason.OVERLOADED
    if status_code in {500, 502, 504}:
        return ProviderFailureReason.SERVER_ERROR
    if status_code == 408:
        return ProviderFailureReason.TIMEOUT
    if status_code in {400, 413, 422}:
        return ProviderFailureReason.FORMAT_ERROR
    return None


# 函数用途: 按特征表给异常文本找第一类匹配(没有返回 None)。
def _reason_from_text(message: str) -> ProviderFailureReason | None:
    for reason, pattern in _TEXT_RULES:
        if pattern.search(message):
            return reason
    return None


# 函数用途: 从文本里抠 HTTP 状态码(供观测,不参与决策)。
def _status_code(message: str) -> int | None:
    match = re.search(r"\bHTTP\s+([45]\d{2})\b", message, re.I)
    if match is None:
        match = re.search(r"\b([45]\d{2})\b", message)
    return int(match.group(1)) if match else None


__all__ = [
    "ClassifiedProviderError",
    "ProviderFailureReason",
    "classify_provider_error",
]
