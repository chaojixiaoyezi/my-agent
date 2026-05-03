"""memory_routing loader 模块测试。

测试 load_memory_routes、load_routes、parse_json_routes、parse_markdown_routes 等函数。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_routing.loader import (
    load_memory_routes,
    load_routes,
    parse_json_routes,
    parse_markdown_routes,
)


# ── load_routes / load_memory_routes 测试 ────────────────────────────────

def test_load_memory_routes_json_file(tmp_path):
    """测试从 JSON 文件加载路由。"""
    index_file = tmp_path / "routes.json"
    index_file.write_text(
        '[{"route_id": "route-1", "topic": "主题1", "trigger_keywords": ["词1", "词2"]}]',
        encoding="utf-8",
    )

    routes = load_memory_routes(index_file)

    assert len(routes) == 1
    assert routes[0].route_id == "route-1"
    assert routes[0].topic == "主题1"


def test_load_memory_routes_markdown_file(tmp_path):
    """测试从 Markdown 文件加载路由。"""
    index_file = tmp_path / "routes.md"
    index_file.write_text(
        "## route-2\n"
        "topic: 主题2\n"
        "trigger_keywords: 关键词A, 关键词B\n",
        encoding="utf-8",
    )

    routes = load_memory_routes(index_file)

    assert len(routes) == 1
    assert routes[0].route_id == "route-2"
    assert routes[0].topic == "主题2"


def test_load_routes_calls_load_memory_routes(tmp_path):
    """测试 load_routes 调用 load_memory_routes 并返回相同结果。"""
    index_file = tmp_path / "routes.json"
    index_file.write_text(
        '[{"route_id": "route-1", "topic": "主题1"}]',
        encoding="utf-8",
    )

    routes_from_alias = load_routes(index_file)
    routes_from_main = load_memory_routes(index_file)

    assert len(routes_from_alias) == 1
    assert routes_from_alias[0].route_id == routes_from_main[0].route_id


def test_load_memory_routes_nonexistent():
    """测试加载不存在的文件抛出异常。"""
    with pytest.raises(ValueError, match="cannot read memory route index"):
        load_memory_routes("/nonexistent/path.md")


def test_load_memory_routes_invalid_json(tmp_path):
    """测试加载无效 JSON 抛出异常。"""
    index_file = tmp_path / "invalid.json"
    index_file.write_text("not valid json {", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON"):
        load_memory_routes(index_file)


# ── parse_json_routes 测试 ────────────────────────────────────────────────

def test_parse_json_routes_list_format():
    """测试解析 JSON 数组格式。"""
    text = '[{"route_id": "r1", "topic": "t1"}]'
    routes = parse_json_routes(text)

    assert len(routes) == 1
    assert routes[0].route_id == "r1"


def test_parse_json_routes_object_format():
    """测试解析带 routes 字段的对象格式。"""
    text = '{"routes": [{"route_id": "r1", "topic": "t1"}]}'
    routes = parse_json_routes(text)

    assert len(routes) == 1
    assert routes[0].route_id == "r1"


def test_parse_json_routes_multiple():
    """测试解析多条路由。"""
    text = '[{"route_id": "r1"}, {"route_id": "r2"}, {"route_id": "r3"}]'
    routes = parse_json_routes(text)

    assert len(routes) == 3


def test_parse_json_routes_empty_list():
    """测试解析空数组。"""
    routes = parse_json_routes("[]")
    assert len(routes) == 0


def test_parse_json_routes_dict_without_routes():
    """测试解析只有单个对象的字典（没有 routes 键）。"""
    # 单个 dict 没有 routes 键时，routes_data 被设为空列表
    text = '{"route_id": "single"}'
    routes = parse_json_routes(text)
    # 解析结果为空列表，因为 routes_data 是空列表
    assert len(routes) == 0


def test_parse_json_routes_dict_with_empty_routes():
    """测试解析带空 routes 数组的对象格式。"""
    text = '{"routes": []}'
    routes = parse_json_routes(text)
    assert len(routes) == 0


def test_parse_json_routes_with_list_fields():
    """测试解析包含列表字段的路由。"""
    text = '[{"route_id": "r1", "trigger_keywords": ["kw1", "kw2"], "aliases": ["a1", "a2"]}]'
    routes = parse_json_routes(text, source_path="test.json")

    assert routes[0].trigger_keywords == ["kw1", "kw2"]
    assert routes[0].aliases == ["a1", "a2"]


def test_parse_json_routes_comma_separated_keywords():
    """测试解析逗号分隔的关键词字符串。"""
    text = '[{"route_id": "r1", "trigger_keywords": "kw1, kw2, kw3"}]'
    routes = parse_json_routes(text)

    assert len(routes[0].trigger_keywords) == 3


# ── parse_markdown_routes 测试 ────────────────────────────────────────────

def test_parse_markdown_routes_basic():
    """测试解析基本 Markdown 格式。"""
    text = (
        "## route-md-1\n"
        "topic: 测试主题\n"
        "trigger_keywords: 关键词A, 关键词B\n"
    )
    routes = parse_markdown_routes(text)

    assert len(routes) == 1
    assert routes[0].route_id == "route-md-1"
    assert routes[0].topic == "测试主题"


def test_parse_markdown_routes_multiple():
    """测试解析多条 Markdown 路由。"""
    text = (
        "## route-1\n"
        "topic: 主题1\n"
        "---\n"
        "## route-2\n"
        "topic: 主题2\n"
    )
    routes = parse_markdown_routes(text)

    assert len(routes) == 2
    assert routes[0].route_id == "route-1"
    assert routes[1].route_id == "route-2"


def test_parse_markdown_routes_priority():
    """测试解析优先级字段。"""
    text = (
        "## priority-route\n"
        "topic: 高优先级\n"
        "priority: 80\n"
    )
    routes = parse_markdown_routes(text)

    assert routes[0].priority == 80


def test_parse_markdown_routes_empty_lines():
    """测试解析跳过空行。"""
    text = (
        "## route-1\n"
        "topic: 主题1\n"
        "\n"
        "\n"
        "## route-2\n"
        "topic: 主题2\n"
    )
    routes = parse_markdown_routes(text)

    assert len(routes) == 2


def test_parse_markdown_routes_comments_skipped():
    """测试解析跳过注释行。"""
    text = (
        "## route-1\n"
        "topic: 主题1\n"
        "# 这是注释\n"
        "## route-2\n"
        "topic: 主题2\n"
    )
    routes = parse_markdown_routes(text)

    assert len(routes) == 2


def test_parse_markdown_routes_aliases():
    """测试解析别名。"""
    text = (
        "## alias-route\n"
        "topic: 别名路由\n"
        "aliases: 别名1, 别名2\n"
    )
    routes = parse_markdown_routes(text)

    assert "别名1" in routes[0].aliases
    assert "别名2" in routes[0].aliases


def test_parse_markdown_routes_authority_path():
    """测试解析 authority_path。"""
    text = (
        "## auth-route\n"
        "topic: 权威路径\n"
        "authority_path: rules/auth.md\n"
    )
    routes = parse_markdown_routes(text)

    assert routes[0].authority_path == "rules/auth.md"


def test_parse_markdown_routes_inject_mode():
    """测试解析 inject_mode。"""
    text = (
        "## inject-route\n"
        "topic: 注入模式\n"
        "inject_mode: always\n"
    )
    routes = parse_markdown_routes(text)

    assert routes[0].inject_mode == "always"


# ── 边界场景测试 ──────────────────────────────────────────────────────────

def test_parse_json_routes_invalid_item_type():
    """测试解析包含非对象项时抛出异常。"""
    with pytest.raises(ValueError, match="must be an object"):
        parse_json_routes('[{"route_id": "r1"}, "not an object"]')


def test_parse_markdown_routes_key_with_dash():
    """测试解析带横线的 key 转换为下划线。"""
    text = (
        "## dash-route\n"
        "trigger-keywords: kw1, kw2\n"
    )
    routes = parse_markdown_routes(text)

    # trigger-keywords 应该被转换为 trigger_keywords
    assert "kw1" in routes[0].trigger_keywords


def test_parse_markdown_routes_whitespace_value():
    """测试解析空白值被忽略。"""
    text = (
        "## ws-route\n"
        "topic: 主题\n"
        "trigger_keywords: \n"
    )
    routes = parse_markdown_routes(text)

    assert routes[0].trigger_keywords == []


def test_parse_json_routes_source_path():
    """测试 source_path 被正确传递。"""
    routes = parse_json_routes('[{"route_id": "r1"}]', source_path="/test/path.json")
    assert routes[0].source_path == "/test/path.json"


def test_parse_markdown_routes_source_path():
    """测试 Markdown 解析时 source_path 被正确传递。"""
    routes = parse_markdown_routes("## r1\ntopic: t1", source_path="/test.md")
    assert routes[0].source_path == "/test.md"