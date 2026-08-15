"""memory_routing models 模块测试。

测试 MemoryRoute、MemoryRouteMatch、MemoryPathResolution、MemoryReadReceipt 等数据类。
"""
from __future__ import annotations

import time

import pytest

from agent_py_agent.agent.common.value_parsing import dedupe_strings
from agent_py_agent.agent.memory_routing.models import (
    MemoryPathResolution,
    MemoryReadReceipt,
    MemoryRoute,
    MemoryRouteMatch,
)

# ── MemoryRoute 测试 ────────────────────────────────────────────────────────

def test_memory_route_basic():
    """测试基本 MemoryRoute 创建。"""
    route = MemoryRoute(route_id="test-route", topic="测试主题")
    assert route.route_id == "test-route"
    assert route.topic == "测试主题"
    assert route.trigger_keywords == []
    assert route.related_terms == []


def test_memory_route_default_values():
    """测试默认值。"""
    route = MemoryRoute(route_id="r1", topic="t1")
    assert route.when_to_read == ""
    assert route.authority_path == ""
    assert route.inject_mode == "on_hit"
    assert route.scope == "global"
    assert route.priority == 0


def test_memory_route_full_fields():
    """测试所有字段。"""
    route = MemoryRoute(
        route_id="full-route",
        topic="完整主题",
        trigger_keywords=["kw1", "kw2"],
        related_terms=["related_term1", "related_term2"],
        when_to_read="需要数据分析时",
        authority_path="rules/analysis.md",
        inject_mode="always",
        scope="project",
        priority=80,
        stale_check="每季度复核",
        last_verified_at="2024-01-01",
        source_file="indexes/routes.json",
        source_path="/path/to/index",
    )
    assert route.trigger_keywords == ["kw1", "kw2"]
    assert route.related_terms == ["related_term1", "related_term2"]
    assert route.inject_mode == "always"
    assert route.priority == 80


def test_memory_route_authority_file_source_file_priority():
    """测试 authority_file 优先返回 source_file。"""
    route1 = MemoryRoute(route_id="r1", topic="t", source_file="new.md", authority_path="old.md")
    assert route1.authority_file() == "new.md"

    route2 = MemoryRoute(route_id="r2", topic="t", authority_path="old.md")
    assert route2.authority_file() == "old.md"


def test_memory_route_authority_file_strips_whitespace():
    """测试 authority_file 去除空白。"""
    route = MemoryRoute(route_id="r1", topic="t", source_file="  rules/test.md  ")
    assert route.authority_file() == "rules/test.md"


def test_memory_route_trigger_terms():
    """测试 trigger_terms 合并关键词和相关词。"""
    route = MemoryRoute(
        route_id="r1",
        topic="t",
        trigger_keywords=["kw1"],
        related_terms=["related_term1"],
    )
    terms = route.trigger_terms()
    assert "kw1" in terms
    assert "related_term1" in terms


def test_memory_route_trigger_terms_removes_duplicates():
    """测试 trigger_terms 去除重复。"""
    route = MemoryRoute(
        route_id="r1",
        topic="t",
        trigger_keywords=["same"],
        related_terms=["same"],
    )
    terms = route.trigger_terms()
    assert terms.count("same") == 1


# ── MemoryRouteMatch 测试 ───────────────────────────────────────────────────

def test_memory_route_match_basic():
    """测试基本 MemoryRouteMatch。"""
    route = MemoryRoute(route_id="r1", topic="测试")
    match = MemoryRouteMatch(route=route, score=10.5)

    assert match.route.route_id == "r1"
    assert match.score == 10.5


def test_memory_route_match_default_lists():
    """测试默认值列表。"""
    route = MemoryRoute(route_id="r1", topic="t")
    match = MemoryRouteMatch(route=route, score=1.0)

    assert match.reasons == []
    assert match.matched_terms == []


def test_memory_route_match_with_lists():
    """测试带列表字段。"""
    route = MemoryRoute(route_id="r1", topic="t")
    match = MemoryRouteMatch(
        route=route,
        score=8.5,
        reasons=["精确命中相关词", "命中主题"],
        matched_terms=["相关词1", "主题"],
    )

    assert len(match.reasons) == 2
    assert len(match.matched_terms) == 2


# ── MemoryPathResolution 测试 ──────────────────────────────────────────────

