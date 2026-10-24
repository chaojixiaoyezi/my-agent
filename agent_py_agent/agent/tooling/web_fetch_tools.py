# LLM: 缓存/批量必须有总容量；凭据选择仅来自原宿主并进入安全回执，refresh不改变网络或凭据权限。
# 模块用途: 提供可归档的单页/批量 HTTP 读取，准确报告缓存、失败和未完成 URL。
from __future__ import annotations

import base64
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit

from ..common.cancellation import cancellation_requested
from ..contracts.gates.network_safety import (
    GatewayEndpointConfig,
    NetworkSafetySettings,
    _canonical_ip,
)
from ..user_space.task_credential_storage import CredentialInputError
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
    allowed_private_hosts_provider: Any = None
    allow_private_resolution: bool | None = None
    gateway_endpoint_config: GatewayEndpointConfig = GatewayEndpointConfig()


@dataclass(frozen=True)
class CachedFetch:
    expires_at: float
    response: RawResponseParts


# LLM: 每跳审计后 pin 同一地址；共享缓存必须隔离授权且命中时仍重检端点。
# 类用途: 统一抓取网页/API，保存响应但不缓存网络权限。
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
        self._allowed_private_hosts = tuple(deps.allowed_private_hosts)
        self._allowed_private_hosts_provider = deps.allowed_private_hosts_provider
        self._extra_private_hosts: tuple[str, ...] = ()
        self.allow_private_resolution = deps.allow_private_resolution
        self.gateway_endpoint_config = deps.gateway_endpoint_config
        self._cache: OrderedDict[tuple[object, ...], CachedFetch] = OrderedDict()
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

    # LLM: 动态配置由 provider 实时提供，逐调用临时授权独立保存在 _extra_private_hosts 中合并读取。
    # 函数用途: 返回当前生效的私网白名单元组，保证撤权立即对后续请求生效。
    @property
    def allowed_private_hosts(self) -> tuple[str, ...]:
        provider = self._allowed_private_hosts_provider
        if not callable(provider):
            return self._allowed_private_hosts
        base = tuple(provider() or ())
        return tuple(dict.fromkeys([*base, *self._extra_private_hosts]))

    # LLM: 直接赋值仅在无动态 provider 时更新静态白名单；有 provider 时仅由 _extra_private_hosts 承载临时授权。
    # 函数用途: 兼容静态白名单属性写入，不将过期动态配置快照固化进临时授权字段。
    @allowed_private_hosts.setter
    def allowed_private_hosts(self, value: object) -> None:
        if isinstance(value, tuple) and all(isinstance(item, str) for item in value):
            items = value
        elif isinstance(value, (list, tuple, set, frozenset)):
            items = tuple(item for item in value if isinstance(item, str))
        else:
            items = ()
        if callable(self._allowed_private_hosts_provider):
            base = set(self._allowed_private_hosts_provider() or ())
            self._extra_private_hosts = tuple(item for item in items if item not in base)
        else:
            self._allowed_private_hosts = items

    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        try:
            max_chars = self.response_preview_chars(params, self.max_chars)
            mode = str(params.get("mode", params.get("format", "auto")) or "auto").strip().lower()
            if mode == "extract" or params.get("urls") is not None:
                if params.get("credential_ref"):
                    raise ValueError("凭据引用仅用于单接口请求")
                return _execute_extract(self, params, max_chars)
            request = self._request_parts(params, mode, max_chars)
        except ValueError as exc:
            return ToolHandlerOutcome("web_fetch", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")
        # 网络安全门已下沉到 _resolve_pin(逐跳:初始 URL + 每个重定向都校验+pin)。不再单独 pre-check——
        # 否则一次 execute 内 pre-check 与 resolve_pin 二次解析,对 DNS 翻转的判定会不一致。
        return _execute_single_request(self, request, max_chars)

    # LLM: 每跳重查端点；字面 IP 直接 canonical，不接受 resolver 对已审计地址的替换。
    # 函数用途: 解析 URL 并读取工具副本当前授权，逐跳检查后返回固定 IP 或结构化拒绝。
    def _resolve_pin(self, url: str, *, resolved_by_host: dict[str, tuple[str, ...]] | None = None) -> PinResult:
        """解析一次主机 → 用这"固定 IP 列表"过网关(避免与连接层二次解析的 TOCTOU)→ 返回校验过的 IP 来 pin。

        每个重定向跳也调本函数重新过网关,故内网/metadata 跳转会被拒。
        """
        try:
            host = (urlsplit(url).hostname or "").lower().strip("[]").rstrip(".")
            literal_ip = _canonical_ip(host)
            resolved = (literal_ip,) if literal_ip else tuple(str(ip) for ip in self.resolver(host))
        except Exception:
            host, literal_ip, resolved = "", "", ()
        previous = resolved_by_host.get(host, ()) if resolved_by_host is not None and host else ()
        err = self.network_safety_error(
            "web_fetch", url, lambda _h: resolved, self._network_safety_settings(previous_resolved_ips=previous)
        )
        if err is not None:
            return PinResult(None, err)
        if resolved_by_host is not None and host and not literal_ip and resolved:
            resolved_by_host.setdefault(host, resolved)
        return PinResult(resolved[0] if resolved else None, None)

    # LLM: registry 会在每次调用前把授权注入到工具副本属性；检查前必须从这些当前属性重建设置。
    # 函数用途: 生成这一跳使用的私网授权快照，不复用构造时的旧授权。
    def _network_safety_settings(self, *, previous_resolved_ips: tuple[str, ...] = ()) -> NetworkSafetySettings:
        return NetworkSafetySettings(
            allowed_private_hosts=tuple(self.allowed_private_hosts or ()),
            allow_private_resolution=self.allow_private_resolution,
            gateway_endpoint=self.gateway_endpoint_config,
            previous_resolved_ips=previous_resolved_ips,
        )

    # LLM: 凭据错误保留宿主code/stage，不混成发卡等待；净化覆盖成功/失败正文且在归档前，传输仍逐跳pin。
    # 函数用途: 发起一次HTTP请求，保留可行动的群凭据拒绝原因并移除秘密回显。
    def _fetch_raw_request(self, request: _WebFetchRequest) -> RawResponseParts | ToolHandlerOutcome:
        raw = FetchRawRequest(tool="web_fetch", url=request.url, method=request.method,
            headers={"User-Agent": "MyAgent-WebFetch/1.0", **request.headers},
            data=None if request.body_text is None else request.body_text.encode("utf-8"), timeout=self.timeout)
        resolved_by_host: dict[str, tuple[str, ...]] = {}
        # LLM: send 固定原逐跳 pin，不接受任意脚本、库或模型提供的请求回调。
        # 函数用途: 为宿主登录和业务请求复用本工具的实际网络许可。
        def send(raw_request):
            return fetch_raw_response(
                raw_request,
                format_http_error=self.format_http_error,
                resolve_pin=lambda hop_url: self._resolve_pin(hop_url, resolved_by_host=resolved_by_host),
            )
        try:
            if request.credential_ref and getattr(self, "credential_authenticator", None):
                response, secrets = self.credential_authenticator(request.credential_ref, raw, send)
                request = replace(request, secrets_to_redact=secrets)
            else:
                response = send(raw)
        except CredentialInputError as exc:
            return ToolHandlerOutcome('web_fetch', False, str(exc), error_code=exc.code,
                effect_outcome=exc.effect_outcome, result_envelope={'credential_error': {'stage': exc.stage, 'error_code': exc.code}})
        except (ValueError, KeyError, TypeError):
            return ToolHandlerOutcome("web_fetch", False, "私人认证未完成、已失效或方案无效。", error_code="CREDENTIAL_INPUT_UNAVAILABLE")
        if isinstance(response, ToolHandlerOutcome) and request.secrets_to_redact:
            response = replace(response,
                output=_redact_secret_echo(response.output, request.secrets_to_redact),
                result_envelope=_redact_secret_echo(response.result_envelope, request.secrets_to_redact))
        elif isinstance(response, RawResponseParts) and request.secrets_to_redact:
            body = response.body
            for secret in _secret_echo_variants(request.secrets_to_redact):
                body = body.replace(secret.encode("utf-8"), b"[private]")
            headers = {str(k): str(v) for k, v in response.headers.items()}
            for key, value in headers.items():
                for secret in _secret_echo_variants(request.secrets_to_redact):
                    value = value.replace(secret, "[private]")
                headers[key] = value
            response = replace(response, body=body, headers=headers)
        return response

    # LLM: 引用仅由宿主解析，本次 origin/scope 校验失败不发网络；认证头与同名显式头不能混用。
    # 函数用途: 校验 HTTP 参数并在内存中注入授权凭据，不把明文写进工具参数或回执。
    def _request_parts(self, params: dict[str, Any], mode: str, max_chars: int) -> _WebFetchRequest:
        body = params.get("body")
        body_text = None if body is None else scalar_text(
            body,
            name="body",
            max_chars=MAX_BODY_CHARS,
            allow_empty=True,
        )
        url = self.normalize_url(params.get("url"))
        headers = {} if params.get("headers") is None else normalize_headers(params.get("headers"))
        secrets = ()
        reference = str(params.get("credential_ref") or "")
        if reference and not getattr(self, "credential_authenticator", None):
            resolver = getattr(self, "credential_resolver", None)
            if resolver is None:
                raise ValueError("凭据注入不可用")
            private_headers, secrets = resolver(reference, url)
            if {key.lower() for key in private_headers} & {key.lower() for key in headers}:
                raise ValueError("不能同时指定同名认证头和凭据引用")
            headers.update(private_headers)
        return _WebFetchRequest(
            url=url,
            method=normalize_method(params.get("method", "GET")),
            headers=headers,
            secrets_to_redact=secrets,
            credential_ref=reference,
            body_text=body_text,
            fmt=normalize_fetch_format(mode),
            max_chars=max_chars,
            refresh=bool(params.get("refresh", False)),
        )

# LLM: 凭据仅存在于本次宿主请求内；secrets_to_redact 只供归档/模型输出前净化，不进入缓存键或工具回执。
# 类用途: 保存已校验 HTTP 请求和本次需要抹除的秘密回显，执行结束即释放。
@dataclass(frozen=True)
class _WebFetchRequest:
    url: str
    method: str
    headers: dict[str, str]
    body_text: str | None
    fmt: str
    max_chars: int
    refresh: bool = False
    secrets_to_redact: tuple[str, ...] = ()
    credential_ref: str = ""


# LLM: 只净化本调用已知凭据的原文、JSON、URL/表单及常见 Base64 回显；须在格式化、归档和模型前调用，不能猜任意外部字符串是密钥。
# 函数用途: 生成本次秘密的有限编码变体，先替换长串；不改变实际请求或增加跨 owner 的秘密表。
def _secret_echo_variants(secrets):
    values=set()
    for value in secrets:
        if not value:
            continue
        values.update((value,json.dumps(value,ensure_ascii=True)[1:-1],
            json.dumps(value,ensure_ascii=False)[1:-1],quote(value,safe=''),quote_plus(value,safe='')))
        for encoder in (base64.b64encode, base64.urlsafe_b64encode):
            encoded = encoder(value.encode('utf-8')).decode('ascii')
            values.update((encoded, encoded.rstrip('=')))
    return sorted(values,key=len,reverse=True)


# LLM: 保持错误信封结构，只替换字符串值；不把外部响应当控制字段，也不泄露 HTTP 失败的认证回显。
# 函数用途: 清理单条错误文字及嵌套错误证据里的本次秘密，不写文件或发送请求。
def _redact_secret_echo(value, secrets):
    if isinstance(value, str):
        for secret in _secret_echo_variants(secrets):
            value = value.replace(secret, "[private]")
    elif isinstance(value, dict):
        return {key: _redact_secret_echo(item, secrets) for key, item in value.items()}
    elif isinstance(value, list):
        return [_redact_secret_echo(item, secrets) for item in value]
    return value


# LLM: 缓存按授权隔离；命中仍重查当前端点，防撤权或动态 Gateway 端口变化泄漏旧响应；锁不覆盖网络。
# 函数用途: 处理单页抓取和格式化，并把实际凭据选择交给原工具审计。
def _execute_single_request(
    tool: WebFetchTool,
    request: _WebFetchRequest,
    max_chars: int,
) -> ToolHandlerOutcome:
    cache_key = request.method == "GET" and not request.headers and request.body_text is None and not request.credential_ref
    cached = _cache_get(tool, request.url, request.fmt) if cache_key and not request.refresh else None
    cache_hit = cached is not None
    if cached is not None:
        for url in dict.fromkeys((request.url, cached.url)):
            pin = tool._resolve_pin(url)
            if pin.error is not None:
                return pin.error
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
    if response.credential_selection is not None:
        result = replace(result, result_envelope={**result.result_envelope, 'credential_selection': response.credential_selection})
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
    resolved_by_host: dict[str, tuple[str, ...]] = {}
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
            resolve_pin=lambda hop_url: tool._resolve_pin(hop_url, resolved_by_host=resolved_by_host),
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


# LLM: 浅拷贝共享内存缓存不能共享授权，当前主机集合和私网开关均属于缓存身份。
# 函数用途: 构造响应缓存的权限隔离键，不保存凭据。
def _response_cache_key(tool: WebFetchTool, url: str, fmt: str) -> tuple[object, ...]:
    return url, fmt, tuple(tool.allowed_private_hosts or ()), tool.allow_private_resolution


# LLM: 按当前授权查缓存并清过期项；锁只保护字典，返回后调用者仍须重查网络门。
# 函数用途: 获取最近使用的缓存响应，并在使用时按 TTL 删除过期项。
def _cache_get(tool: WebFetchTool, url: str, fmt: str) -> RawResponseParts | None:
    if tool.cache_ttl_seconds <= 0:
        return None
    with tool._cache_lock:
        _expire_cache(tool)
        key = _response_cache_key(tool, url, fmt)
        item = tool._cache.get(key)
        if item is None:
            return None
        tool._cache.move_to_end(key)
        return item.response


# LLM: 每工具缓存最多 64 项与 8 MiB 正文，写入时绑定当前授权，不能变为跨权限响应库。
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
        key = _response_cache_key(tool, url, fmt)
        tool._cache[key] = CachedFetch(time.time() + tool.cache_ttl_seconds, response)
        tool._cache.move_to_end(key)
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
                "credential_ref": {"type": "string", "description": "request_credentials发现或返回的引用；群共享按当前发起人优先本人凭据，无本人凭据时用本群同站点可用账号。本人失败须先撤销，不能自动借更高权限；个人仍限原任务/监控，宿主注入且不返回明文。"},
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
