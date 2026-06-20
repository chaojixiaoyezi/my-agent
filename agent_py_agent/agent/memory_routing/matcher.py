
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from ..common.value_parsing import dedupe_strings
from .models import MemoryPathResolution, MemoryReadReceipt, MemoryRoute, MemoryRouteMatch

VALID_MODES = {"soft", "strict"}


def match_routes(
    query: str,
    routes: list[MemoryRoute],
    *,
    limit: int | None = 5,
) -> list[MemoryRouteMatch]:
    normalized_query = _normalize(query)

    hits = [
        match
        for route in routes
        if (match := _match_one_route(normalized_query, route)) is not None
    ]
    hits.sort(key=lambda item: (-item.score, -item.route.priority, item.route.route_id))
    return _limit_matches(hits, limit)


def score_route(normalized_query: str, route: MemoryRoute) -> tuple[float, list[str], list[str]]:
    reasons: list[str] = []
    matched_terms: list[str] = []
    acc = _ScoreAccumulator(reasons, matched_terms)
    related_term_score, related_term_exact, related_term_fuzzy = _score_terms(
        normalized_query,
        route.related_terms,
        spec=_TermScoreSpec("精确命中相关词", "模糊命中相关词", 20.0, 10.0, 2.0, 1.5),
        acc=acc,
    )
    keyword_score, keyword_exact, keyword_fuzzy = _score_terms(
        normalized_query,
        route.trigger_keywords,
        spec=_TermScoreSpec("精确命中关键词", "模糊命中关键词", 16.0, 8.0, 1.5, 1.0),
        acc=acc,
    )
    score = related_term_score + keyword_score
    exact_hit = related_term_exact or keyword_exact
    fuzzy_hit = related_term_fuzzy or keyword_fuzzy

    topic_score, topic_hit = _score_topic(normalized_query, route, acc)
    score += topic_score
    fuzzy_hit = fuzzy_hit or topic_hit
    score += _score_when_overlap(normalized_query, route, score, acc)
    score = _apply_priority_score(score, route.priority)
    if score > 0 and route.inject_mode == "always" and not exact_hit and not fuzzy_hit:
        score += 0.5
        reasons.append("默认注入规则")

    return score, dedupe_strings(reasons), dedupe_strings(matched_terms)


def resolve_required_paths(
    matches: list[MemoryRouteMatch],
    *,
    mode: str = "soft",
    auto_read_limit: int = 3,
) -> MemoryPathResolution:
    if mode not in VALID_MODES:
        raise ValueError(f"memory route mode must be one of {sorted(VALID_MODES)}, got {mode!r}")

    ordered = [match for match in matches if match.route.authority_file()]
    selected = [] if auto_read_limit <= 0 else ordered[:auto_read_limit]
    selected_paths = _unique_paths([match.route.authority_file() for match in selected])
    all_paths = _unique_paths([match.route.authority_file() for match in ordered])
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
    params: ReadReceiptParams | None = None,
    status: str = "planned",
    content_hash: str = "",
    elapsed_ms: float = 0.0,
    error: str = "",
) -> MemoryReadReceipt:
    values = params or ReadReceiptParams(status, content_hash, elapsed_ms, error)
    return MemoryReadReceipt(
        route_id=match.route.route_id,
        authority_path=match.route.authority_file(),
        status=str(values.status),
        reasons=match.reasons,
        content_hash=str(values.content_hash),
        elapsed_ms=float(values.elapsed_ms),
        error=str(values.error),
    ).mark_now()


@dataclass(frozen=True)
class ReadReceiptParams:
    status: str = "planned"
    content_hash: str = ""
    elapsed_ms: float = 0.0
    error: str = ""


@dataclass(frozen=True)
class _TermScoreSpec:
    exact_reason: str
    fuzzy_reason: str
    exact_weight: float
    fuzzy_weight: float
    exact_bonus: float
    fuzzy_bonus: float


@dataclass(frozen=True)
class _ScoreAccumulator:
    reasons: list[str]
    matched_terms: list[str]


