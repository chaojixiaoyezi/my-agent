"""memory_routing matcher 模块测试。

测试 match_routes、score_route、resolve_required_paths、build_read_receipt 等函数。
"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.memory_routing.matcher import (
    _dedupe,
    _normalize,
    _tokens,
    _unique_paths,
    build_read_receipt,
    match_routes,
    resolve_required_paths,
    score_route,
)
from agent_py_agent.agent.memory_routing.models import (
    MemoryRoute,
    MemoryRouteMatch,
)

# ── _normalize 测试 ────────────────────────────────────────────────────────

def test_normalize_lowercase():
    """测试小写转换。"""
    assert _normalize("Hello World") == "hello world"


def test_normalize_collapse_whitespace():
    """测试空白字符合并。"""
    assert _normalize("hello   world\n\ttest") == "hello world test"


def test_normalize_strip():
    """测试首尾空白去除。"""
    assert _normalize("  hello  ") == "hello"


def test_normalize_empty():
    """测试空字符串。"""
    assert _normalize("") == ""


def test_normalize_chinese():
    """测试中文保持不变。"""
    assert _normalize("你好世界") == "你好世界"


# ── _tokens 测试 ───────────────────────────────────────────────────────────

def test_tokens_english():
    """测试英文分词。"""
    tokens = _tokens("hello world test")
    assert "hello" in tokens
    assert "world" in tokens
    assert "test" in tokens


def test_tokens_chinese():
    """测试中文分词 2-4 字片段。"""
    tokens = _tokens("数据分析")
    assert "数据" in tokens
    assert "分析" in tokens


def test_tokens_mixed():
    """测试中英文混合。"""
    tokens = _tokens("hello world 数据分析")
    assert "hello" in tokens
    assert "world" in tokens
    assert "数据" in tokens


def test_tokens_empty():
    """测试空字符串。"""
    assert _tokens("") == []


def test_tokens_numbers_and_slashes():
    """测试数字和斜杠保留。"""
    tokens = _tokens("path/to/file v2.1")
    assert "path/to/file" in tokens
    assert "v2.1" in tokens


# ── _unique_paths 测试 ─────────────────────────────────────────────────────

def test_unique_paths_keeps_first():
    """测试去重保留第一次出现的路径。"""
    paths = ["/a", "/b", "/a", "/c"]
    result = _unique_paths(paths)
    assert result == ["/a", "/b", "/c"]


def test_unique_paths_removes_empty():
    """测试空白路径被移除。"""
    paths = ["/a", "", "  ", "/b"]
    result = _unique_paths(paths)
    assert result == ["/a", "/b"]


def test_unique_paths_empty_list():
    """测试空列表。"""
    assert _unique_paths([]) == []


# ── _dedupe 测试 ───────────────────────────────────────────────────────────

def test_dedupe_preserves_order():
    """测试去重保持顺序。"""
    items = ["a", "b", "a", "c", "b"]
    result = _dedupe(items)
    assert result == ["a", "b", "c"]


def test_dedupe_strips_whitespace():
    """测试去重前先去除空白。"""
    items = ["a", "  a  ", "b"]
    result = _dedupe(items)
    assert result == ["a", "b"]


def test_dedupe_empty_string():
    """测试空字符串被移除。"""
    items = ["a", "", "b"]
    result = _dedupe(items)
    assert result == ["a", "b"]


def test_dedupe_empty_list():
    """测试空列表。"""
    assert _dedupe([]) == []


# ── score_route 测试 ───────────────────────────────────────────────────────

def test_score_route_exact_alias_match():
    """测试精确别名匹配。"""
    route = MemoryRoute(
        route_id="r1",
        topic="测试主题",
        aliases=["数据分析"],
    )
    score, reasons, terms = score_route("数据分析", route)
    assert score > 0
    assert "精确命中别名" in reasons[0]


def test_score_route_fuzzy_alias_match():
    """测试模糊别名匹配。"""
    route = MemoryRoute(
        route_id="r1",
        topic="测试",
        aliases=["数据"],
    )
    score, reasons, terms = score_route("数据分析工具", route)
    assert score > 0
    assert any("模糊命中别名" in r for r in reasons)


def test_score_route_exact_keyword_match():
    """测试精确关键词匹配。"""
    route = MemoryRoute(
        route_id="r1",
        topic="测试",
        trigger_keywords=["分析"],
    )
    score, reasons, terms = score_route("分析", route)
    assert score > 0
    assert any("精确命中关键词" in r for r in reasons)


def test_score_route_fuzzy_keyword_match():
    """测试模糊关键词匹配。"""
    route = MemoryRoute(
        route_id="r1",
        topic="测试",
        trigger_keywords=["数据"],
    )
    score, reasons, terms = score_route("数据分析", route)
    assert score > 0


def test_score_route_topic_match():
    """测试主题匹配。"""
    route = MemoryRoute(
        route_id="r1",
        topic="数据处理",
        trigger_keywords=[],
    )
    score, reasons, terms = score_route("我想做数据处理", route)
    assert any("命中主题" in r for r in reasons)


def test_score_route_priority_boost():
    """测试优先级提升分数。"""
    route = MemoryRoute(
        route_id="r1",
        topic="测试",
        trigger_keywords=["测试"],
        priority=50,
    )
    score1, _, _ = score_route("测试", route)
    assert score1 > 16  # 基础分 + priority 加分


def test_score_route_no_match():
    """测试无匹配时返回零分。"""
    route = MemoryRoute(
        route_id="r1",
        topic="测试",
        trigger_keywords=["abc"],
    )
    score, reasons, terms = score_route("xyz完全无关", route)
    assert score == 0


# ── match_routes 测试 ─────────────────────────────────────────────────────

def test_match_routes_basic():
    """测试基本路由匹配。"""
    routes = [
        MemoryRoute(
            route_id="route-1",
            topic="数据分析",
            trigger_keywords=["分析", "统计"],
        ),
    ]
    hits = match_routes("需要数据分析工具", routes)
    assert len(hits) >= 1
    assert hits[0].route.route_id == "route-1"


def test_match_routes_inject_mode_never():
    """测试 inject_mode=never 的路由被跳过。"""
    routes = [
        MemoryRoute(
            route_id="never-route",
            topic="从不注入",
            trigger_keywords=["测试"],
            inject_mode="never",
        ),
    ]
    hits = match_routes("测试查询", routes)
    assert len(hits) == 0


def test_match_routes_inject_mode_always_without_query():
    """测试空查询时 inject_mode=always 被注入。"""
    routes = [
        MemoryRoute(
            route_id="always-route",
            topic="总是注入",
            trigger_keywords=[],
            inject_mode="always",
        ),
    ]
    hits = match_routes("", routes)
    assert len(hits) == 1
    assert "默认注入规则" in hits[0].reasons


def test_match_routes_limit():
    """测试结果数量限制。"""
    routes = [
        MemoryRoute(route_id=f"r{i}", topic="测试", trigger_keywords=["测试"])
        for i in range(10)
    ]
    hits = match_routes("测试", routes, limit=3)
    assert len(hits) == 3


def test_match_routes_negative_limit():
    """测试负数 limit 返回空列表。"""
    routes = [
        MemoryRoute(route_id="r1", topic="测试", trigger_keywords=["测试"]),
    ]
    hits = match_routes("测试", routes, limit=-1)
    assert hits == []


def test_match_routes_zero_limit():
    """测试零 limit 返回所有结果。"""
    routes = [
        MemoryRoute(route_id="r1", topic="测试", trigger_keywords=["测试"]),
        MemoryRoute(route_id="r2", topic="测试2", trigger_keywords=["测试"]),
    ]
    hits = match_routes("测试", routes, limit=0)
    assert len(hits) == 2


def test_match_routes_sorting():
    """测试结果按分数、优先级、route_id 排序。"""
    routes = [
        MemoryRoute(route_id="c", topic="测试", trigger_keywords=["测试"], priority=10),
        MemoryRoute(route_id="a", topic="测试", trigger_keywords=["测试"], priority=50),
        MemoryRoute(route_id="b", topic="测试", trigger_keywords=["测试"], priority=30),
    ]
    hits = match_routes("测试", routes)
    assert hits[0].route.route_id == "a"  # priority 最高


# ── resolve_required_paths 测试 ───────────────────────────────────────────

def test_resolve_required_paths_soft_mode():
    """测试 soft 模式返回候选路径。"""
    from agent_py_agent.agent.memory_routing.models import MemoryRoute

    route = MemoryRoute(
        route_id="r1",
        topic="测试",
        trigger_keywords=["测试"],
        authority_path="rules/test.md",
    )
    match = MemoryRouteMatch(route=route, score=10.0, reasons=["命中"])
    resolution = resolve_required_paths([match], mode="soft", auto_read_limit=3)

    assert resolution.mode == "soft"
    assert resolution.required_read_paths == []
    assert len(resolution.candidate_paths) == 1


def test_resolve_required_paths_strict_mode():
    """测试 strict 模式提升候选为必须读。"""
    route = MemoryRoute(
        route_id="r1",
        topic="测试",
        trigger_keywords=["测试"],
        authority_path="rules/test.md",
    )
    match = MemoryRouteMatch(route=route, score=10.0, reasons=["命中"])
    resolution = resolve_required_paths([match], mode="strict", auto_read_limit=3)

    assert resolution.mode == "strict"
    assert len(resolution.required_read_paths) == 1
    assert resolution.candidate_paths == []


def test_resolve_required_paths_auto_read_limit_zero():
    """测试 auto_read_limit=0 时不自动选路径。"""
    route = MemoryRoute(
        route_id="r1",
        topic="测试",
        trigger_keywords=["测试"],
        authority_path="rules/test.md",
    )
    match = MemoryRouteMatch(route=route, score=10.0, reasons=["命中"])
    resolution = resolve_required_paths([match], mode="strict", auto_read_limit=0)

    assert resolution.required_read_paths == []


def test_resolve_required_paths_invalid_mode():
    """测试无效 mode 抛出异常。"""
    route = MemoryRoute(route_id="r1", topic="测试", trigger_keywords=[])
    match = MemoryRouteMatch(route=route, score=1.0)

    with pytest.raises(ValueError, match="memory route mode must be one of"):
        resolve_required_paths([match], mode="invalid")


def test_resolve_required_paths_deduplicates_paths():
    """测试多条路由指向同一文件时去重。"""
    route1 = MemoryRoute(route_id="r1", topic="t", authority_path="rules/test.md")
    route2 = MemoryRoute(route_id="r2", topic="t", authority_path="rules/test.md")
    match1 = MemoryRouteMatch(route=route1, score=10.0)
    match2 = MemoryRouteMatch(route=route2, score=8.0)

    resolution = resolve_required_paths([match1, match2], mode="strict", auto_read_limit=5)

    # 同一路径只保留一次
    assert len(resolution.required_read_paths) == 1


# ── build_read_receipt 测试 ────────────────────────────────────────────────

def test_build_read_receipt_basic():
    """测试生成基本回执。"""
    route = MemoryRoute(
        route_id="receipt-route",
        topic="测试",
        trigger_keywords=["测试"],
        authority_path="rules/test.md",
    )
    match = MemoryRouteMatch(route=route, score=10.0, reasons=["命中测试"])

    receipt = build_read_receipt(match)

    assert receipt.route_id == "receipt-route"
    assert receipt.authority_path == "rules/test.md"
    assert receipt.status == "planned"
    assert receipt.reasons == ["命中测试"]
    assert receipt.read_at > 0


def test_build_read_receipt_custom_status():
    """测试自定义状态。"""
    route = MemoryRoute(route_id="r1", topic="t", authority_path="p.md")
    match = MemoryRouteMatch(route=route, score=1.0)
    receipt = build_read_receipt(match, status="read", content_hash="abc123")

    assert receipt.status == "read"
    assert receipt.content_hash == "abc123"


def test_build_read_receipt_error_status():
    """测试错误状态。"""
    route = MemoryRoute(route_id="r1", topic="t", authority_path="p.md")
    match = MemoryRouteMatch(route=route, score=1.0)
    receipt = build_read_receipt(match, status="error", error="文件不存在")

    assert receipt.status == "error"
    assert receipt.error == "文件不存在"


# ── 边界场景测试 ──────────────────────────────────────────────────────────

def test_score_route_empty_keywords():
    """测试空关键词列表。"""
    route = MemoryRoute(route_id="r1", topic="测试", trigger_keywords=[])
    score, reasons, terms = score_route("测试", route)
    # 只靠 topic 匹配
    assert score >= 0


def test_match_routes_empty_routes():
    """测试空路由列表。"""
    hits = match_routes("测试", [])
    assert hits == []


def test_resolve_required_paths_empty_matches():
    """测试空匹配列表。"""
    resolution = resolve_required_paths([], mode="soft")
    assert resolution.required_read_paths == []
    assert resolution.candidate_paths == []