def test_memory_path_resolution_soft():
    """测试 soft 模式路径解析。"""
    route = MemoryRoute(route_id="r1", topic="t")
    match = MemoryRouteMatch(route=route, score=5.0)
    resolution = MemoryPathResolution(
        mode="soft",
        required_read_paths=[],
        candidate_paths=["rules/a.md", "rules/b.md"],
        matches=[match],
    )

    assert resolution.mode == "soft"
    assert len(resolution.candidate_paths) == 2


def test_memory_path_resolution_strict():
    """测试 strict 模式路径解析。"""
    route = MemoryRoute(route_id="r1", topic="t")
    match = MemoryRouteMatch(route=route, score=5.0)
    resolution = MemoryPathResolution(
        mode="strict",
        required_read_paths=["rules/required.md"],
        candidate_paths=["rules/optional.md"],
        matches=[match],
    )

    assert resolution.mode == "strict"
    assert len(resolution.required_read_paths) == 1


# ── MemoryReadReceipt 测试 ─────────────────────────────────────────────────

def test_memory_read_receipt_basic():
    """测试基本 MemoryReadReceipt。"""
    receipt = MemoryReadReceipt(
        route_id="r1",
        authority_path="rules/test.md",
        status="planned",
    )
    assert receipt.route_id == "r1"
    assert receipt.status == "planned"
    assert receipt.read_at == 0.0
    assert receipt.content_hash == ""


def test_memory_read_receipt_mark_now():
    """测试 mark_now 补时间戳。"""
    receipt = MemoryReadReceipt(
        route_id="r1",
        authority_path="p.md",
        status="read",
    )
    before = time.time() - 1
    result = receipt.mark_now()
    after = time.time() + 1

    assert receipt.read_at >= before
    assert receipt.read_at <= after
    assert result is receipt  # 返回 self


def test_memory_read_receipt_mark_now_idempotent():
    """测试 mark_now 已有时戳不覆盖。"""
    receipt = MemoryReadReceipt(
        route_id="r1",
        authority_path="p.md",
        status="read",
        read_at=12345.0,
    )
    receipt.mark_now()
    assert receipt.read_at == 12345.0


def test_memory_read_receipt_full_fields():
    """测试完整字段。"""
    receipt = MemoryReadReceipt(
        route_id="full-receipt",
        authority_path="rules/full.md",
        status="read",
        reasons=["命中关键词"],
        read_at=123456.0,
        content_hash="sha256abc",
        elapsed_ms=15.5,
        error="",
    )
    assert receipt.reasons == ["命中关键词"]
    assert receipt.content_hash == "sha256abc"
    assert receipt.elapsed_ms == 15.5


# ── _dedupe 测试 ───────────────────────────────────────────────────────────

def test_dedupe_preserves_order():
    """测试保持顺序去重。"""
    items = ["a", "b", "a", "c"]
    result = dedupe_strings(items)
    assert result == ["a", "b", "c"]


def test_dedupe_removes_empty():
    """测试空字符串被移除。"""
    items = ["a", "", "b", "  "]
    result = dedupe_strings(items)
    assert result == ["a", "b"]


def test_dedupe_strips_before_dedup():
    """测试去重前先去除空白。"""
    items = ["a", "  a  ", "b"]
    result = dedupe_strings(items)
    assert result == ["a", "b"]


def test_dedupe_only_strings():
    """测试去重只处理字符串，非字符串应预先转换。"""
    items = ["a", "b", "a", "c"]
    result = dedupe_strings(items)
    assert result == ["a", "b", "c"]


def test_dedupe_empty_list():
    """测试空列表。"""
    assert dedupe_strings([]) == []


# ── 边界场景测试 ──────────────────────────────────────────────────────────

def test_memory_route_empty_route_id():
    """测试空 route_id 可以创建（验证层会报错）。"""
    route = MemoryRoute(route_id="", topic="测试")
    assert route.route_id == ""


def test_memory_read_receipt_error_status():
    """测试错误状态回执。"""
    receipt = MemoryReadReceipt(
        route_id="r1",
        authority_path="p.md",
        status="error",
        error="文件不存在",
    )
    assert receipt.status == "error"
    assert receipt.error == "文件不存在"


def test_memory_route_match_negative_score():
    """测试负数分数可以存在。"""
    route = MemoryRoute(route_id="r1", topic="t")
    match = MemoryRouteMatch(route=route, score=-5.0)
    assert match.score == -5.0
