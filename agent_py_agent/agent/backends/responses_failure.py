# LLM: Responses 流（SSE 与 WebSocket 共用 responses_wire.collect_response）里的 error / response.failed 事件，只按服务商给的
#   结构化错误对象（code/type/param/message）分类，与 HTTP 错误共用 gateway_helpers 的上下文超限、硬额度判定，不按模型名或地址分支。
#   分类决定恢复路线：上下文超限 → ProviderContextWindowError（主循环走既有压缩恢复）；硬额度 → ProviderQuotaExhaustedError；
#   限流 → ProviderUsageLimitError；服务端繁忙/内部错误 → ProviderTransientError（既有瞬时自动续跑）；其余认不出的错误码一律
#   保留原错误码的 ProviderResponseError（MODEL_RESPONSE_FAILED），已知码表只是优化，不会因为不在表里就丢原因。
#   改动须同步 test_responses_failure.py 与 contracts/error_taxonomy.py 的 MODEL_RESPONSE_FAILED。
# 模块用途: 把服务商的“失败事件”变成带原因、能走对恢复路线的结构化异常，不再一律报“返回失败事件”。
from __future__ import annotations

import json

from .errors import (
    ProviderContextWindowError,
    ProviderQuotaExhaustedError,
    ProviderResponseError,
    ProviderTransientError,
    ProviderUsageLimitError,
)
from .gateway_helpers import (
    _provider_error_indicates_context_window,
    _provider_error_indicates_quota_exhausted,
)

FAILED_EVENT_ERROR_CODE = "MODEL_RESPONSE_FAILED"
# 已知的限流与服务端临时错误码（OpenAI/ChatGPT Responses 公开写法）；不在表里的码照样保留原文，只是不自动重试。
_RATE_LIMIT_CODES = frozenset({"rate_limit_exceeded", "rate_limit_error", "too_many_requests"})
_TRANSIENT_CODES = frozenset({
    "server_error", "internal_error", "internal_server_error", "server_is_overloaded", "overloaded",
    "overloaded_error", "service_unavailable", "slow_down", "timeout",
})
# 诊断里只留错误对象的这几个字段，并限长；不带请求正文或回复正文。
_KEPT_FIELDS = {"code": 64, "type": 64, "param": 128, "message": 500}


# LLM: response.failed 的错误在 response.error；error 事件的错误在 error 对象或顶层 code/message/param（顶层 type 恒为 "error"，
#   不当作错误码）。字段只留白名单并限长，缺失时返回空字典。纯函数。
# 函数用途: 从失败事件里取出服务商给的错误对象。
def failed_event_provider_error(event: dict) -> dict[str, str]:
    if event.get("type") == "response.failed":
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        raw = response.get("error")
    elif isinstance(event.get("error"), dict):
        raw = event["error"]
    else:
        raw = {key: event.get(key) for key in ("code", "message", "param")}
    if not isinstance(raw, dict):
        return {}
    return {key: str(raw[key])[:limit] for key, limit in _KEPT_FIELDS.items() if raw.get(key) not in (None, "")}


# LLM: 先判上下文超限与硬额度（与 HTTP 同一判定函数），再按错误码判限流和临时错误，其余保留原码；返回异常而不抛出，
#   由调用方 raise。用户可见文字只带错误码，服务商原始消息只进 details 供诊断。
# 函数用途: 把一个 error / response.failed 事件换成对应的结构化供应商异常。
def failed_event_error(event: dict) -> Exception:
    provider_error = failed_event_provider_error(event)
    code = (provider_error.get("code") or provider_error.get("type") or "").strip().lower()
    detail = json.dumps(provider_error, ensure_ascii=False, sort_keys=True)
    message = f"Responses 服务返回失败事件（错误码 {code}）。" if code else "Responses 服务返回失败事件（服务商没有给出错误码）。"
    details = {"event_type": str(event.get("type") or ""), "provider_error": provider_error}
    if provider_error and _provider_error_indicates_context_window(detail):
        return ProviderContextWindowError(message, details=details)
    if provider_error and _provider_error_indicates_quota_exhausted(detail):
        return ProviderQuotaExhaustedError(message, details=details)
    if code in _RATE_LIMIT_CODES:
        return ProviderUsageLimitError(message)
    if code in _TRANSIENT_CODES:
        return ProviderTransientError(message)
    return ProviderResponseError(message, error_code=FAILED_EVENT_ERROR_CODE, details=details)


__all__ = ["FAILED_EVENT_ERROR_CODE", "failed_event_error", "failed_event_provider_error"]
