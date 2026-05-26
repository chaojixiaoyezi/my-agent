# LLM: web_fetch/web_extract keep page reading separate from low-level http_request.
# 模块用途: 提供单页读取和多页抽取工具，保存大正文/二进制内容为 artifact ref。

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import BaseTool, ToolExecutionResult, ToolSpec
from .web_fetch_runtime import (
    FetchFormatRequest,
    FetchRawRequest,
    RawResponseParts,
    default_artifact_root,
    fetch_raw_response,
    format_fetch_result,
    normalize_fetch_format,
    normalize_url_list,
    page_payload_from_response,
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


# LLM: CachedFetch stores short-lived GET responses for repeated model reads.
# 类用途: 为 web_fetch 的同 URL/format 短期缓存保存过期时间和响应。
@dataclass(frozen=True)
class CachedFetch:
    expires_at: float
    response: RawResponseParts


# LLM: WebFetchTool returns a small preview and stores binary content as artifacts.
# 类用途: 打开一个 URL，按 auto/markdown/text/html 返回模型可读预览。
class WebFetchTool(BaseTool):
    """Fetch one URL as markdown/text/html or as an artifact for binary content."""

    # LLM: WebFetchTool.__init__ wires timeout, DNS resolver, cache and artifact root.
    # 函数用途: 初始化 web_fetch 的配置、依赖和工具说明。
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
        self.spec = ToolSpec(
            name="web_fetch",
            category="web",
            effect="read_only",
            description="打开一个 URL，并按 markdown/text/html 返回网页正文；二进制或大内容保存为 artifact。",
            use_cases=[
                "已经知道 URL，需要读取网页、在线文档、PDF 或文本内容",
                "搜索后读取候选来源正文，保留可恢复的 artifact 引用",
            ],
            avoid_when=[
                "不知道 URL 时先用 web_search；需要 POST/DELETE/API body 时用 http_request",
            ],
            keywords=["网页", "打开URL", "fetch", "web_fetch", "markdown", "正文", "在线文档", "PDF"],
            parameters={
                "url": "完整 URL",
                "format": "auto/markdown/text/html，默认 auto",
                "max_chars": "可选，本次返回预览字符数",
            },
            parameter_details={
                "url": "必填，完整 http 或 https 地址。",
                "format": "auto 默认 HTML 转 Markdown、文本原样返回、二进制保存 artifact。",
                "max_chars": "只限制返回给模型的预览，不影响 artifact 保存。",
            },
            examples=[
                '{"tool": "web_fetch", "url": "https://example.com/docs", "format": "markdown"}',
            ],
        )

    # LLM: WebFetchTool.execute performs GET, cache lookup and preview/artifact formatting.
    # 函数用途: 执行单 URL 读取；网络失败返回工具错误，不终止任务流程。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            url = self.normalize_url(params.get("url"))
            max_chars = self.response_preview_chars(params, self.max_chars)
            fmt = normalize_fetch_format(params.get("format", "auto"))
        except ValueError as exc:
            return ToolExecutionResult("web_fetch", False, str(exc))
        network_error = self.network_safety_error(
            "web_fetch",
            url,
            self.resolver,
            self.allowed_private_hosts,
            self.allow_private_resolution,
        )
        if network_error is not None:
            return network_error
        cached = self._cache_get(url, fmt)
        cache_hit = cached is not None
        response = cached or fetch_raw_response(
            FetchRawRequest(
                tool="web_fetch",
                url=url,
                method="GET",
                headers={"User-Agent": "MyAgent-WebFetch/1.0"},
                data=None,
                timeout=self.timeout,
            ),
            format_http_error=self.format_http_error,
        )
        if isinstance(response, ToolExecutionResult):
            return response
        if not cache_hit:
            self._cache_put(url, fmt, response)
        return format_fetch_result(
            FetchFormatRequest(
                tool="web_fetch",
                artifact_root=self.artifact_root,
                url=url,
                response=response,
                fmt=fmt,
                max_chars=max_chars,
                cache_hit=cache_hit,
            )
        )

    # LLM: _cache_get returns valid cached responses only.
    # 函数用途: 按 URL 和格式读取未过期缓存，过期时返回空。
    def _cache_get(self, url: str, fmt: str) -> RawResponseParts | None:
        if self.cache_ttl_seconds <= 0:
            return None
        item = self._cache.get((url, fmt))
        if not item or item.expires_at < time.time():
            return None
        return item.response

    # LLM: _cache_put stores successful GET responses for repeated local reads.
    # 函数用途: 将读取结果按 TTL 写入内存缓存。
    def _cache_put(self, url: str, fmt: str, response: RawResponseParts) -> None:
        if self.cache_ttl_seconds <= 0:
            return
        self._cache[(url, fmt)] = CachedFetch(time.time() + self.cache_ttl_seconds, response)


# LLM: WebExtractTool reads several URLs and returns per-page refs instead of one huge blob.
# 类用途: 批量读取网页，把全文或二进制内容保存成 artifact，并返回预览/失败列表。
class WebExtractTool(BaseTool):
    """Extract readable text from one or more URLs and persist full text artifacts."""

    # LLM: WebExtractTool.__init__ wires resolver and artifact storage for multi-source extraction.
    # 函数用途: 初始化 web_extract 的配置、依赖和工具说明。
    def __init__(self, deps: WebRuntimeDeps):
        self.max_chars = deps.max_chars
        self.timeout = deps.timeout
        self.resolver = deps.resolver
        self.normalize_url = deps.normalize_url
        self.response_preview_chars = deps.response_preview_chars
        self.network_safety_error = deps.network_safety_error
        self.format_http_error = deps.format_http_error
        self.artifact_root = deps.artifact_root or default_artifact_root()
        self.allowed_private_hosts = tuple(deps.allowed_private_hosts)
        self.allow_private_resolution = deps.allow_private_resolution
        self.spec = ToolSpec(
            name="web_extract",
            category="web",
            effect="read_only",
            description="批量抽取 URL 正文，保存全文 artifact，并返回标题、预览、hash 和引用。",
            use_cases=[
                "研究任务需要同时读取多个候选网页并保留来源证据",
                "网页很长时保存全文，只把摘要/预览放回上下文",
            ],
            avoid_when=[
                "只打开一个短网页时 web_fetch 更直接；需要 API 请求体时用 http_request",
            ],
            keywords=["网页抽取", "正文抽取", "web_extract", "多URL", "来源证据", "artifact"],
            parameters={
                "urls": "URL 字符串或 URL 数组",
                "max_chars": "每页返回预览字符数",
            },
            parameter_details={
                "urls": "必填，可传一个 URL 或多个 URL。",
                "max_chars": "可选；只影响每页 preview，全文始终保存 artifact。",
            },
            examples=[
                '{"tool": "web_extract", "urls": ["https://example.com/a", "https://example.com/b"]}',
            ],
        )

    # LLM: WebExtractTool.execute keeps partial success visible when some URLs fail.
    # 函数用途: 批量抽取 URL，成功页和失败页分开返回，避免一个坏来源掩盖其他证据。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            urls = normalize_url_list(params.get("urls"), self.normalize_url)
            max_chars = self.response_preview_chars(params, self.max_chars)
        except ValueError as exc:
            return ToolExecutionResult("web_extract", False, str(exc))
        pages: list[dict[str, Any]] = []
        failures: list[dict[str, str | None]] = []
        for url in urls:
            network_error = self.network_safety_error(
                "web_extract",
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
                    tool="web_extract",
                    url=url,
                    method="GET",
                    headers={"User-Agent": "MyAgent-WebExtract/1.0"},
                    data=None,
                    timeout=self.timeout,
                ),
                format_http_error=self.format_http_error,
            )
            if isinstance(response, ToolExecutionResult):
                failures.append({"url": url, "error_code": response.error_code, "error": response.output})
                continue
            pages.append(page_payload_from_response(self.artifact_root, url, response, max_chars))
        payload = {"pages": pages, "failures": failures}
        return ToolExecutionResult(
            "web_extract",
            bool(pages),
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            result_envelope=payload,
        )


__all__ = ["WebFetchTool", "WebExtractTool", "WebRuntimeDeps"]
