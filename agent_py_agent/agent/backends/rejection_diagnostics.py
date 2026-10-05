# LLM: 供应商拒绝诊断只从 typed HTTP 事实构造：状态码、白名单响应头、响应体安全摘要。
#   绝不记录请求头、cookie、完整响应头、认证信息或正文原文；白名单之外的头一律不记（见下方常量）。
#   判定只按状态码和白名单头，不解析自然语言，不猜测服务端语义。
# 模块用途: 让 4xx/5xx 拒绝在下一次能直接分清限流/额度/鉴权/内容拦截，而不是“原因未定”。
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# 响应头白名单：只有这些头名（或前缀）允许进诊断。带注释说明用途；不认识的头一律不记。
_REJECTION_HEADER_ALLOWLIST = frozenset({
    "x-request-id",       # 供应商请求编号：支持工单对账
    "cf-ray",             # Cloudflare 边缘请求编号（订阅端点常见）
    "retry-after",        # 限流后建议等待秒数
    "www-authenticate",   # 鉴权方案（只取方案名，见 _safe_header_value）
})
# 前缀白名单：限流窗口事实（x-ratelimit-limit / -remaining / -reset 等）。
_REJECTION_HEADER_PREFIXES = ("x-ratelimit-",)

# 响应体摘要上限：只保留开头若干字符，避免把整段服务端正文带进账本或展示。
_BODY_EXCERPT_CHARS = 200
# 单个头值上限；超长截断（头值可能被中间层塞入长内容）。
_HEADER_VALUE_CHARS = 120
# C0/C1 控制字符（含 NUL、换行）：先替换成空格再折叠，防止把控制序列带进账本或展示。
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# LLM: 折叠 = 控制字符替换成空格、再按空白切分合并；这是所有头值与正文摘要共用的唯一去控制字符口径。
# 函数用途: 把任意文本整理成单行、无控制字符的安全字符串。
def _collapse_text(value: object) -> str:
    return " ".join(_CONTROL_CHARS.sub(" ", str(value or "")).split())

# cause_code 词表：只按状态码判定，四个值对应用户可行动的方向；其它状态码不猜（空串）。
_CAUSE_BY_STATUS = {429: "rate_limited", 402: "quota", 401: "auth", 403: "forbidden_unknown"}
_REJECTION_CAUSE_CODES = frozenset(_CAUSE_BY_STATUS.values())


# LLM: 判定只读整数状态码，不读响应体、不解析语言；未知状态码返回空串表示“原因未定”，不伪造分类。
# 函数用途: 由 HTTP 状态码给出可行动的原因码（rate_limited/quota/auth/forbidden_unknown）。
def rejection_cause_code(status_code: int) -> str:
    return _CAUSE_BY_STATUS.get(int(status_code or 0), "")


# LLM: 头值先折叠（去控制字符）再截断；www-authenticate 只留方案名，避免把 realm/token 参数带出。
# 函数用途: 把一个白名单头值整理成安全、有界的字符串。
def _safe_header_value(name: str, value: object) -> str:
    text = _collapse_text(value)
    if name == "www-authenticate":
        return text.split(" ", 1)[0][:_HEADER_VALUE_CHARS]
    return text[:_HEADER_VALUE_CHARS]


# LLM: 兼容 email.message.Message 与普通映射（两者都有 items()）；大小写不敏感、前缀匹配；不引入别的来源。
# 函数用途: 从响应头里挑出白名单内的安全子集（名字小写、值已折叠截断）。
def rejection_headers(headers: object) -> dict[str, str]:
    items = headers.items() if callable(getattr(headers, "items", None)) else ()
    selected: dict[str, str] = {}
    for raw_name, raw_value in items:
        name = str(raw_name or "").strip().lower()
        if name in _REJECTION_HEADER_ALLOWLIST or name.startswith(_REJECTION_HEADER_PREFIXES):
            selected[name] = _safe_header_value(name, raw_value)
    return selected


# LLM: 只按名字取原始头值（大小写不敏感），给 content-type 这类专用字段用；不放进白名单集合。
# 函数用途: 从响应头里取一个指定头名的原始值；缺失返回空串。
def _raw_header(headers: object, wanted: str) -> str:
    items = headers.items() if callable(getattr(headers, "items", None)) else ()
    for raw_name, raw_value in items:
        if str(raw_name or "").strip().lower() == wanted:
            return str(raw_value or "")
    return ""


# LLM: 响应体只做安全摘要：折叠空白去控制字符、截断；body_bytes 按 UTF-8 重编码长度计（坏字节已在解码时替换）。
#   不记录正文以外的推断，也不解析语言。
# 函数用途: 构造一次供应商拒绝的结构化诊断（状态码、内容类型、体量、摘要、白名单头、原因码）。
def provider_rejection_diagnostic(
    *,
    status_code: int,
    headers: object,
    body: str,
) -> dict[str, Any]:
    text = str(body or "")
    return {
        "status_code": int(status_code or 0),
        "content_type": _safe_header_value("content-type", _raw_header(headers, "content-type")),
        "body_bytes": len(text.encode("utf-8", "replace")),
        "body_excerpt": _collapse_text(text)[:_BODY_EXCERPT_CHARS],
        "headers": rejection_headers(headers),
        "cause_code": rejection_cause_code(status_code),
    }


# LLM: 投影/持久化共用清洗：只保留白名单字段与合法类型，头集合再过一次白名单；坏值不报错、直接丢弃。
#   状态码不合法时整体返回空（没有 typed 事实就不展示诊断）。
# 函数用途: 把一份可能来自持久化或异常 details 的诊断清洗成可安全展示的子集。
def public_rejection_diagnostic(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    status = value.get("status_code")
    if type(status) is not int or not (100 <= status <= 599):
        return {}
    body_bytes = value.get("body_bytes")
    result: dict[str, Any] = {
        "status_code": status,
        "content_type": str(value.get("content_type") or "")[:80],
        "body_bytes": body_bytes if type(body_bytes) is int and body_bytes >= 0 else 0,
        "body_excerpt": _collapse_text(value.get("body_excerpt") or "")[:_BODY_EXCERPT_CHARS],
        "headers": rejection_headers(value.get("headers") or {}),
    }
    cause = str(value.get("cause_code") or "")
    if cause in _REJECTION_CAUSE_CODES:
        result["cause_code"] = cause
    return result
