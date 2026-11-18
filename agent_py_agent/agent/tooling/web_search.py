# LLM: 搜索候选仅是 provider/parser 观察，query_evidence 记录真实条件和覆盖未知，不把 HTML 空结果当来源真空。
# 模块用途: 查询公开搜索来源并回传候选及实际过滤/失败事实；复查仍由模型在原权限和预算内选择。

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from html import unescape
from html.parser import HTMLParser
from typing import Any

from ..common.cancellation import (
    ToolCancelled,
    cancellation_requested,
    register_cancellation_callback,
)
from ..contracts.gates.network_safety import _canonical_ip
from ..settings.model_provider_schema import ModelProfileError
from ..user_space.owner_egress import OwnerEgressPolicy, OwnerEgressPolicyError
from .host_output_redaction import credential_echo_variants
from .models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    TimeoutPolicy,
    ToolHandlerOutcome,
    ToolInvocationContext,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from .web import (
    _default_network_resolver,
    _has_control_chars,
    _network_safety_error,
    _normalize_url,
)
from .web_fetch_runtime import (
    FetchRawRequest,
    PinResult,
    egress_runtime_denial,
    fetch_raw_response,
    invocation_network_denial,
    owner_egress_pin,
    owner_http_scope,
)
from .web_http_helpers import scalar_text

# 搜索查询词最多 512 字符：超长拒绝，防畸形查询。
_MAX_QUERY_CHARS = 512
# 搜索最多返回 10 条结果：够用又不刷屏。
_MAX_SEARCH_RESULT_COUNT = 10
DEEPSEEK_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
_DEEPSEEK_MAX_QUERY_CHARS = 128


@dataclass(frozen=True)
class _SearchResult:
    title: str
    url: str
    snippet: str
    source: str = "duckduckgo_html"


@dataclass(frozen=True)
class _SearchRequest:
    query: str
    limit: int
    allowed_domains: list[str]
    blocked_domains: list[str]
    provider: str | None = None


# LLM: observations 只记实际 provider/parser 返回窗口，不携带网络认证或把候选窗口声明成互联网总数。
# 类用途: 保留当前搜索候选、逐来源失败与有界观察。
@dataclass(frozen=True)
class _ProviderSearchResult:
    provider_name: str
    rows: list[dict[str, str]]
    failures: list[dict[str, str]]
    observations: list[dict[str, Any]] = field(default_factory=list)


class WebSearchProvider:
    """Small provider interface for web_search backends.

    这只是搜索来源的“插槽”。DuckDuckGo、Exa、Parallel、Tavily 这类来源都应该长得像
    `search(query, limit) -> list[dict]`，这样以后加来源不用改主工具流程。
    """

    name = "base"

    def __init__(self, *, timeout: int = 30, owner_egress: Callable[[], OwnerEgressPolicy] | None = None):
        self.timeout = timeout
        self._read = partial(_read_search_response, owner_egress=owner_egress)

    def search(self, query: str, limit: int) -> list[dict[str, str]]:
        raise NotImplementedError


