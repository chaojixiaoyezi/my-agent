
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any

from .models import BaseTool, ToolExecutionResult, ToolSpec
from .web import _has_control_chars, _normalize_url
from .web_http_helpers import scalar_text

_MAX_QUERY_CHARS = 512
_MAX_SEARCH_RESULTS = 10


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


@dataclass(frozen=True)
class _ProviderSearchResult:
    provider_name: str
    rows: list[dict[str, str]]
    failures: list[dict[str, str]]


class WebSearchProvider:
    """Small provider interface for web_search backends.

    这只是搜索来源的“插槽”。DuckDuckGo、Exa、Parallel、Tavily 这类来源都应该长得像
    `search(query, limit) -> list[dict]`，这样以后加来源不用改主工具流程。
    """

    name = "base"

    def search(self, query: str, limit: int) -> list[dict[str, str]]:
        raise NotImplementedError


class DuckDuckGoHtmlProvider(WebSearchProvider):
    """Built-in DuckDuckGo HTML provider that needs no external API key."""

    name = "duckduckgo_html"

    def __init__(self, *, timeout: int):
        self.timeout = timeout

    def search(self, query: str, limit: int) -> list[dict[str, str]]:
        url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": "MyAgent-WebSearch/1.0"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read().decode("utf-8", "replace")

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

    def __init__(self, *, timeout: int):
        self.timeout = timeout

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
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
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
    return max(1, min(configured_max, _MAX_SEARCH_RESULTS, limit))


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

    def __init__(self, *, max_results: int = 5, timeout: int, providers: list[WebSearchProvider] | None = None):
        self.max_results = max(1, min(_MAX_SEARCH_RESULTS, max_results))
        self.timeout = timeout
        # 多后端冗余(Bing 首选,DuckDuckGo 兜底):任一可用即可,告别单点故障。
        self.providers = providers or [
            BingHtmlProvider(timeout=timeout),
            DuckDuckGoHtmlProvider(timeout=timeout),
        ]
        self.spec = ToolSpec(
            name="web_search",
            category="web",
            effect="read_only",
            description="按关键词搜索公开网页，返回结构化候选来源 URL、标题和摘要。",
            use_cases=[
                "不知道具体 URL 时，先搜索公开来源候选，再用 web_fetch 读取",
                "研究论文、项目资料、文档和新闻入口时获取可核验链接",
            ],
            avoid_when=[
                "已经有确定 URL 或 API 地址时，直接用 web_fetch",
            ],
            keywords=["搜索", "网页搜索", "查找来源", "search", "web_search", "公开来源", "候选链接"],
            parameters={"query": "搜索关键词", "limit": "可选，最多返回多少条候选结果", "allowed_domains": "可选，只保留这些域名及其子域名的结果", "blocked_domains": "可选，排除这些域名及其子域名的结果"},
            parameter_details={
                "query": "必填，普通搜索关键词；工具只把它作为搜索引擎查询，不从自然语言推断任务事实。",
                "limit": f"可选，1 到 {self.max_results}；超过配置会自动收敛。",
                "allowed_domains": "可选字符串数组，例如 [\"github.com\"]；和 blocked_domains 不能同时使用。",
                "blocked_domains": "可选字符串数组，例如 [\"example.com\"]；和 allowed_domains 不能同时使用。",
            },
            examples=['{"tool": "web_search", "query": "open model reasoning paper arxiv", "limit": 5}', '{"tool": "web_search", "query": "project weekly ranking 20260105", "allowed_domains": ["example.com"]}'],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            request = _search_request_from_params(params, self.max_results)
        except ValueError as exc:
            return ToolExecutionResult("web_search", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")

        provider_result = _search_with_providers(self.providers, request.query, request.limit)
        if not provider_result.rows:
            payload = {"query": request.query, "provider_failures": provider_result.failures}
            if provider_result.failures:
                # 所有 provider 都抛异常(网络/HTTP/反爬挑战)→ 工具暂时不可用,可重试或改用 web_fetch
                return ToolExecutionResult("web_search", False, json.dumps(payload, ensure_ascii=False), error_code="TOOL_UNAVAILABLE")
            # provider 正常响应但查询无匹配→不是工具故障,是"搜索无结果";ok=True 避免被当成工具坏了而放弃
            payload["results"] = []
            payload["note"] = "搜索无匹配结果(也可能是来源限流返回空)。换更具体/不同关键词重试,或改用 web_fetch 直接抓已知 URL。"
            return ToolExecutionResult("web_search", True, json.dumps(payload, ensure_ascii=False, sort_keys=True))
        results = _normalized_provider_results(provider_result)
        results = _filter_search_results(results, allowed_domains=request.allowed_domains, blocked_domains=request.blocked_domains)
        payload = _search_payload(provider_result, request, _dedupe_search_results(results, request.limit))
        return ToolExecutionResult("web_search", True, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _search_request_from_params(params: dict[str, Any], max_results: int) -> _SearchRequest:
    allowed_domains = _normalize_domain_filter(params.get("allowed_domains"), name="allowed_domains")
    blocked_domains = _normalize_domain_filter(params.get("blocked_domains"), name="blocked_domains")
    if allowed_domains and blocked_domains:
        raise ValueError("allowed_domains 和 blocked_domains 不能同时使用")
    return _SearchRequest(
        query=_normalize_query(params.get("query")),
        limit=_search_limit(params.get("limit"), max_results),
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
    )


def _search_with_providers(providers: list[WebSearchProvider], query: str, limit: int) -> _ProviderSearchResult:
    failures: list[dict[str, str]] = []
    for provider in providers:
        try:
            rows = provider.search(query, limit)
        except urllib.error.HTTPError as exc:
            failures.append({"provider": provider.name, "error": f"HTTP {exc.code}"})
            continue
        except (urllib.error.URLError, TimeoutError, Exception) as exc:
            failures.append({"provider": provider.name, "error": exc.__class__.__name__})
            continue
        if rows:
            return _ProviderSearchResult(provider.name, rows, failures)
    return _ProviderSearchResult("", [], failures)


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


def _search_payload(
    provider_result: _ProviderSearchResult,
    request: _SearchRequest,
    results: list[_SearchResult],
) -> dict[str, Any]:
    return {
        "engine": provider_result.provider_name,
        "query": request.query,
        "provider_failures": provider_result.failures,
        "results": [
            {"source": item.source, "title": item.title, "url": item.url, "snippet": item.snippet}
            for item in results
        ],
    }
