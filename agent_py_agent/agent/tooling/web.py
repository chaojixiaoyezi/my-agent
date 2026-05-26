# LLM: 网络访问边界和错误格式要稳定，避免外部请求拖垮主流程。
# 模块用途: HTTP 和网页抓取工具，统一 URL/header 校验、超时和响应截断。

from __future__ import annotations

"""implements outbound HTTP tools behind explicit timeout and output-size limits.

给人看的解释：
这个文件只负责访问网络。
`web_fetch` 偏向'简单打开一个网页'，`web_extract` 偏向'批量抽取来源'，`http_request` 偏向'调接口、带 header、带 body'。
这里统一限制超时时间和返回长度，避免一次请求把主流程卡死或把 prompt 撑爆。
"""

import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..contracts.gates import NetworkResolver, NetworkSafetyFacts, evaluate_network_safety_gate
from .models import BaseTool, ToolExecutionResult, ToolSpec
from .web_fetch_tools import WebExtractTool as _WebExtractTool
from .web_fetch_tools import WebFetchTool as _WebFetchTool
from .web_fetch_tools import WebRuntimeDeps
from .web_html_preview import format_html_response, is_html_response, visible_html_text

_MAX_URL_CHARS = 4096
_MAX_BODY_CHARS = 1_000_000
_MAX_HEADER_JSON_CHARS = 65536
_MAX_HEADER_COUNT = 100
_MAX_HEADER_NAME_CHARS = 128
_MAX_HEADER_VALUE_CHARS = 8192
_MAX_METHOD_CHARS = 16
_MIN_RESPONSE_PREVIEW_CHARS = 256
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HTTP_METHOD_RE = re.compile(r"^[A-Z][A-Z0-9_-]*$")
_TEXTUAL_CONTENT_MARKERS = (
    "text/",
    "json",
    "xml",
    "javascript",
    "x-www-form-urlencoded",
)
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# LLM: _ResponseParts 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: HTTP 响应片段模型，统一传递工具名、状态、头和正文。
@dataclass(frozen=True)
class _ResponseParts:
    tool: str
    status: int
    headers: Any
    body: str


@dataclass(frozen=True)
class _HttpRequestParts:
    url: str
    method: str
    headers: dict[str, str]
    body_text: str | None
    max_chars: int


# LLM: _has_control_chars 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 判断 has_control_chars 是否满足安全或状态条件。
def _has_control_chars(text: str) -> bool:
    return any(ord(char) < 32 for char in text)


# LLM: _scalar_text 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 scalar_text 步骤，并保持调用方依赖的数据形状。
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


# LLM: _normalize_url 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把输入值归一成 工具系统 内部使用的稳定格式。
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


# LLM: _default_network_resolver supplies DNS facts for the network_safety gate.
# 函数用途: 只解析 host 到 IP，不发起 HTTP 请求；失败由 network_safety gate 转成结构化错误。
def _default_network_resolver(host: str) -> tuple[str, ...]:
    answers = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    return tuple(str(sockaddr[0]) for *_prefix, sockaddr in answers)


# LLM: _network_safety_error converts SSRF/DNS gate denials into normal tool failures.
# 函数用途: 所有出站 HTTP 工具在请求前统一过 network_safety 门，避免只靠 URL 字面校验。
def _network_safety_error(
    tool: str,
    url: str,
    resolver: NetworkResolver,
    allowed_private_hosts: Iterable[str] = (),
    allow_private_resolution: bool | None = None,
) -> ToolExecutionResult | None:
    decision = evaluate_network_safety_gate(
        NetworkSafetyFacts(
            url=url,
            resolver=resolver,
            allowed_private_hosts=allowed_private_hosts,
            allow_private_resolution=_effective_allow_private_resolution(allow_private_resolution),
        )
    )
    if decision.allowed:
        return None
    code = decision.finding_codes[0] if decision.finding_codes else "NETWORK_SAFETY_DENIED"
    return ToolExecutionResult(
        tool,
        False,
        f"网络安全检查失败: {code}",
        result_envelope={"network_safety_gate": decision.to_dict()},
        error_code=code,
    )


