from __future__ import annotations

"""LLM: implements outbound HTTP tools behind explicit timeout and output-size limits.

给人看的解释：
这个文件只负责访问网络。
`fetch_url` 偏向'简单打开一个网页'，`http_request` 偏向'调接口、带 header、带 body'。
这里统一限制超时时间和返回长度，避免一次请求把主流程卡死或把 prompt 撑爆。
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .models import BaseTool, ToolExecutionResult, ToolSpec

_MAX_URL_CHARS = 4096
_MAX_BODY_CHARS = 1_000_000
_MAX_HEADER_JSON_CHARS = 65536
_MAX_HEADER_COUNT = 100
_MAX_HEADER_NAME_CHARS = 128
_MAX_HEADER_VALUE_CHARS = 8192
_MAX_METHOD_CHARS = 16
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HTTP_METHOD_RE = re.compile(r"^[A-Z][A-Z0-9_-]*$")


def _has_control_chars(text: str) -> bool:
    return any(ord(char) < 32 for char in text)


def _scalar_text(value: Any, *, name: str, max_chars: int, allow_empty: bool = False) -> str:
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


def _normalize_url(value: Any) -> str:
    url = _scalar_text(value, name="url", max_chars=_MAX_URL_CHARS)
    if _has_control_chars(url):
        raise ValueError("url 包含不支持的控制字符")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"}:
        raise ValueError("url 只支持 http 或 https")
    if not parts.netloc or not parts.hostname:
        raise ValueError("url 必须包含有效主机名")
    if parts.username is not None or parts.password is not None:
        raise ValueError("url 不支持携带用户名或密码")
    return url


def _normalize_method(value: Any) -> str:
    method = _scalar_text(value, name="method", max_chars=_MAX_METHOD_CHARS).upper()
    if not _HTTP_METHOD_RE.fullmatch(method):
        raise ValueError("method 必须是有效的 HTTP 方法名")
    return method


def _format_response(tool: str, status: int, headers: Any, body: str, max_chars: int) -> ToolExecutionResult:
    result = (
        f"status={status}\n"
        f"content_type={headers.get('Content-Type', '')}\n\n"
        f"{body[:max_chars]}"
    )
    if len(body) > max_chars:
        result += "\n... 已截断"
    return ToolExecutionResult(tool, True, result)


def _format_http_error(tool: str, exc: urllib.error.HTTPError, max_chars: int) -> ToolExecutionResult:
    detail = exc.read(max_chars + 1).decode("utf-8", "replace")
    result = (
        f"HTTP {exc.code}\n"
        f"content_type={exc.headers.get('Content-Type', '')}\n\n"
        f"{detail[:max_chars]}"
    )
    if len(detail) > max_chars:
        result += "\n... 已截断"
    return ToolExecutionResult(tool, False, result)


class FetchUrlTool(BaseTool):
    """抓取网页或纯文本接口内容。"""

    def __init__(self, *, max_chars: int, timeout: int):
        self.max_chars = max_chars
        self.timeout = timeout
        self.spec = ToolSpec(
            name="fetch_url",
            category="web",
            description="抓取网页或文本接口内容，适合查在线文档、网页说明和纯文本页面。",
            use_cases=[
                "查看在线文档、普通网页正文或文本接口响应",
                "快速确认某个 URL 是否能访问、返回了什么文本",
            ],
            avoid_when=[
                "需要带复杂请求头、请求体或切换 HTTP 方法时，优先用 http_request",
            ],
            keywords=["网页", "抓网页", "文档", "URL", "fetch", "GET", "在线说明"],
            parameters={
                "url": "完整 URL",
            },
            parameter_details={
                "url": "必填，传入完整的 http 或 https 地址；工具内部固定按 GET 请求处理。",
            },
            examples=[
                '{"tool": "fetch_url", "url": "https://example.com/docs"}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            url = _normalize_url(params.get("url"))
        except ValueError as exc:
            return ToolExecutionResult("fetch_url", False, str(exc))

        req = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": "SimplePythonAgent/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                return _format_response("fetch_url", resp.status, resp.headers, body, self.max_chars)
        except urllib.error.HTTPError as exc:
            return _format_http_error("fetch_url", exc, self.max_chars)
        except (urllib.error.URLError, TimeoutError) as exc:
            return ToolExecutionResult("fetch_url", False, f"请求失败: {exc.__class__.__name__}")


class HttpRequestTool(BaseTool):
    """通用 HTTP / API 调试工具。"""

    def __init__(self, *, max_chars: int, timeout: int):
        self.max_chars = max_chars
        self.timeout = timeout
        self.spec = ToolSpec(
            name="http_request",
            category="api",
            description="发送通用 HTTP 请求，适合调 REST API、Webhook 和普通接口。",
            use_cases=[
                "测试 GET/POST/PUT/DELETE 等接口返回是否正常",
                "带请求头、请求体去联调 API",
            ],
            avoid_when=[
                "只是想看一个普通网页正文时，fetch_url 更简单",
            ],
            keywords=["API", "接口", "HTTP", "POST", "GET", "Webhook", "请求头", "请求体"],
            parameters={
                "url": "完整 URL",
                "method": "HTTP 方法，默认 GET",
                "headers": "可选请求头",
                "body": "可选请求体",
            },
            parameter_details={
                "url": "必填，接口完整地址。",
                "method": "可选，支持 GET/POST/PUT/DELETE 等；默认是 GET。",
                "headers": "可传 JSON 对象或 JSON 字符串，常用于 Content-Type、Authorization 等。",
                "body": "可选，请求体会按 utf-8 文本发送；适合传 JSON 字符串或普通文本。",
            },
            examples=[
                '{"tool": "http_request", "url": "https://example.com/health"}',
                '{"tool": "http_request", "url": "https://example.com/api", "method": "POST", "headers": {"Content-Type": "application/json"}, "body": "{\\"name\\": \\"demo\\"}"}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            url = _normalize_url(params.get("url"))
            method = _normalize_method(params.get("method", "GET"))
            headers = self._normalize_headers(params.get("headers"))
            body = params.get("body")
            body_text = None if body is None else _scalar_text(
                body,
                name="body",
                max_chars=_MAX_BODY_CHARS,
                allow_empty=True,
            )
        except ValueError as exc:
            return ToolExecutionResult("http_request", False, str(exc))
        data = None if body_text is None else body_text.encode("utf-8")

        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                response_body = resp.read().decode("utf-8", "replace")
                return _format_response("http_request", resp.status, resp.headers, response_body, self.max_chars)
        except urllib.error.HTTPError as exc:
            return _format_http_error("http_request", exc, self.max_chars)
        except (urllib.error.URLError, TimeoutError) as exc:
            return ToolExecutionResult("http_request", False, f"请求失败: {exc.__class__.__name__}")

    def _normalize_headers(self, headers: Any) -> dict[str, str]:
        """把请求头统一整理成 `dict[str, str]`。"""

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
