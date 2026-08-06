"""正式 Lesson 只通过 routing metadata 召回，不再扫描文件名做模糊 fallback。"""

from agent_py_agent.agent.memory_routing.matcher import _chinese_ngrams, match_routes
from agent_py_agent.agent.memory_routing.models import MemoryRoute


def _route(*keywords: str) -> MemoryRoute:
    return MemoryRoute(
        route_id="lesson.lesson-1",
        topic="日志运营值守",
        trigger_keywords=list(keywords),
        authority_path="memory/lessons/log-ops.md",
        inject_mode="on_hit",
    )


def test_chinese_keyword_routes_formal_lesson() -> None:
    hits = match_routes("帮我盯日志运营", [_route("日志", "运营", "值守")])

    assert [item.route.route_id for item in hits] == ["lesson.lesson-1"]


def test_unrelated_query_does_not_route_lesson() -> None:
    assert match_routes("写个待办工具", [_route("日志", "运营", "值守")]) == []


def test_short_keyword_uses_explicit_substring_not_filename_guess() -> None:
    assert match_routes("xabz", [_route("ab")])
    assert match_routes("xyz", [_route("ab")]) == []


def test_chinese_ngrams_are_deterministic() -> None:
    grams = _chinese_ngrams("压缩续航")

    assert "压缩" in grams
    assert "续航" in grams
    assert grams == _chinese_ngrams("压缩续航")


def test_word_order_difference_requires_declared_keywords() -> None:
    route = _route("压缩", "续航")

    assert match_routes("这个续航压缩方案不错", [route])
    assert match_routes("今天天气很好", [route]) == []


def test_route_returns_authority_path_not_scanned_stem() -> None:
    hit = match_routes("使用 todo-cli 工具", [_route("todo-cli")])[0]

    assert hit.route.authority_file() == "memory/lessons/log-ops.md"
