
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .models import ToolHandlerOutcome

MAX_BODY_CHARS = 1_000_000
_MAX_HEADER_JSON_CHARS = 65536
_MAX_HEADER_COUNT = 100
_MAX_HEADER_NAME_CHARS = 128
_MAX_HEADER_VALUE_CHARS = 8192
_MAX_METHOD_CHARS = 16
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HTTP_METHOD_RE = re.compile(r"^[A-Z][A-Z0-9_-]*$")
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class HttpRequestParts:
    url: str
    method: str
    headers: dict[str, str]
    body_text: str | None
    max_chars: int


def scalar_text(value: Any, *, name: str, max_chars: int, allow_empty: bool = False) -> str:
    if value is None:
        raise ValueError(f"缺少必填参数 {name}")
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"{name} 参数必须是字符串或标量文本")
    text = str(value).strip() if name in {"url", "method"} else str(value)
    if not allow_empty and text == "":
        raise ValueError(f"{name} 不能为空")
    if len(text) > max_chars:
        raise ValueError(f"{name} 过长，最多 {max_chars} 个字符")
    return text


def normalize_method(value: Any) -> str:
    method = scalar_text(value, name="method", max_chars=_MAX_METHOD_CHARS).upper()
    if not _HTTP_METHOD_RE.fullmatch(method):
        raise ValueError("method 必须是有效的 HTTP 方法名")
    return method


def normalize_headers(headers: Any) -> dict[str, str]:
    if headers is None:
        return {"User-Agent": "SimplePythonAgent/1.0"}
    if isinstance(headers, dict):
        normalized = _normalize_header_dict(headers)
        normalized.setdefault("User-Agent", "SimplePythonAgent/1.0")
        return normalized
    if isinstance(headers, str):
        if len(headers) > _MAX_HEADER_JSON_CHARS:
            raise ValueError(f"headers 过长，最多 {_MAX_HEADER_JSON_CHARS} 个字符")
        try:
            parsed = json.loads(headers)
        except json.JSONDecodeError as exc:
            raise ValueError("headers 必须是 JSON 对象字符串") from exc
        if not isinstance(parsed, dict):
            raise ValueError("headers 字符串解析后必须是 JSON 对象")
        normalized = _normalize_header_dict(parsed)
        normalized.setdefault("User-Agent", "SimplePythonAgent/1.0")
        return normalized
    raise ValueError("headers 必须为空、对象或 JSON 字符串")


def attach_http_advisory(result: ToolHandlerOutcome, request: HttpRequestParts) -> None:
    effect = "mutating" if request.method in _MUTATING_METHODS else "read_only"
    advisories = ["idempotency_key"] if effect == "mutating" and not _has_idempotency_key(request.headers) else []
    result.result_envelope.update({
        "http_effect": effect,
        "method": request.method,
        "advisories": advisories,
        "advisory_details": {
            "idempotency_key": "变更类请求建议提供幂等键，便于网络重试时避免重复副作用。"
        } if advisories else {},
    })


def _normalize_header_dict(headers: dict[Any, Any]) -> dict[str, str]:
    if len(headers) > _MAX_HEADER_COUNT:
        raise ValueError(f"headers 字段过多，最多 {_MAX_HEADER_COUNT} 个")
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, (str, int, float, bool)):
            raise ValueError("headers 名称必须是字符串或标量")
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError("headers 值必须是字符串或标量")
        name = str(key).strip()
        header_value = str(value)
        if not name:
            raise ValueError("headers 包含空名称")
        if len(name) > _MAX_HEADER_NAME_CHARS:
            raise ValueError(f"headers 名称过长，最多 {_MAX_HEADER_NAME_CHARS} 个字符")
        if len(header_value) > _MAX_HEADER_VALUE_CHARS:
            raise ValueError(f"headers 值过长，最多 {_MAX_HEADER_VALUE_CHARS} 个字符")
        if not _HEADER_NAME_RE.fullmatch(name):
            raise ValueError("headers 名称包含非法字符")
        if "\r" in header_value or "\n" in header_value:
            raise ValueError("headers 值不能包含换行")
        normalized[name] = header_value
    return normalized


def _has_idempotency_key(headers: dict[str, str]) -> bool:
    return any(key.lower() in {"idempotency-key", "x-idempotency-key"} for key in headers)
