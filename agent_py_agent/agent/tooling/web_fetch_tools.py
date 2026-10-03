# LLM: 网页缓存和批量抓取必须有总容量；refresh 只绕过本地缓存，不改变网络权限。
# 模块用途: 提供可归档的单页/批量 HTTP 读取，准确报告缓存、失败和未完成 URL。
from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..common.cancellation import cancellation_requested
from ..contracts.gates.network_safety import GatewayEndpointConfig, NetworkSafetySettings
from .models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    TimeoutPolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
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


# LLM: 注册时保存工具依赖和配置后备；每次解析时从当前工具副本读取热注入的私网授权。
# 类用途: 汇总 web_fetch 的超时、缓存、响应和 Gateway 配置后备，不冻结逐调用授权或保存凭据。
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
    gateway_endpoint_config: GatewayEndpointConfig = GatewayEndpointConfig()


@dataclass(frozen=True)
class CachedFetch:
    expires_at: float
    response: RawResponseParts


class WebFetchTool(BaseTool):
    """Fetch URL content, extract several URLs, or call a simple HTTP API."""

    # LLM: 不冻结 registry 每次调用注入的网络授权；保留 Gateway 配置后备供每一跳重取运行端口。
    # 函数用途: 保存稳定运行依赖与配置后备，网络授权在真正检查时再读取。
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
        self.gateway_endpoint_config = deps.gateway_endpoint_config
        self._cache: OrderedDict[tuple[str, str], CachedFetch] = OrderedDict()
        self._cache_lock = threading.Lock()
        self.model_spec = _web_fetch_model_spec()
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy(
                "read_only",
                by_parameter=((
                    "method",
                    (
                        ("GET", "read_only"),
                        ("HEAD", "read_only"),
                        ("POST", "dangerous"),
                        ("PUT", "dangerous"),
                        ("DELETE", "dangerous"),
                        ("PATCH", "dangerous"),
                    ),
                ),),
            ),
            idempotency_policy=IdempotencyPolicy("operation"),
            timeout_policy=TimeoutPolicy(self.timeout),
            resource_scopes=ResourceScopePolicy(
                parameter_names=("url", "urls"),
                parameter_kinds={"url": "logical", "urls": "logical"},
            ),
            output_policy=OutputPolicy(trust="external_data"),
        )

    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        try:
            max_chars = self.response_preview_chars(params, self.max_chars)
            mode = str(params.get("mode", params.get("format", "auto")) or "auto").strip().lower()
            if mode == "extract" or params.get("urls") is not None:
                return _execute_extract(self, params, max_chars)
            request = self._request_parts(params, mode, max_chars)
        except ValueError as exc:
            return ToolHandlerOutcome("web_fetch", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")
        # 网络安全门已下沉到 _resolve_pin(逐跳:初始 URL + 每个重定向都校验+pin)。不再单独 pre-check——
        # 否则一次 execute 内 pre-check 与 resolve_pin 二次解析,对 DNS 翻转的判定会不一致。
        return _execute_single_request(self, request, max_chars)

    # LLM: 每个 URL（含每个重定向目标）必须重新解析、应用当前 Gateway 端点事实，再 pin 通过检查的地址。
    # 函数用途: 解析 URL 并读取工具副本当前授权，逐跳检查后返回固定 IP 或结构化拒绝。
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
            "web_fetch", url, lambda _h: resolved, self._network_safety_settings()
        )
        if err is not None:
            return PinResult(None, err)
        return PinResult(resolved[0] if resolved else None, None)

    # LLM: registry 会在每次调用前把授权注入到工具副本属性；检查前必须从这些当前属性重建设置。
    # 函数用途: 生成这一跳使用的私网授权快照，不复用构造时的旧授权。
    def _network_safety_settings(self) -> NetworkSafetySettings:
        return NetworkSafetySettings(
            allowed_private_hosts=tuple(self.allowed_private_hosts or ()),
            allow_private_resolution=self.allow_private_resolution,
            gateway_endpoint=self.gateway_endpoint_config,
        )

    def _fetch_raw_request(self, request: _WebFetchRequest) -> RawResponseParts | ToolHandlerOutcome:
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
            refresh=bool(params.get("refresh", False)),
        )

@dataclass(frozen=True)
class _WebFetchRequest:
    url: str
    method: str
    headers: dict[str, str]
    body_text: str | None
    fmt: str
    max_chars: int
    refresh: bool = False