# LLM: _effective_allow_private_resolution keeps proxy/VPN private DNS opt-in structured.
# 函数用途: 从显式参数或环境配置读取是否允许私网解析，但 metadata/link-local 仍由 gate 永久拦截。
def _effective_allow_private_resolution(value: bool | None) -> bool:
    if value is not None:
        return bool(value)
    raw = os.environ.get("MY_AGENT_ALLOW_PRIVATE_URLS", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# LLM: _normalize_method 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把输入值归一成 工具系统 内部使用的稳定格式。
def _normalize_method(value: Any) -> str:
    method = _scalar_text(value, name="method", max_chars=_MAX_METHOD_CHARS).upper()
    if not _HTTP_METHOD_RE.fullmatch(method):
        raise ValueError("method 必须是有效的 HTTP 方法名")
    return method


# LLM: _format_response 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把 format_response 转成人或模型可读的展示文本。
def _format_response(parts: _ResponseParts, max_chars: int) -> ToolExecutionResult:
    if is_html_response(parts.headers):
        output = format_html_response(status=parts.status, headers=parts.headers, body=parts.body, max_chars=max_chars)
        return ToolExecutionResult(parts.tool, True, output)
    result = (
        f"status={parts.status}\n"
        f"content_type={parts.headers.get('Content-Type', '')}\n\n"
        f"{parts.body[:max_chars]}"
    )
    if len(parts.body) > max_chars:
        result += "\n... 已截断"
    return ToolExecutionResult(parts.tool, True, result)


# LLM: _format_http_error 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把 format_http_error 转成人或模型可读的展示文本。
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


# LLM: _response_preview_chars lets agents request small previews without raising global config.
# 函数用途: 读取可选 max_chars，但只能缩小到本工具配置上限，避免大网页反复灌进模型上下文。
def _response_preview_chars(params: dict[str, Any], configured_max: int) -> int:
    raw = params.get("max_chars")
    if raw in (None, ""):
        return configured_max
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        return configured_max
    return max(_MIN_RESPONSE_PREVIEW_CHARS, min(configured_max, requested))


# LLM: FetchUrlTool 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: FetchUrlTool 数据模型，集中保存 工具系统 的结构化状态。
class FetchUrlTool(BaseTool):

    # LLM: FetchUrlTool.__init__ 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 FetchUrlTool 的依赖、配置和运行期字段。
    def __init__(
        self,
        *,
        max_chars: int,
        timeout: int,
        resolver: NetworkResolver | None = None,
        allowed_private_hosts: Iterable[str] = (),
        allow_private_resolution: bool | None = None,
    ):
        self.max_chars = max_chars
        self.timeout = timeout
        self.resolver = resolver or _default_network_resolver
        self.allowed_private_hosts = tuple(allowed_private_hosts)
        self.allow_private_resolution = allow_private_resolution
        self.spec = ToolSpec(
            name="fetch_url",
            category="web",
            effect="read_only",
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
                "max_chars": "可选，限制本次返回正文预览字符数",
            },
            parameter_details={
                "url": "必填，传入完整的 http 或 https 地址；工具内部固定按 GET 请求处理。",
                "max_chars": "可选；只缩小本次预览，不能超过配置文件里的 tool_web_max_chars。研究/批量抓取时建议先用较小预览。",
            },
            examples=[
                '{"tool": "fetch_url", "url": "https://example.com/docs"}',
            ],
        )

    # LLM: FetchUrlTool.execute 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 执行 FetchUrlTool 的主流程并返回 ToolExecutionResult。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            url = _normalize_url(params.get("url"))
            max_chars = _response_preview_chars(params, self.max_chars)
        except ValueError as exc:
            return ToolExecutionResult("fetch_url", False, str(exc))
        network_error = _network_safety_error(
            "fetch_url",
            url,
            self.resolver,
            self.allowed_private_hosts,
            self.allow_private_resolution,
        )
        if network_error is not None:
            return network_error

        req = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": "SimplePythonAgent/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                return _format_response(_ResponseParts("fetch_url", resp.status, resp.headers, body), max_chars)
        except urllib.error.HTTPError as exc:
            return _format_http_error("fetch_url", exc, max_chars)
        except (urllib.error.URLError, TimeoutError) as exc:
            return ToolExecutionResult("fetch_url", False, f"请求失败: {exc.__class__.__name__}")


# LLM: WebFetchTool keeps the old import path while implementation lives in web_fetch_tools.py.
# 类用途: 给旧调用方保留 agent.tooling.web.WebFetchTool，同时注入共享 URL、安全和错误格式 helper。
class WebFetchTool(_WebFetchTool):
    def __init__(self, *, max_chars: int, timeout: int, **kwargs: Any):
        super().__init__(WebRuntimeDeps(
            max_chars=max_chars,
            timeout=timeout,
            resolver=kwargs.pop("resolver", None) or _default_network_resolver,
            normalize_url=_normalize_url,
            response_preview_chars=_response_preview_chars,
            network_safety_error=_network_safety_error,
            format_http_error=_format_http_error,
            **kwargs,
        ))


# LLM: WebExtractTool keeps the old import path while implementation lives in web_fetch_tools.py.
# 类用途: 给旧调用方保留 agent.tooling.web.WebExtractTool，同时注入共享 URL、安全和错误格式 helper。
class WebExtractTool(_WebExtractTool):
    def __init__(self, *, max_chars: int, timeout: int, **kwargs: Any):
        super().__init__(WebRuntimeDeps(
            max_chars=max_chars,
            timeout=timeout,
            resolver=kwargs.pop("resolver", None) or _default_network_resolver,
            normalize_url=_normalize_url,
            response_preview_chars=_response_preview_chars,
            network_safety_error=_network_safety_error,
            format_http_error=_format_http_error,
            **kwargs,
        ))


