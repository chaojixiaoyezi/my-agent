# LLM: Memory routing module; keep context selection and read-receipt records stable.
# 模块用途: 根据任务上下文选择可注入记忆，并记录读取路径。

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .models import MemoryPathResolution, MemoryReadReceipt, MemoryRoute, MemoryRouteMatch

VALID_MODES = {"soft", "strict"}


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 match_routes 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 match routes 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
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


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 score_route 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 基于规则或事件字段计算 score route 的判定结果，避免把推测当作事实写入。
def score_route(normalized_query: str, route: MemoryRoute) -> tuple[float, list[str], list[str]]:
    reasons: list[str] = []
    matched_terms: list[str] = []
    acc = _ScoreAccumulator(reasons, matched_terms)
    alias_score, alias_exact, alias_fuzzy = _score_terms(
        normalized_query,
        route.aliases,
        spec=_TermScoreSpec("精确命中别名", "模糊命中别名", 20.0, 10.0, 2.0, 1.5),
        acc=acc,
    )
    keyword_score, keyword_exact, keyword_fuzzy = _score_terms(
        normalized_query,
        route.trigger_keywords,
        spec=_TermScoreSpec("精确命中关键词", "模糊命中关键词", 16.0, 8.0, 1.5, 1.0),
        acc=acc,
    )
    score = alias_score + keyword_score
    exact_hit = alias_exact or keyword_exact
    fuzzy_hit = alias_fuzzy or keyword_fuzzy

    topic_score, topic_hit = _score_topic(normalized_query, route, acc)
    score += topic_score
    fuzzy_hit = fuzzy_hit or topic_hit
    score += _score_when_overlap(normalized_query, route, score, acc)
    score = _apply_priority_score(score, route.priority)
    if score > 0 and route.inject_mode == "always" and not exact_hit and not fuzzy_hit:
        score += 0.5
        reasons.append("默认注入规则")

    return score, _dedupe(reasons), _dedupe(matched_terms)


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 resolve_required_paths 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 resolve required paths 在当前模块中的核心转换或协调步骤，衔接 memory routing 读取项目规则、路径和上下文片段来决定注入范围。
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


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 build_read_receipt 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 build read receipt 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
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


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 ReadReceiptParams 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ReadReceiptParams 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class ReadReceiptParams:
    # LLM: read receipt status fields are grouped for future routing evidence fields.
    status: str = "planned"
    content_hash: str = ""
    elapsed_ms: float = 0.0
    error: str = ""


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _TermScoreSpec 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _TermScoreSpec 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _TermScoreSpec:
    exact_reason: str
    fuzzy_reason: str
    exact_weight: float
    fuzzy_weight: float
    exact_bonus: float
    fuzzy_bonus: float


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _ScoreAccumulator 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _ScoreAccumulator 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _ScoreAccumulator:
    reasons: list[str]
    matched_terms: list[str]


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _match_one_route 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 match one route 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
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


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _always_route_match 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 always route match 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
def _always_route_match(route: MemoryRoute) -> MemoryRouteMatch | None:
    if route.inject_mode != "always":
        return None
    score, reasons, terms = _always_route_score(route)
    return MemoryRouteMatch(route=route, score=score, reasons=reasons, matched_terms=terms)


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _always_route_score 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 基于规则或事件字段计算 always route score 的判定结果，避免把推测当作事实写入。
def _always_route_score(route: MemoryRoute) -> tuple[float, list[str], list[str]]:
    return 1.0 + min(max(route.priority, 0), 100) / 100, ["默认注入规则"], []


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _score_terms 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 基于规则或事件字段计算 score terms 的判定结果，避免把推测当作事实写入。
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


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _score_topic 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 基于规则或事件字段计算 score topic 的判定结果，避免把推测当作事实写入。
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


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _score_when_overlap 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 基于规则或事件字段计算 score when overlap 的判定结果，避免把推测当作事实写入。
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


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _apply_priority_score 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 基于规则或事件字段计算 apply priority score 的判定结果，避免把推测当作事实写入。
def _apply_priority_score(score: float, priority: int) -> float:
    if score <= 0 or not priority:
        return score
    return score + min(max(priority, 0), 100) / 100


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _limit_matches 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 limit matches 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
def _limit_matches(hits: list[MemoryRouteMatch], limit: int | None) -> list[MemoryRouteMatch]:
    if limit is None or limit == 0:
        return hits
    if limit < 0:
        return []
    return hits[:limit]


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _normalize 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 normalize 涉及的字段，让后续匹配和存储使用同一形态。
def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _tokens 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 tokens 在当前模块中的核心转换或协调步骤，衔接 memory routing 读取项目规则、路径和上下文片段来决定注入范围。
def _tokens(text: str) -> list[str]:
    normalized = _normalize(text)
    raw_tokens = re.findall(r"[a-z0-9_./-]+|[\u4e00-\u9fff]+", normalized)
    expanded: list[str] = []
    for token in raw_tokens:
        expanded.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            expanded.extend(_chinese_ngrams(token))
    return _dedupe(expanded)


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _chinese_ngrams 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 chinese ngrams 在当前模块中的核心转换或协调步骤，衔接 memory routing 读取项目规则、路径和上下文片段来决定注入范围。
def _chinese_ngrams(token: str) -> list[str]:
    grams: list[str] = []
    for size in (2, 3, 4):
        grams.extend(token[idx : idx + size] for idx in range(0, max(len(token) - size + 1, 0)))
    return grams


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _unique_paths 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 unique paths 涉及的字段，让后续匹配和存储使用同一形态。
def _unique_paths(paths: list[str]) -> list[str]:
    return _dedupe([path.strip() for path in paths if path.strip()])


# LLM: memory routing 读取项目规则、路径和上下文片段来决定注入范围；修改 _dedupe 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 dedupe 涉及的字段，让后续匹配和存储使用同一形态。
def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = item.strip() if isinstance(item, str) else str(item)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result