def _match_one_route(normalized_query: str, route: MemoryRoute) -> MemoryRouteMatch | None:
    if route.inject_mode == "never":
        return None
    if not normalized_query:
        return _always_route_match(route)

    score, reasons, terms = score_route(normalized_query, route)
    if score <= 0 and route.inject_mode == "always":
        score, reasons, terms = _always_route_score(route)
    if score <= 0:
        return None
    return MemoryRouteMatch(route=route, score=score, reasons=reasons[:6], matched_terms=terms)


def _always_route_match(route: MemoryRoute) -> MemoryRouteMatch | None:
    if route.inject_mode != "always":
        return None
    score, reasons, terms = _always_route_score(route)
    return MemoryRouteMatch(route=route, score=score, reasons=reasons, matched_terms=terms)


def _always_route_score(route: MemoryRoute) -> tuple[float, list[str], list[str]]:
    return 1.0 + min(max(route.priority, 0), 100) / 100, ["默认注入规则"], []


def _score_terms(
    normalized_query: str,
    terms: Sequence[str],
    *,
    spec: _TermScoreSpec,
    acc: _ScoreAccumulator,
) -> tuple[float, bool, bool]:
    score = 0.0
    exact_hit = False
    fuzzy_hit = False
    for original in terms:
        term = _normalize(original)
        if not term:
            continue
        if term == normalized_query:
            score += spec.exact_weight + min(len(term) / 20, spec.exact_bonus)
            acc.reasons.append(f"{spec.exact_reason}: {original}")
            acc.matched_terms.append(original)
            exact_hit = True
            continue
        if term in normalized_query:
            score += spec.fuzzy_weight + min(len(term) / 20, spec.fuzzy_bonus)
            acc.reasons.append(f"{spec.fuzzy_reason}: {original}")
            acc.matched_terms.append(original)
            fuzzy_hit = True
    return score, exact_hit, fuzzy_hit


def _score_topic(
    normalized_query: str,
    route: MemoryRoute,
    acc: _ScoreAccumulator,
) -> tuple[float, bool]:
    topic = _normalize(route.topic)
    if not topic or topic not in normalized_query:
        return 0.0, False
    acc.reasons.append(f"命中主题: {route.topic}")
    acc.matched_terms.append(route.topic)
    return 3.0, True


def _score_when_overlap(
    normalized_query: str,
    route: MemoryRoute,
    current_score: float,
    acc: _ScoreAccumulator,
) -> float:
    if current_score <= 0:
        return 0.0
    query_tokens = set(_tokens(normalized_query))
    overlap = [token for token in _tokens(route.when_to_read) if token in query_tokens and len(token) >= 2]
    if not overlap:
        return 0.0
    top_overlap = overlap[:3]
    acc.reasons.append(f"when-to-read overlap: {', '.join(top_overlap)}")
    acc.matched_terms.extend(top_overlap)
    return float(min(len(overlap), 3))


def _apply_priority_score(score: float, priority: int) -> float:
    if score <= 0 or not priority:
        return score
    return score + min(max(priority, 0), 100) / 100


def _limit_matches(hits: list[MemoryRouteMatch], limit: int | None) -> list[MemoryRouteMatch]:
    if limit is None or limit == 0:
        return hits
    if limit < 0:
        return []
    return hits[:limit]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _tokens(text: str) -> list[str]:
    normalized = _normalize(text)
    # \u4efb\u610f\u811a\u672c\u8bcd\u5b57\u7b26(\u9664 CJK)+ ./- \u8def\u5f84\u5b57\u7b26 \u2192 \u975e\u4e2d\u82f1\u8bed\u8a00\u4e0d\u518d\u96f6 token(\u5ba1\u8ba1 #7);CJK \u4ecd\u5355\u5217
    raw_tokens = re.findall(r"(?:[^\W\u4e00-\u9fff]|[./-])+|[\u4e00-\u9fff]+", normalized)
    expanded: list[str] = []
    for token in raw_tokens:
        expanded.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            expanded.extend(_chinese_ngrams(token))
    return dedupe_strings(expanded)


def _chinese_ngrams(token: str) -> list[str]:
    grams: list[str] = []
    for size in (2, 3, 4):
        grams.extend(token[idx : idx + size] for idx in range(0, max(len(token) - size + 1, 0)))
    return grams


def _unique_paths(paths: list[str]) -> list[str]:
    return dedupe_strings([path.strip() for path in paths if path.strip()])