class _NoSearchRedirect(urllib.request.HTTPRedirectHandler):
    """官方请求带凭据，拒绝重定向，避免跨站转发。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 让原 HTTP 错误处理生成带响应句柄的 HTTPError，由调用方关闭。
        return None


class _OfficialSearchError(ValueError):
    def __init__(
        self, code: str, message: str, next_action: str, *,
        effect: str = "not_started", reported_code: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.reported_code = reported_code
        self.next_action = next_action
        self.effect = effect


class _SearchNetworkError(Exception):
    def __init__(self, outcome: ToolHandlerOutcome):
        super().__init__(outcome.output)
        self.outcome = outcome


class DeepSeekSearchProvider(WebSearchProvider):
    """宿主逐次取当前 owner 凭据，只发送固定的官方搜索请求。"""

    name = "deepseek"
    parser_version = "deepseek_search.v1"

    def __init__(
        self, *, timeout: int, credential: Callable[[], str] | None, enabled: Callable[[], bool] | None,
        owner_egress: Callable[[], OwnerEgressPolicy] | None = None,
    ):
        super().__init__(timeout=timeout, owner_egress=owner_egress)
        self._credential = credential
        self._enabled = enabled

    def search(self, query: str, limit: int) -> list[dict[str, str]]:
        if self._enabled is not None and not self._enabled():
            raise _OfficialSearchError(
                "TOOL_UNAVAILABLE", "部署配置 tool_deepseek_search_enabled=false，官方搜索请求未发送。",
                "请让部署管理员启用该开关后重试。"
            )
        if len(query) > _DEEPSEEK_MAX_QUERY_CHARS:
            raise _OfficialSearchError(
                "TOOL_INVALID_ARGUMENTS",
                f"DeepSeek 官方搜索的 query 为 {len(query)} 字符，最多支持 {_DEEPSEEK_MAX_QUERY_CHARS} 字符，未发送请求。",
                '缩短 query 后重试，例如 {"provider":"deepseek","query":"DeepSeek 官方搜索"}。'
            )
        if self._credential is None:
            raise _OfficialSearchError(
                "TOOL_UNAVAILABLE", "当前工具宿主没有绑定用户凭据目录，官方搜索请求未发送。",
                "请从当前用户的正常会话调用网页搜索。"
            )
        try:
            key = self._credential()
        except ModelProfileError as exc:
            raise _OfficialSearchError(
                "TOOL_UNAVAILABLE", f"DeepSeek 凭据解析失败 ({exc.reason})，官方搜索请求未发送。",
                str(exc) if exc.reason.startswith("credential_") else "请在 /model 检查当前用户的模型目录。"
            ) from exc
        except OSError as exc:
            raise _OfficialSearchError(
                "TOOL_UNAVAILABLE", f"读取用户 DeepSeek 凭据目录失败 ({type(exc).__name__})，官方搜索请求未发送。",
                "请让管理员核对当前用户模型目录的读取权限。"
            ) from exc
        return self._post(query, limit, key)

    def _post(self, query: str, limit: int, key: str) -> list[dict[str, str]]:
        request = urllib.request.Request(
            DEEPSEEK_SEARCH_URL,
            data=json.dumps({"query": query, "count": limit, "need_content": False}, ensure_ascii=False).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": "MyAgent-WebSearch/1.0"},
        )
        try:
            payload = json.loads(self._read(request, self.timeout, follow_redirects=False))
        except urllib.error.HTTPError as exc:
            exc.close()
            code, next_action = {
                400: ("TOOL_INVALID_ARGUMENTS", "请缩短或改写 query 后再试。"),
                401: ("TOOL_PERMISSION_DENIED", "请在 /model 更新当前用户保存的 DeepSeek 官方 API Key。"),
                402: ("TOOL_PERMISSION_DENIED", "请检查 DeepSeek API 账户余额。"),
                403: ("TOOL_PERMISSION_DENIED", "请检查该 DeepSeek API Key 的搜索接口权限。"),
                429: ("TOOL_UNAVAILABLE", "请等待 DeepSeek 服务商的限速窗口结束后再发起新请求。"),
            }.get(exc.code, ("TOOL_UNAVAILABLE", "请核对 DeepSeek 服务状态后再发起新请求。"))
            raise _OfficialSearchError(
                code, f"DeepSeek 官方搜索返回 HTTP {exc.code}，请求未成功，按上游规则不计费。",
                next_action, effect="failed", reported_code=f"DEEPSEEK_SEARCH_HTTP_{exc.code}",
            ) from exc
        except (OSError, ValueError) as exc:
            raise _OfficialSearchError(
                "TOOL_UNAVAILABLE", f"DeepSeek 官方搜索未取得有效响应 ({type(exc).__name__})，计费状态未知。",
                "请检查 DeepSeek 服务状态与用量，避免直接重复请求。", effect="unknown",
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise _OfficialSearchError(
                "TOOL_UNAVAILABLE", "DeepSeek 官方搜索返回缺少 results 数组的响应，计费状态未知。",
                "请核对 DeepSeek 服务状态与用量，再决定是否发起新请求。", effect="unknown",
            )
        secrets = credential_echo_variants((key,))
        rows = []
        for result in payload["results"][:limit]:
            if not isinstance(result, dict) or any(not isinstance(result.get(field), str)
                                                   for field in ("title", "url", "snippet")):
                raise _OfficialSearchError(
                    "TOOL_UNAVAILABLE", "DeepSeek 官方搜索响应条目缺少 title、url 或 snippet，计费状态未知。",
                    "请核对 DeepSeek 服务状态与用量，再决定是否发起新请求。", effect="unknown",
                )
            if any(secret in result["url"] for secret in secrets):
                continue
            try:
                url = _normalize_url(result["url"])
            except ValueError:
                continue
            row = {"source": self.name, "url": url}
            for field in ("title", "snippet"):
                value = result[field]
                for secret in secrets:
                    value = value.replace(secret, "[已隐藏]")
                row[field] = value
            rows.append(row)
        return rows


class DuckDuckGoHtmlProvider(WebSearchProvider):
    """Built-in DuckDuckGo HTML provider that needs no external API key."""

    name = "duckduckgo_html"
    parser_version = "duckduckgo_html.v1"

    def search(self, query: str, limit: int) -> list[dict[str, str]]:
        url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": "MyAgent-WebSearch/1.0"},
        )
        body = self._read(req, self.timeout)

        parser = _DuckDuckGoHtmlResultParser()
        parser.feed(body)
        return [
            {
                "source": item.source,
                "title": item.title,
                "url": item.url,
                "snippet": item.snippet,
            }
            for item in _dedupe_search_results(parser.results, limit)
        ]


# LLM: Bing HTML 搜索后端(R12-R15 实锤:唯一后端 DuckDuckGo 在常见网络环境
#   对脚本返回 202 反爬挑战页〔0 结果〕→ web_search 全程 TOOL_UNAVAILABLE → 模型
#   被迫退化成抓页面/瞎推断。Bing HTML 在同环境稳定返回真结果,免 API key)。
#   解析按 b_algo 结果块逐块进行——块内取 h2>a(标题+跳转链接)与块内 snippet,
#   保证标题/URL/摘要一一对应,不会跨结果错位(github 产物错位病的同源教训)。
#   provider 插槽设计不变,本类与 DuckDuckGo 并列,_search_with_providers 按序
#   fallback,任一可用即可。
class BingHtmlProvider(WebSearchProvider):
    """Built-in Bing HTML provider that needs no external API key."""

    name = "bing_html"
    parser_version = "bing_html.v1"

    def search(self, query: str, limit: int) -> list[dict[str, str]]:
        url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "setlang": "en"})
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        body = self._read(req, self.timeout)
        return _parse_bing_results(body, limit)


# 函数用途: Bing 的结果链接是 /ck/a 跳转(真实 URL 在 u 参数,a1+base64url),
#   解出真实 URL;非跳转链接原样返回;解不出返回空串(由调用方丢弃)。
def _decode_bing_redirect(href: str) -> str:
    href = unescape(href or "").strip()
    if "bing.com/ck/a" not in href:
        return href
    encoded = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("u", [""])[0]
    if not encoded.startswith("a1"):
        return ""
    payload = encoded[2:]
    try:
        return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8", "replace")
    except (ValueError, UnicodeDecodeError):
        return ""


# 函数用途: 把 Bing 结果页按 b_algo 块切开,块内提取标题/真实URL/摘要(逐块对应,
#   绝不跨结果错位);URL 过 _normalize_url 安全校验,解不出的块跳过。
def _parse_bing_results(html: str, limit: int) -> list[dict[str, str]]:
    starts = [m.start() for m in re.finditer(r'<li class="b_algo"', html)]
    rows: list[dict[str, str]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(html)
        block = html[start:end]
        link = re.search(r'<h2[^>]*>\s*<a[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        if not link:
            continue
        url = _decode_bing_redirect(link.group(1))
        try:
            url = _normalize_url(url) if url else ""
        except ValueError:
            url = ""
        title = _clean_search_text(re.sub(r"<[^>]+>", " ", link.group(2)))
        if not url or not title:
            continue
        snippet_match = re.search(
            r'<p class="b_lineclamp[^"]*"[^>]*>(.*?)</p>', block, re.DOTALL
        ) or re.search(r'<div class="b_caption"[^>]*>.*?<p[^>]*>(.*?)</p>', block, re.DOTALL)
        snippet = _clean_search_text(re.sub(r"<[^>]+>", " ", snippet_match.group(1))) if snippet_match else ""
        rows.append({"source": "bing_html", "title": title, "url": url, "snippet": snippet})
        if len(rows) >= limit:
            break
    return rows


class _DuckDuckGoHtmlResultParser(HTMLParser):

    def __init__(self) -> None:
        super().__init__()
        self.results: list[_SearchResult] = []
        self._active_link_url = ""
        self._active_link_text: list[str] = []
        self._active_snippet = False
        self._snippet_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key: value or "" for key, value in attrs}
        classes = set(attrs_map.get("class", "").split())
        if tag == "a" and "result__a" in classes:
            self._active_link_url = _search_result_url(attrs_map.get("href", ""))
            self._active_link_text = []
            return
        if "result__snippet" in classes:
            self._active_snippet = True
            self._snippet_text = []

    def handle_data(self, data: str) -> None:
        if self._active_link_url:
            self._active_link_text.append(data)
        elif self._active_snippet:
            self._snippet_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._active_link_url:
            title = _clean_search_text(" ".join(self._active_link_text))
            if title:
                self.results.append(_SearchResult(title=title, url=self._active_link_url, snippet=""))
            self._active_link_url = ""
            self._active_link_text = []
            return
        if self._active_snippet and tag in {"a", "div", "span"}:
            snippet = _clean_search_text(" ".join(self._snippet_text))
            if snippet and self.results and not self.results[-1].snippet:
                last = self.results[-1]
                self.results[-1] = _SearchResult(title=last.title, url=last.url, snippet=snippet)
            self._active_snippet = False
            self._snippet_text = []


def _normalize_query(value: Any) -> str:
    query = scalar_text(value, name="query", max_chars=_MAX_QUERY_CHARS)
    if _has_control_chars(query):
        raise ValueError("query 包含不支持的控制字符")
    return query


def _search_limit(value: Any, configured_max: int) -> int:
    try:
        limit = int(value) if value not in (None, "") else configured_max
    except (TypeError, ValueError):
        limit = configured_max
    return max(1, min(configured_max, _MAX_SEARCH_RESULT_COUNT, limit))


def _normalize_domain_filter(value: Any, *, name: str) -> list[str]:
    if value in (None, ""):
        return []
    raw_items = value if isinstance(value, list) else [value]
    domains: list[str] = []
    for raw in raw_items:
        domain = scalar_text(raw, name=name, max_chars=253).lower()
        if _has_control_chars(domain):
            raise ValueError(f"{name} 包含不支持的控制字符")
        if "://" in domain:
            host = urllib.parse.urlsplit(domain).hostname or ""
            domain = host.lower()
        domain = domain.strip(".")
        if not domain or "/" in domain or " " in domain:
            raise ValueError(f"{name} 必须是域名列表")
        domains.append(domain)
    return list(dict.fromkeys(domains))


def _domain_matches(hostname: str, domains: list[str]) -> bool:
    host = hostname.lower().strip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _filter_search_results(
    results: list[_SearchResult],
    *,
    allowed_domains: list[str],
    blocked_domains: list[str],
) -> list[_SearchResult]:
    if not allowed_domains and not blocked_domains:
        return results
    filtered: list[_SearchResult] = []
    for item in results:
        hostname = urllib.parse.urlsplit(item.url).hostname or ""
        if blocked_domains and _domain_matches(hostname, blocked_domains):
            continue
        if allowed_domains and not _domain_matches(hostname, allowed_domains):
            continue
        filtered.append(item)
    return filtered


def _search_result_url(value: str) -> str:
    if not value:
        return ""
    url = unescape(value.strip())
    parsed = urllib.parse.urlsplit(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        query = urllib.parse.parse_qs(parsed.query)
        url = str((query.get("uddg") or [""])[0])
    elif url.startswith("/l/"):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        url = str((query.get("uddg") or [""])[0])
    try:
        return _normalize_url(url)
    except ValueError:
        return ""


def _clean_search_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def _dedupe_search_results(results: list[_SearchResult], limit: int) -> list[_SearchResult]:
    seen: set[str] = set()
    deduped: list[_SearchResult] = []
    for item in results:
        if not item.url or item.url in seen:
            continue
        seen.add(item.url)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


class WebSearchTool(BaseTool):

    def __init__(
        self, *, max_results: int = 5, timeout: int, providers: list[WebSearchProvider] | None = None,
        deepseek_credential: Callable[[], str] | None = None, deepseek_enabled: Callable[[], bool] | None = None,
        owner_egress: Callable[[], OwnerEgressPolicy] | None = None,
    ):
        self.max_results = max(1, min(_MAX_SEARCH_RESULT_COUNT, max_results))
        self.owner_egress = owner_egress
        self.timeout = timeout
        # 多后端冗余(Bing 首选,DuckDuckGo 兜底):任一可用即可,告别单点故障。
        if providers and owner_egress is not None:
            raise ValueError("owner 出站策略仅支持使用宿主传输的搜索 provider")
        self.providers = providers or [
            BingHtmlProvider(timeout=timeout, owner_egress=owner_egress),
            DuckDuckGoHtmlProvider(timeout=timeout, owner_egress=owner_egress),
        ]
        self._deepseek = DeepSeekSearchProvider(
            timeout=timeout, credential=deepseek_credential, enabled=deepseek_enabled, owner_egress=owner_egress,
        )
        self.model_spec = ToolModelSpec(
            name="web_search",
            description="按关键词搜索公开网页，返回结构化候选来源 URL、标题和摘要。",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "必填，普通搜索关键词；只作为搜索引擎查询，不从自然语言推断任务事实。",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": self.max_results,
                        "description": f"最多返回多少条候选结果，范围 1-{self.max_results}。",
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["deepseek"],
                        "description": (
                            "仅用户要求使用 DeepSeek 官方 API 时传 deepseek。"
                            "由宿主使用当前用户保存的官方密钥，请求可能计费，query 最多 128 字符。"
                            "普通网页搜索省略本字段。"
                        ),
                    },
                    "allowed_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "只保留这些域名及其子域名；不能和 blocked_domains 同时使用。",
                    },
                    "blocked_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "排除这些域名及其子域名；不能和 allowed_domains 同时使用。",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            hints=ToolModelHints(
                category="web",
                use_cases=(
                    "不知道具体 URL 时，先搜索公开来源候选，再用 web_fetch 读取",
                    "研究论文、项目资料、文档和新闻入口时获取可核验链接",
                ),
                avoid_when=("已经有确定 URL 或 API 地址时，直接用 web_fetch",),
                keywords=("搜索", "网页搜索", "查找来源", "search", "web_search", "公开来源", "候选链接"),
                examples=(
                    '{"tool": "web_search", "query": "open model reasoning paper arxiv", "limit": 5}',
                    '{"tool": "web_search", "query": "project weekly ranking 20260105", "allowed_domains": ["example.com"]}',
                ),
            ),
        )
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy("read_only"),
            timeout_policy=TimeoutPolicy(self.timeout),
            concurrency_policy=ConcurrencyPolicy("parallel_safe"),
            resource_scopes=ResourceScopePolicy(
                parameter_names=("query",),
                parameter_kinds={"query": "logical"},
            ),
            output_policy=OutputPolicy(trust="external_data"),
        )

    def execute_scoped(self, params: dict[str, Any], context: ToolInvocationContext) -> ToolHandlerOutcome:
        denial = invocation_network_denial("web_search", context)
        return denial if denial is not None else self.execute(params)

    # 实际条件及查询观察进入原结果，复查不自动执行。
    # 函数用途: 执行一次已声明网页搜索并如实报告覆盖未知。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        try:
            with owner_http_scope(self.owner_egress):
                return self._execute_http(params)
        except OwnerEgressPolicyError as exc:
            return egress_runtime_denial("web_search", exc)

    def _execute_http(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        try:
            request = _search_request_from_params(params, self.max_results)
        except ValueError as exc:
            return ToolHandlerOutcome("web_search", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")

        try:
            provider_result = _search_with_providers(
                [self._deepseek] if request.provider == self._deepseek.name else self.providers,
                request.query,
                request.limit,
                allowed_domains=request.allowed_domains,
                blocked_domains=request.blocked_domains,
            )
        except _SearchNetworkError as exc:
            return exc.outcome
        except _OfficialSearchError as exc:
            return ToolHandlerOutcome(
                "web_search", False, str(exc), error_code=exc.code,
                effect_outcome=exc.effect, next_action=exc.next_action, reported_error_code=exc.reported_code,
            )
        except ToolCancelled:
            return ToolHandlerOutcome(
                "web_search",
                False,
                "CANCELLED: 网页搜索已取消。",
                error_code="CANCELLED",
            )
        if not provider_result.rows:
            payload = {"query": request.query, "provider_failures": provider_result.failures,
                       "query_evidence": _web_query_evidence(provider_result, request, 0)}
            payload["query"] = payload["query_evidence"]["conditions"]["query"]
            if request.provider is not None:
                payload["engine"] = request.provider
            if provider_result.failures:
                # 所有 provider 都抛异常(网络/HTTP/反爬挑战)→ 工具暂时不可用,可重试或改用 web_fetch
                return ToolHandlerOutcome("web_search", False, json.dumps(payload, ensure_ascii=False), error_code="TOOL_UNAVAILABLE",
                                          result_envelope={"query_evidence": payload["query_evidence"]})
            # provider 正常响应但查询无匹配→不是工具故障,是"搜索无结果";ok=True 避免被当成工具坏了而放弃
            payload["query"] = payload["query_evidence"]["conditions"]["query"]
            payload["results"] = []
            if request.provider == self._deepseek.name:
                payload["note"] = "DeepSeek 官方搜索返回的候选窗口无符合条件的结果。可调整查询词或域名筛选后重新搜索。"
            else:
                payload["note"] = "搜索无匹配结果(也可能是来源限流返回空)。换更具体/不同关键词重试,或改用 web_fetch 直接抓已知 URL。"
            return ToolHandlerOutcome("web_search", True, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                                      result_envelope={"query_evidence": payload["query_evidence"]})
        results = _normalized_provider_results(provider_result)
        results = _filter_search_results(results, allowed_domains=request.allowed_domains, blocked_domains=request.blocked_domains)
        payload = _search_payload(provider_result, request, _dedupe_search_results(results, request.limit))
        return ToolHandlerOutcome("web_search", True, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                                      result_envelope={"query_evidence": payload["query_evidence"]})


def _search_request_from_params(params: dict[str, Any], max_results: int) -> _SearchRequest:
    allowed_domains = _normalize_domain_filter(params.get("allowed_domains"), name="allowed_domains")
    blocked_domains = _normalize_domain_filter(params.get("blocked_domains"), name="blocked_domains")
    if allowed_domains and blocked_domains:
        raise ValueError("allowed_domains 和 blocked_domains 不能同时使用")
    provider = params.get("provider")
    if "provider" in params and provider != "deepseek":
        raise ValueError('provider 只接受字符串 "deepseek"；普通搜索请省略该字段，未发送请求。')
    return _SearchRequest(
        query=_normalize_query(params.get("query")),
        limit=_search_limit(params.get("limit"), max_results),
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
        provider=provider,
    )


# LLM: provider 是否命中必须在域名过滤之后判断；未满足用户范围时继续下个真实来源。
# 函数用途: 搜索并过滤来源，避免第一家返回无关域名就挡住后面的有效结果。
def _search_with_providers(
    providers: list[WebSearchProvider], query: str, limit: int, *,
    allowed_domains: list[str] | None = None, blocked_domains: list[str] | None = None,
) -> _ProviderSearchResult:
    failures: list[dict[str, str]] = []
    observations: list[dict[str, Any]] = []
    denied: _SearchNetworkError | None = None
    for provider in providers:
        if cancellation_requested():
            raise ToolCancelled("cancelled")
        try:
            rows = provider.search(query, limit)
        except (ToolCancelled, _OfficialSearchError):
            raise
        except _SearchNetworkError as exc:
            if isinstance(provider, DeepSeekSearchProvider):
                raise
            denied = exc
            failures.append({"provider": provider.name, "error": exc.outcome.reported_error_code})
            continue
        except urllib.error.HTTPError as exc:
            failures.append({"provider": provider.name, "error": f"HTTP {exc.code}"})
            continue
        except (urllib.error.URLError, TimeoutError, Exception) as exc:
            failures.append({"provider": provider.name, "error": exc.__class__.__name__})
            continue
        observations.append({"provider": provider.name, "returned_count": len(rows),
                             "parser_version": str(getattr(provider, "parser_version", "unknown"))})
        if rows:
            candidate = _ProviderSearchResult(provider.name, rows, failures)
            valid = _filter_search_results(_normalized_provider_results(candidate),
                                            allowed_domains=allowed_domains or [], blocked_domains=blocked_domains or [])
            urls = {item.url for item in valid}
            filtered = [row for row in rows if str(row.get("url", "")) in urls]
            if filtered:
                return _ProviderSearchResult(provider.name, filtered, failures, observations)
    if denied is not None and not observations:
        raise denied
    return _ProviderSearchResult("", [], failures, observations)


# LLM: 搜索 HTML 与抓取正文一样必须有读取上限；超过容量报告来源失败，不能先读完再切片。
# 函数用途: 可取消地读取最多 2 MiB 搜索页面，避免异常上游撑爆内存。
def _read_search_response(
    request: urllib.request.Request, timeout: int, *, follow_redirects: bool = True,
    owner_egress: Callable[[], OwnerEgressPolicy] | None = None,
) -> str:
    if cancellation_requested():
        raise ToolCancelled("cancelled")
    if owner_egress is not None:
        try:
            restricted = owner_egress().mode != "unrestricted"
        except OwnerEgressPolicyError as exc:
            raise _SearchNetworkError(ToolHandlerOutcome(
                "web_search", False, str(exc), error_code="TOOL_UNAVAILABLE", reported_error_code=exc.code,
                effect_outcome="not_started", next_action=exc.next_action,
            )) from exc
        if restricted:
            return _read_pinned_search(request, timeout, follow_redirects=follow_redirects, owner_egress=owner_egress)
    open_response = urllib.request.urlopen
    if not follow_redirects:
        open_response = urllib.request.build_opener(_NoSearchRedirect()).open
    with open_response(request, timeout=timeout) as response:
        with register_cancellation_callback(response.close):
            try:
                body = response.read(2 * 1024 * 1024 + 1)
                if len(body) > 2 * 1024 * 1024:
                    raise ValueError("搜索响应超过 2 MiB 上限")
            except (OSError, ValueError) as exc:
                if cancellation_requested():
                    raise ToolCancelled("cancelled") from exc
                raise
    if cancellation_requested():
        raise ToolCancelled("cancelled")
    return body.decode("utf-8", "replace")



def _read_pinned_search(
    request: urllib.request.Request, timeout: int, *, follow_redirects: bool,
    owner_egress: Callable[[], OwnerEgressPolicy],
) -> str:
    def resolve_pin(url: str) -> PinResult:
        try:
            url = _normalize_url(url)
            host = urllib.parse.urlsplit(url).hostname or ""
            literal_ip = _canonical_ip(host)
            ips = (literal_ip,) if literal_ip else _default_network_resolver(host)
        except (ValueError, OSError):
            ips = ()
        safety_error = _network_safety_error("web_search", url, lambda _host: ips)
        return PinResult(None, safety_error) if safety_error else owner_egress_pin("web_search", url, ips, owner_egress)

    def status_error(tool, exc, _max_chars):
        return ToolHandlerOutcome(tool, False, "", error_code="NETWORK_REQUEST_FAILED")

    response = fetch_raw_response(
        FetchRawRequest(
            tool="web_search", url=request.full_url, method=request.get_method(),
            headers=dict(request.header_items()), data=request.data, timeout=timeout,
            max_bytes=2 * 1024 * 1024, follow_redirects=follow_redirects,
        ),
        format_http_error=status_error, resolve_pin=resolve_pin,
    )
    if isinstance(response, ToolHandlerOutcome):
        if response.error_code == "CANCELLED":
            raise ToolCancelled("cancelled")
        status = response.result_envelope.get("http_status")
        if status is not None:
            raise urllib.error.HTTPError(request.full_url, status, "", {}, None)
        raise _SearchNetworkError(response)
    if response.status >= 300:
        raise urllib.error.HTTPError(response.url, response.status, "", {}, None)
    return response.body.decode("utf-8", "replace")

def _normalized_provider_results(provider_result: _ProviderSearchResult) -> list[_SearchResult]:
    return [
        _SearchResult(
            title=str(item.get("title", "")),
            url=str(item.get("url", "")),
            snippet=str(item.get("snippet") or item.get("description") or ""),
            source=str(item.get("source") or provider_result.provider_name),
        )
        for item in provider_result.rows
    ]


# LLM: 公开查询条件脱敏后与原候选共用一个回执，不另存解析账本。
# 函数用途: 渲染网页搜索候选及实际查询观察。
def _search_payload(
    provider_result: _ProviderSearchResult,
    request: _SearchRequest,
    results: list[_SearchResult],
) -> dict[str, Any]:
    return {
        "query_evidence": _web_query_evidence(provider_result, request, len(results)),
        "engine": provider_result.provider_name,
        "query": _web_query_evidence(provider_result, request, len(results))["conditions"]["query"],
        "provider_failures": provider_result.failures,
        "results": [
            {"source": item.source, "title": item.title, "url": item.url, "snippet": item.snippet}
            for item in results
        ],
    }


# LLM: provider 的返回窗口不是互联网总数；未知 parser/失败单列，不从页面正文断言完整空集合。
# 函数用途: 汇总真实网页搜索条件和 provider 窗口观察，不额外执行网络查询。
def _web_query_evidence(provider_result: _ProviderSearchResult, request: _SearchRequest, returned: int) -> dict:
    from .query_evidence import QueryObservation, query_evidence
    evidence = query_evidence(QueryObservation(
        source="web_search", conditions={"query": request.query, "limit": request.limit,
            "allowed_domains": request.allowed_domains, "blocked_domains": request.blocked_domains},
        returned_count=returned, observed_count=len(provider_result.rows), coverage="provider_window",
        parser_version="web_search_provider_projection.v1",
        error_code="TOOL_UNAVAILABLE" if not returned and provider_result.failures else '',
        count_unit="candidate_urls",
    ))
    if request.provider is not None:
        evidence["conditions"]["provider"] = request.provider
    evidence["provider_observations"] = provider_result.observations
    evidence["provider_failures"] = provider_result.failures
    return evidence
