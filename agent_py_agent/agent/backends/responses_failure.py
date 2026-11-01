# LLM: Responses 流（SSE 与 WebSocket 共用 responses_wire.collect_response）里的 error / response.failed 事件，只按服务商给的
#   结构化错误对象（code/type/param/message）分类，与 HTTP 错误共用 gateway_helpers 的上下文超限、硬额度判定，不按模型名或地址分支。
#   分类决定恢复路线：上下文超限 → ProviderContextWindowError（主循环走既有压缩恢复）；硬额度 → ProviderQuotaExhaustedError；
#   限流 → ProviderUsageLimitError；服务端繁忙/内部错误 → ProviderTransientError（既有瞬时自动续跑）；其余认不出的错误码一律
#   保留原错误码的 ProviderResponseError（MODEL_RESPONSE_FAILED），已知码表只是优化，不会因为不在表里就丢原因。
#   改动须同步 test_responses_failure.py 与 contracts/error_taxonomy.py 的 MODEL_RESPONSE_FAILED。
# 模块用途: 把服务商的“失败事件”变成带原因、能走对恢复路线的结构化异常，不再一律报“返回失败事件”。
from __future__ import annotations

import json

from ..contracts.diagnostics import redact_diagnostic_text
from .errors import (
    ProviderContextWindowError,
    ProviderQuotaExhaustedError,
    ProviderResponseError,
    ProviderTransientError,
    ProviderUsageLimitError,
)
from .gateway_helpers import (
    _HARD_QUOTA_ERROR_CODES,
    _provider_error_indicates_context_window,
    _provider_error_indicates_quota_exhausted,
)

FAILED_EVENT_ERROR_CODE = "MODEL_RESPONSE_FAILED"
# 已知的限流与服务端临时错误码（OpenAI/ChatGPT Responses 公开写法）；结构化码优先于消息全文判定，
# 不在表里的码照样保留原文，只是不自动重试。上下文超限码（context_length_exceeded）由全文判定直接识别。
_RATE_LIMIT_CODES = frozenset({"rate_limit_exceeded", "rate_limit_error", "too_many_requests"})
_TRANSIENT_CODES = frozenset({
    "server_error", "internal_error", "internal_server_error", "server_is_overloaded", "overloaded",
    "overloaded_error", "service_unavailable", "slow_down", "timeout",
    "websocket_connection_limit_reached",
})
# 诊断里只留错误对象的这几个字段，并限长；不带请求正文或回复正文。
_KEPT_FIELDS = {"code": 64, "type": 64, "param": 128, "message": 500, "request_id": 128}


# LLM: response.failed 的错误在 response.error；error 事件的错误在 error 对象或顶层 code/message/param（顶层 type 恒为 "error"，
#   不当作错误码）。错误写成字符串时当作 message 保留（error 事件再并上顶层 code/param）。字段只留白名单并限长，缺失时返回空字典。纯函数。
# 函数用途: 从失败事件里取出服务商给的错误对象。
def failed_event_provider_error(event: dict) -> dict[str, str]:
    return _provider_error_projection(event)[0]


def _provider_error_projection(event: dict) -> tuple[dict[str, str], bool]:
    """错误正文与截断事实在同一次提取中生成；公开提取器仍保持原字典签名。"""
    top = {key: event.get(key) for key in ("code", "message", "param")}
    if event.get("type") == "response.failed":
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        raw, top = response.get("error"), {}
    else:
        raw = event.get("error") or top
    if isinstance(raw, str):
        raw = {**top, "message": raw}
    if not isinstance(raw, dict):
        return {}, raw is not None
    # 先脱敏再裁剪，不能将秘密截成无法识别的半截，也不能执行扩展对象的 __str__。
    result = {}
    changed = len(raw) != sum(key in raw for key in _KEPT_FIELDS)
    for key, limit in _KEPT_FIELDS.items():
        value = raw.get(key)
        if type(value) is str and value:
            result[key] = redact_diagnostic_text(value).encode('utf-8', errors='replace')[:limit].decode('utf-8', errors='ignore')
            changed = changed or result[key] != value
        elif key == 'code' and type(value) is int and value.bit_length() <= 64:
            result[key] = str(value)
        elif key in raw and value is not None and type(value) is not str:
            changed = True
    request_id = event.get('request_id')
    if 'request_id' not in result and type(request_id) is str:
        result['request_id'] = redact_diagnostic_text(request_id).encode('utf-8', errors='replace')[:128].decode('utf-8', errors='ignore')
        changed = changed or result['request_id'] != request_id
    return result, changed


# LLM: 结构化错误码优先（_error_for_code），认不出码时才用与 HTTP 同一组全文判定兜底上下文超限与硬额度，其余保留原码；
#   这样“限流消息里提到 token limit”不会被当成上下文超限。返回异常而不抛出，由调用方 raise。
#   CLI 沿原异常文本显示相同的有界脱敏字段；JSON 转义控制字符，完整正文与请求参数不进入提示。
# 函数用途: 把一个 error / response.failed 事件换成对应的结构化供应商异常。
def failed_event_error(event: dict) -> Exception:
    provider_error, truncated = _provider_error_projection(event)
    code = (provider_error.get("code") or provider_error.get("type") or "").strip().lower()
    detail = json.dumps(provider_error, ensure_ascii=False, sort_keys=True)
    message = f"Responses 服务返回失败事件（错误码 {code}）。" if code else "Responses 服务返回失败事件（服务商没有给出错误码）。"
    if provider_error:
        message += f' 诊断：{detail}'
    details = {"event_type": str(event.get("type") or ""), "provider_error": provider_error}
    details['details_truncated'] = truncated
    by_code = _error_for_code(code, message, details)
    if by_code is not None:
        return by_code
    if provider_error and _provider_error_indicates_context_window(detail):
        return ProviderContextWindowError(message, details=details)
    if provider_error and _provider_error_indicates_quota_exhausted(detail):
        return ProviderQuotaExhaustedError(message, details=details)
    return ProviderResponseError(message, error_code=FAILED_EVENT_ERROR_CODE, details=details)


# LLM: 只按已知的结构化错误码（已小写）选恢复路线；认不出返回 None，交调用方做全文兜底。纯函数。
# 函数用途: 已知错误码直接对应额度用完、限流或临时故障（上下文超限码由全文判定识别）。
def _error_for_code(code: str, message: str, details: dict) -> Exception | None:
    if code in _HARD_QUOTA_ERROR_CODES:
        return ProviderQuotaExhaustedError(message, details=details)
    if code in _RATE_LIMIT_CODES:
        return ProviderUsageLimitError(message, details=details)
    if code in _TRANSIENT_CODES:
        return ProviderTransientError(message, details=details)
    return None


__all__ = ["FAILED_EVENT_ERROR_CODE", "failed_event_error", "failed_event_provider_error"]