# LLM: 缓存命中与单页下载副作用分离，锁只包有界内存操作，不能覆盖网络请求。
# 函数用途: 处理单个 URL 的缓存读取、HTTP 抓取、格式化和请求回执。
def _execute_single_request(
    tool: WebFetchTool,
    request: _WebFetchRequest,
    max_chars: int,
) -> ToolHandlerOutcome:
    cache_key = request.method == "GET" and not request.headers and request.body_text is None
    cached = _cache_get(tool, request.url, request.fmt) if cache_key and not request.refresh else None
    cache_hit = cached is not None
    response = cached or tool._fetch_raw_request(request)
    if isinstance(response, ToolHandlerOutcome):
        return response
    if cache_key and not cache_hit:
        _cache_put(tool, request.url, request.fmt, response)
    result = format_fetch_result(
        FetchFormatRequest(
            tool="web_fetch",
            artifact_root=tool.artifact_root,
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


# LLM: 批量读取沿单页相同的逐跳网络闸；共享截止时间与字节预算，停止时明确列出未完成 URL。
# 函数用途: 抽取最多 16 页，保留被接受的方法/请求头/请求体与每页失败事实。
def _execute_extract(
    tool: WebFetchTool,
    params: dict[str, Any],
    max_chars: int,
) -> ToolHandlerOutcome:
    urls = normalize_url_list(params.get("urls", params.get("url")), tool.normalize_url)
    if len(urls) > 16:
        raise ValueError("批量抓取每次最多 16 个 URL，请分批读取")
    parts = tool._request_parts({**params, "url": urls[0]}, "auto", max_chars)
    pages: list[dict[str, Any]] = []
    failures: list[dict[str, str | None]] = []
    deadline, remaining_bytes = time.monotonic() + tool.timeout, 8 * 1024 * 1024
    remaining_urls = []
    for url in urls:
        if cancellation_requested() or time.monotonic() >= deadline or remaining_bytes <= 0:
            remaining_urls = urls[urls.index(url):]
            break
        response = fetch_raw_response(
            FetchRawRequest(
                tool="web_fetch",
                url=url,
                method=parts.method,
                headers={"User-Agent": "MyAgent-WebFetch/1.0", **parts.headers},
                data=None if parts.body_text is None else parts.body_text.encode("utf-8"),
                timeout=max(0.01, deadline - time.monotonic()),
                max_bytes=min(1_000_000, remaining_bytes),
            ),
            format_http_error=tool.format_http_error,
            resolve_pin=tool._resolve_pin,
        )
        if isinstance(response, ToolHandlerOutcome):
            failures.append({"url": url, "error_code": response.error_code, "error": response.output})
            continue
        remaining_bytes -= len(response.body)
        pages.append(page_payload_from_response(tool.artifact_root, url, response, max_chars))
    payload = {"pages": pages, "failures": failures, "mode": "extract",
               "complete": not failures and not remaining_urls, "remaining_urls": remaining_urls,
               "stop_reason": "cancelled" if cancellation_requested() else "budget" if remaining_urls else ""}
    return ToolHandlerOutcome(
        "web_fetch",
        bool(pages),
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        result_envelope=payload,
    )


# LLM: 过期缓存必须先清理；锁只覆盖内存字典访问，避免多个请求串行等待网络。
# 函数用途: 获取最近使用的缓存响应，并在使用时按 TTL 删除过期项。
def _cache_get(tool: WebFetchTool, url: str, fmt: str) -> RawResponseParts | None:
    if tool.cache_ttl_seconds <= 0:
        return None
    with tool._cache_lock:
        _expire_cache(tool)
        item = tool._cache.get((url, fmt))
        if item is None:
            return None
        tool._cache.move_to_end((url, fmt))
        return item.response


# LLM: 每工具缓存最多 64 项与 8 MiB 正文，缓存只优化已授权 GET，不改变请求权限。
# 函数用途: 保存响应并按 LRU 淘汰超期或超容量内容。
def _cache_put(
    tool: WebFetchTool,
    url: str,
    fmt: str,
    response: RawResponseParts,
) -> None:
    if tool.cache_ttl_seconds <= 0:
        return
    with tool._cache_lock:
        _expire_cache(tool)
        tool._cache[(url, fmt)] = CachedFetch(time.time() + tool.cache_ttl_seconds, response)
        tool._cache.move_to_end((url, fmt))
        while len(tool._cache) > 64 or sum(len(item.response.body) for item in tool._cache.values()) > 8 * 1024 * 1024:
            tool._cache.popitem(last=False)


# LLM: 调用方必须已获取缓存锁；此函数不操作文件或网络，只删除 TTL 到期的内存条目。
# 函数用途: 清除过期缓存，避免缓存正文无限期留在工具实例中。
def _expire_cache(tool: WebFetchTool) -> None:
    now = time.time()
    for key in list(tool._cache):
        if tool._cache[key].expires_at <= now:
            del tool._cache[key]


def _web_fetch_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="web_fetch",
        description="读取 URL、批量抽取网页，或带 method/header/body 调一个 HTTP/API；大内容保存为 artifact。",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "完整 http 或 https 地址；传 urls 时可省略。"},
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "URL 数组；用于一次读取多个来源并保存每页 artifact。",
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "markdown", "text", "html", "json", "raw", "extract"],
                    "description": "默认 auto；extract 用于批量来源抽取。",
                },
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
                    "description": "HTTP 方法，默认 GET；非读取方法属于危险外部副作用并进入审批门。",
                },
                "headers": {"type": "object", "description": "可选请求头 JSON 对象。"},
                "body": {"type": "string", "description": "可选，请求体按 UTF-8 文本发送。"},
                "refresh": {"type": "boolean", "default": False, "description": "true 时绕过本地缓存重新获取；不代表上游 CDN 必定刷新。"},
                "max_chars": {"type": "integer", "minimum": 1, "description": "只限制模型预览，不影响 artifact 保存。"},
            },
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="web",
            use_cases=(
                "已经知道 URL，需要读取网页、在线文档、PDF 或文本内容",
                "搜索后读取候选来源正文，保留可恢复的 artifact 引用",
                "需要 GET/POST/PUT/DELETE 等 HTTP/API 请求",
            ),
            avoid_when=("不知道 URL 时先用 web_search",),
            keywords=("网页", "打开URL", "fetch", "web_fetch", "markdown", "正文", "在线文档", "PDF", "API", "HTTP", "POST", "GET"),
            examples=(
                '{"tool": "web_fetch", "url": "https://example.com/docs", "mode": "markdown"}',
                '{"tool": "web_fetch", "urls": ["https://example.com/a", "https://example.com/b"], "mode": "extract"}',
                '{"tool": "web_fetch", "url": "https://example.com/api", "method": "POST", "headers": {"Content-Type": "application/json"}, "body": "{\\"name\\": \\"demo\\"}", "mode": "json"}',
            ),
        ),
    )


__all__ = ["WebFetchTool", "WebRuntimeDeps"]