# LLM: HttpRequestTool 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: HttpRequestTool 数据模型，集中保存 工具系统 的结构化状态。
class HttpRequestTool(BaseTool):

    # LLM: HttpRequestTool.__init__ 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 HttpRequestTool 的依赖、配置和运行期字段。
    def __init__(
        self,
        *,
        max_chars: int,
        timeout: int,
        resolver: NetworkResolver | None = None,
        allowed_private_hosts: Iterable[str] = (),
        allow_private_resolution: bool | None = None,
    ):
        self.max_chars = max_chars
        self.timeout = timeout
        self.resolver = resolver or _default_network_resolver
        self.allowed_private_hosts = tuple(allowed_private_hosts)
        self.allow_private_resolution = allow_private_resolution
        self.spec = ToolSpec(
            name="http_request",
            category="api",
            effect="mutating",
            requires_idempotency=True,
            description="发送通用 HTTP 请求，适合调 REST API、Webhook 和普通接口。",
            use_cases=[
                "测试 GET/POST/PUT/DELETE 等接口返回是否正常",
                "带请求头、请求体去联调 API",
            ],
            avoid_when=[
                "只是想看一个普通网页正文时，web_fetch 更简单",
            ],
            keywords=["API", "接口", "HTTP", "POST", "GET", "Webhook", "请求头", "请求体"],
            parameters={
                "url": "完整 URL",
                "method": "HTTP 方法，默认 GET",
                "headers": "可选请求头",
                "body": "可选请求体",
                "max_chars": "可选，限制本次返回正文预览字符数",
            },
            parameter_details={
                "url": "必填，接口完整地址。",
                "method": "可选，支持 GET/POST/PUT/DELETE 等；默认是 GET。",
                "headers": "可传 JSON 对象或 JSON 字符串，常用于 Content-Type、Authorization 等。",
                "body": "可选，请求体会按 utf-8 文本发送；适合传 JSON 字符串或普通文本。",
                "max_chars": "可选；只缩小本次预览，不能超过配置文件里的 tool_web_max_chars。分页/批量 API 建议先用较小预览。",
            },
            examples=[
                '{"tool": "http_request", "url": "https://example.com/health"}',
                '{"tool": "http_request", "url": "https://example.com/api", "method": "POST", "headers": {"Content-Type": "application/json"}, "body": "{\\"name\\": \\"demo\\"}"}',
            ],
        )

    # LLM: HttpRequestTool.execute 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 执行 HttpRequestTool 的主流程并返回 ToolExecutionResult。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            request = self._request_parts(params)
        except ValueError as exc:
            return ToolExecutionResult("http_request", False, str(exc))
        network_error = _network_safety_error(
            "http_request",
            request.url,
            self.resolver,
            self.allowed_private_hosts,
            self.allow_private_resolution,
        )
        if network_error is not None:
            return network_error
        return self._execute_request(request)

    def _request_parts(self, params: dict[str, Any]) -> _HttpRequestParts:
        body = params.get("body")
        body_text = None if body is None else _scalar_text(
            body,
            name="body",
            max_chars=_MAX_BODY_CHARS,
            allow_empty=True,
        )
        return _HttpRequestParts(
            url=_normalize_url(params.get("url")),
            method=_normalize_method(params.get("method", "GET")),
            headers=self._normalize_headers(params.get("headers")),
            body_text=body_text,
            max_chars=_response_preview_chars(params, self.max_chars),
        )

    def _execute_request(self, request: _HttpRequestParts) -> ToolExecutionResult:
        data = None if request.body_text is None else request.body_text.encode("utf-8")
        req = urllib.request.Request(request.url, data=data, method=request.method, headers=request.headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                response_body = resp.read().decode("utf-8", "replace")
                result = _format_response(
                    _ResponseParts("http_request", resp.status, resp.headers, response_body),
                    request.max_chars,
                )
                _attach_http_advisory(result, request)
                return result
        except urllib.error.HTTPError as exc:
            return _format_http_error("http_request", exc, request.max_chars)
        except (urllib.error.URLError, TimeoutError) as exc:
            return ToolExecutionResult("http_request", False, f"请求失败: {exc.__class__.__name__}")

    # LLM: HttpRequestTool._normalize_headers 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把输入值归一成 工具系统 内部使用的稳定格式。
    def _normalize_headers(self, headers: Any) -> dict[str, str]:

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


def _attach_http_advisory(result: ToolExecutionResult, request: _HttpRequestParts) -> None:
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


# LLM: _normalize_header_dict 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把输入值归一成 工具系统 内部使用的稳定格式。
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
