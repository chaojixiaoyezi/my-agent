from __future__ import annotations

"""LLM contract: deterministic matcher and path resolver for memory routes.

这里做的是“代码先找路”。用户输入进来后，先用可解释的关键词和别名规则找候选，
再把候选分成必须读和可选读，减少长期规则只能靠模型自觉遵守的问题。
"""

import re

from .models import MemoryPathResolution, MemoryReadReceipt, MemoryRoute, MemoryRouteMatch

VALID_MODES = {"soft", "strict"}


def match_routes(
    query: str,
    routes: list[MemoryRoute],
    *,
    limit: int | None = 5,
) -> list[MemoryRouteMatch]:
    """LLM contract: ranks memory routes by deterministic keyword, alias, and topic hits.

    大白话：这不是向量搜索，也不是让模型判断，而是普通代码按词命中打分。
    好处是稳定、可测试、能解释：比如“命中别名 memory index，所以建议读这个规则文件”。
    """

    normalized_query = _normalize(query)
    if not normalized_query:
        return []

    hits: list[MemoryRouteMatch] = []
    for route in routes:
        score, reasons, terms = score_route(normalized_query, route)
        if score <= 0:
            continue
        hits.append(
            MemoryRouteMatch(
                route=route,
                score=score,
                reasons=reasons[:6],
                matched_terms=terms,
            )
        )
    hits.sort(key=lambda item: (-item.score, -item.route.priority, item.route.route_id))
    if limit is None or limit == 0:
        return hits
    if limit < 0:
        return []
    return hits[:limit]


def score_route(normalized_query: str, route: MemoryRoute) -> tuple[float, list[str], list[str]]:
    """LLM contract: scores one route against an already-normalized query string.

    大白话：别名比普通关键词更像“明确指路”，所以分数更高；topic 和 when_to_read
    也会参与，但权重较低。最后返回分数、理由和命中的词，方便日志解释。
    """

    score = 0.0
    reasons: list[str] = []
    matched_terms: list[str] = []

    for alias in route.aliases:
        term = _normalize(alias)
        if term and term in normalized_query:
            score += 8.0 + min(len(term) / 20, 1.5)
            reasons.append(f"命中别名“{alias}”")
            matched_terms.append(alias)

    for keyword in route.trigger_keywords:
        term = _normalize(keyword)
        if term and term in normalized_query:
            score += 6.0 + min(len(term) / 20, 1.0)
            reasons.append(f"命中关键词“{keyword}”")
            matched_terms.append(keyword)

    topic = _normalize(route.topic)
    if topic and topic in normalized_query:
        score += 3.0
        reasons.append(f"命中主题“{route.topic}”")
        matched_terms.append(route.topic)

    when_tokens = _tokens(route.when_to_read)
    query_tokens = set(_tokens(normalized_query))
    overlap = [token for token in when_tokens if token in query_tokens and len(token) >= 2]
    if score > 0 and overlap:
        score += min(len(overlap), 3) * 1.0
        reasons.append(f"命中阅读条件“{', '.join(overlap[:3])}”")
        matched_terms.extend(overlap[:3])

    if score > 0 and route.priority:
        score += min(max(route.priority, 0), 100) / 100

    return score, _dedupe(reasons), _dedupe(matched_terms)


def resolve_required_paths(
    matches: list[MemoryRouteMatch],
    *,
    mode: str = "soft",
    auto_read_limit: int = 3,
) -> MemoryPathResolution:
    """LLM contract: converts route matches into required and candidate authority paths.

    大白话：soft 模式只说“这些文件可能相关”；strict 模式会把前几个高分文件升级成
    “必须读”。`auto_read_limit=0` 表示本轮不自动读任何正文，只保留 matches 证据。
    后续正式任务、安全边界、工具权限这类场景就可以用 strict 卡住模型乱猜。
    """

    if mode not in VALID_MODES:
        raise ValueError(f"memory route mode must be one of {sorted(VALID_MODES)}, got {mode!r}")
    ordered = [match for match in matches if match.route.authority_path.strip()]
    if auto_read_limit <= 0:
        selected = []
    else:
        selected = ordered[:auto_read_limit]

    selected_paths = _unique_paths([match.route.authority_path for match in selected])
    all_paths = _unique_paths([match.route.authority_path for match in ordered])
    if mode == "strict":
        return MemoryPathResolution(
            mode=mode,
            required_read_paths=selected_paths,
            candidate_paths=[path for path in all_paths if path not in selected_paths],
            matches=matches,
        )
    return MemoryPathResolution(
        mode=mode,
        required_read_paths=[],
        candidate_paths=selected_paths,
        matches=matches,
    )


def build_read_receipt(
    match: MemoryRouteMatch,
    *,
    status: str = "planned",
    content_hash: str = "",
    elapsed_ms: float = 0.0,
    error: str = "",
) -> MemoryReadReceipt:
    """LLM contract: creates a read receipt stub for one matched route.

    大白话：当前模块不真的读文件，但可以先生成一张标准小票。以后运行时真的读了文件，
    只要补上 hash、耗时和状态，就能形成可审计链路。
    """

    return MemoryReadReceipt(
        route_id=match.route.route_id,
        authority_path=match.route.authority_path,
        status=status,
        reasons=match.reasons,
        content_hash=content_hash,
        elapsed_ms=elapsed_ms,
        error=error,
    ).mark_now()


def _normalize(text: str) -> str:
    """LLM contract: lowercases text and collapses whitespace for deterministic matching.

    大白话：英文大小写和多余空格不应该影响命中；中文不会被拆坏，仍然可以按短语匹配。
    """

    return re.sub(r"\s+", " ", text.lower()).strip()


def _tokens(text: str) -> list[str]:
    """LLM contract: extracts coarse English and Chinese tokens from text.

    大白话：这个 tokenizer 很轻量，只服务普通规则匹配。中文长句会额外切 2-4 字片段，
    让“任务恢复”这类短词也能从长句里被命中。
    """

    normalized = _normalize(text)
    raw_tokens = re.findall(r"[a-z0-9_./-]+|[\u4e00-\u9fff]+", normalized)
    expanded: list[str] = []
    for token in raw_tokens:
        expanded.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            for size in (2, 3, 4):
                for idx in range(0, max(len(token) - size + 1, 0)):
                    expanded.append(token[idx : idx + size])
    return _dedupe(expanded)


def _unique_paths(paths: list[str]) -> list[str]:
    """LLM contract: keeps the first occurrence of each authority path.

    大白话：多个 route 可能指向同一个正式规则文件，这里只保留一次，避免调用方重复读文件。
    """

    return _dedupe([path.strip() for path in paths if path.strip()])


def _dedupe(items: list[str]) -> list[str]:
    """LLM contract: deduplicates strings while preserving order.

    大白话：理由、命中词和路径都需要稳定顺序，这样测试输出和调试日志不会来回跳。
    """

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
