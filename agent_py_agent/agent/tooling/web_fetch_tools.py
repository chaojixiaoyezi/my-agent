
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import BaseTool, ToolExecutionResult, ToolSpec
from .web_fetch_runtime import (
    FetchFormatRequest,
    FetchRawRequest,
    PinResult,
    RawResponseParts,
    default_artifact_root,
    fetch_raw_response,
    format_fetch_result,
    normalize_fetch_format,
    normalize_url_list,
    page_payload_from_response,
)
from .web_http_helpers import (
    MAX_BODY_CHARS,
    HttpRequestParts,
    attach_http_advisory,
    normalize_headers,
    normalize_method,
    scalar_text,
)


@dataclass(frozen=True)
class WebRuntimeDeps:
    max_chars: int
    timeout: int
    resolver: Any
    normalize_url: Any
    response_preview_chars: Any
    network_safety_error: Any
    format_http_error: Any
    artifact_root: Path | None = None
    cache_ttl_seconds: int = 900
    allowed_private_hosts: tuple[str, ...] = ()
    allow_private_resolution: bool | None = None


@dataclass(frozen=True)
class CachedFetch:
    expires_at: float
    response: RawResponseParts


class WebFetchTool(BaseTool):
    """Fetch URL content, extract several URLs, or call a simple HTTP API."""

    def __init__(self, deps: WebRuntimeDeps):
        self.max_chars = deps.max_chars
        self.timeout = deps.timeout
        self.resolver = deps.resolver
        self.normalize_url = deps.normalize_url
        self.response_preview_chars = deps.response_preview_chars
        self.network_safety_error = deps.network_safety_error
        self.format_http_error = deps.format_http_error
        self.artifact_root = deps.artifact_root or default_artifact_root()
        self.cache_ttl_seconds = max(0, int(deps.cache_ttl_seconds))
        self.allowed_private_hosts = tuple(deps.allowed_private_hosts)
        self.allow_private_resolution = deps.allow_private_resolution
        self._cache: dict[tuple[str, str], CachedFetch] = {}
        self.spec = _web_fetch_spec()

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            max_chars = self.response_preview_chars(params, self.max_chars)
            mode = str(params.get("mode", params.get("format", "auto")) or "auto").strip().lower()
            if mode == "extract" or params.get("urls") is not None:
                return self._execute_extract(params, max_chars)
            request = self._request_parts(params, mode, max_chars)
        except ValueError as exc:
            return ToolExecutionResult("web_fetch", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")
        # 网络安全门已下沉到 _resolve_pin(逐跳:初始 URL + 每个重定向都校验+pin)。不再单独 pre-check——
        # 否则一次 execute 内 pre-check 与 resolve_pin 二次解析,对 DNS 翻转的判定会不一致。
        return self._execute_single_request(request, max_chars)

    def _execute_single_request(self, request: _WebFetchRequest, max_chars: int) -> ToolExecutionResult:
        cache_key = request.method == "GET" and not request.headers and request.body_text is None
        cached = self._cache_get(request.url, request.fmt) if cache_key else None
        cache_hit = cached is not None
        response = cached or self._fetch_raw_request(request)
        if isinstance(response, ToolExecutionResult):
            return response
        if cache_key and not cache_hit:
            self._cache_put(request.url, request.fmt, response)
        result = format_fetch_result(
            FetchFormatRequest(
                tool="web_fetch",
                artifact_root=self.artifact_root,
                url=request.url,
                response=response,
                fmt=request.fmt,
                max_chars=max_chars,
                cache_hit=cache_hit,
            )
        )
        attach_http_advisory(result, HttpRequestParts(
            url=request.url,
            method=request.method,
            headers=request.headers,
            body_text=request.body_text,
            max_chars=max_chars,
        ))
        return result

    def _resolve_pin(self, url: str) -> PinResult:
        """解析一次主机 → 用这"固定 IP 列表"过网关(避免与连接层二次解析的 TOCTOU)→ 返回校验过的 IP 来 pin。

        每个重定向跳也调本函数重新过网关,故内网/metadata 跳转会被拒。
        """
        host = urlsplit(url).hostname or ""
        try:
            resolved = tuple(str(ip) for ip in self.resolver(host))
        except Exception:
            resolved = ()
        err = self.network_safety_error(
            "web_fetch", url, lambda _h: resolved, self.allowed_private_hosts, self.allow_private_resolution
        )
        if err is not None:
            return PinResult(None, err)
        return PinResult(resolved[0] if resolved else None, None)

    def _fetch_raw_request(self, request: _WebFetchRequest) -> RawResponseParts | ToolExecutionResult:
        return fetch_raw_response(
            FetchRawRequest(
                tool="web_fetch",
                url=request.url,
                method=request.method,
                headers={"User-Agent": "MyAgent-WebFetch/1.0", **request.headers},
                data=None if request.body_text is None else request.body_text.encode("utf-8"),
                timeout=self.timeout,
            ),
            format_http_error=self.format_http_error,
            resolve_pin=self._resolve_pin,
        )

    def _request_parts(self, params: dict[str, Any], mode: str, max_chars: int) -> _WebFetchRequest:
        body = params.get("body")
        body_text = None if body is None else scalar_text(
            body,
            name="body",
            max_chars=MAX_BODY_CHARS,
            allow_empty=True,
        )
        return _WebFetchRequest(
            url=self.normalize_url(params.get("url")),
            method=normalize_method(params.get("method", "GET")),
            headers={} if params.get("headers") is None else normalize_headers(params.get("headers")),
            body_text=body_text,
            fmt=normalize_fetch_format(mode),
            max_chars=max_chars,
        )

    def _execute_extract(self, params: dict[str, Any], max_chars: int) -> ToolExecutionResult:
        urls = normalize_url_list(params.get("urls", params.get("url")), self.normalize_url)
        pages: list[dict[str, Any]] = []
        failures: list[dict[str, str | None]] = []
        for url in urls:
            network_error = self.network_safety_error(
                "web_fetch",
                url,
                self.resolver,
                self.allowed_private_hosts,
                self.allow_private_resolution,
            )
            if network_error is not None:
                failures.append({"url": url, "error_code": network_error.error_code, "error": network_error.output})
                continue
            response = fetch_raw_response(
                FetchRawRequest(
                    tool="web_fetch",
                    url=url,
                    method="GET",
                    headers={"User-Agent": "MyAgent-WebFetch/1.0"},
                    data=None,
                    timeout=self.timeout,
                ),
                format_http_error=self.format_http_error,
                resolve_pin=self._resolve_pin,
            )
            if isinstance(response, ToolExecutionResult):
                failures.append({"url": url, "error_code": response.error_code, "error": response.output})
                continue
            pages.append(page_payload_from_response(self.artifact_root, url, response, max_chars))
        payload = {"pages": pages, "failures": failures, "mode": "extract"}
        return ToolExecutionResult(
            "web_fetch",
            bool(pages),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            result_envelope=payload,
        )

    def _cache_get(self, url: str, fmt: str) -> RawResponseParts | None:
        if self.cache_ttl_seconds <= 0:
            return None
        item = self._cache.get((url, fmt))
        if not item or item.expires_at < time.time():
            return None
        return item.response

    def _cache_put(self, url: str, fmt: str, response: RawResponseParts) -> None:
        if self.cache_ttl_seconds <= 0:
            return
        self._cache[(url, fmt)] = CachedFetch(time.time() + self.cache_ttl_seconds, response)


@dataclass(frozen=True)
class _WebFetchRequest:
    url: str
    method: str
    headers: dict[str, str]
    body_text: str | None
    fmt: str
    max_chars: int


def _web_fetch_spec() -> ToolSpec:
    return ToolSpec(
        name="web_fetch",
        category="web",
        effect="read_only",
        output_trust="external_data",
        description="读取 URL、批量抽取网页，或带 method/header/body 调一个 HTTP/API；大内容保存为 artifact。",
        use_cases=[
            "已经知道 URL，需要读取网页、在线文档、PDF 或文本内容",
            "搜索后读取候选来源正文，保留可恢复的 artifact 引用",
            "需要 GET/POST/PUT/DELETE 等 HTTP/API 请求，但不想切换到另一个网络工具",
        ],
        avoid_when=["不知道 URL 时先用 web_search"],
        keywords=["网页", "打开URL", "fetch", "web_fetch", "markdown", "正文", "在线文档", "PDF", "API", "HTTP", "POST", "GET"],
        parameters={
            "url": "完整 URL；批量读取时也可用 urls",
            "urls": "可选，URL 数组；存在时按批量 extract 返回 pages/failures",
            "mode": "auto/markdown/text/html/json/raw/extract，默认 auto；extract 用于批量来源抽取",
            "method": "HTTP 方法，默认 GET",
            "headers": "可选请求头，JSON 对象或 JSON 字符串",
            "body": "可选请求体，适合 API 调用",
            "max_chars": "可选，本次返回预览字符数",
        },
        parameter_details={
            "url": "完整 http 或 https 地址；传 urls 时可省略。",
            "urls": "字符串或数组；用于一次读取多个来源并保存每页 artifact。",
            "mode": "auto 默认 HTML 转 Markdown、文本原样返回、二进制保存 artifact；extract 会返回 pages/failures。",
            "method": "可选，支持 GET/POST/PUT/DELETE 等；默认 GET。",
            "headers": "可传 JSON 对象或 JSON 字符串。",
            "body": "可选，请求体会按 utf-8 文本发送。",
            "max_chars": "只限制返回给模型的预览，不影响 artifact 保存。",
        },
        parameter_schema={
            "url": {"type": "string"},
            "urls": {"type": "array", "items": {"type": "string"}},
            "mode": {"type": "string", "enum": ["auto", "markdown", "text", "html", "json", "raw", "extract"]},
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"]},
            "headers": {"type": "object"},
            "body": {"type": "string"},
            "max_chars": {"type": "integer"},
        },
        examples=[
            '{"tool": "web_fetch", "url": "https://example.com/docs", "mode": "markdown"}',
            '{"tool": "web_fetch", "urls": ["https://example.com/a", "https://example.com/b"], "mode": "extract"}',
            '{"tool": "web_fetch", "url": "https://example.com/api", "method": "POST", "headers": {"Content-Type": "application/json"}, "body": "{\\"name\\": \\"demo\\"}", "mode": "json"}',
        ],
    )


__all__ = ["WebFetchTool", "WebRuntimeDeps"]
