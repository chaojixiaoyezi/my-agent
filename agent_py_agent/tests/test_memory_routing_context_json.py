"""memory_routing context 模块测试。

测试 build_routed_memory_context 和相关路径解析函数。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_routing.context import (
    RouteContextOptions,
    _resolve_relative_path,
    _resolve_root,
    build_routed_memory_context,
)

# ── _resolve_root 测试 ─────────────────────────────────────────────────────

def test_build_routed_memory_context_index_is_directory(tmp_path):
    """测试索引是目录而非文件时返回 finding。"""
    index_dir = tmp_path / "memory" / "routing"
    index_dir.mkdir(parents=True)

    context = build_routed_memory_context(
        root=tmp_path,
        query="测试",
        options=RouteContextOptions(
            index_path="memory/routing",
        ),
    )

    assert len(context.findings) > 0
    assert "not a file" in context.findings[0]

def test_build_routed_memory_context_invalid_json_index(tmp_path):
    """测试无效 JSON 索引返回 finding。"""
    index_file = tmp_path / "memory" / "routing" / "INDEX.json"
    index_file.parent.mkdir(parents=True)
    index_file.write_text("not valid json {", encoding="utf-8")

    context = build_routed_memory_context(
        root=tmp_path,
        query="测试",
        options=RouteContextOptions(
            index_path="memory/routing/INDEX.json",
        ),
    )

    assert len(context.findings) > 0
    assert "invalid JSON" in context.findings[0]

def test_build_routed_memory_context_json_index(tmp_path):
    """测试 JSON 格式索引。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    rules_dir.joinpath("test.md").write_text("JSON rule", encoding="utf-8")

    index_file = tmp_path / "memory" / "routing" / "INDEX.json"
    index_file.parent.mkdir(parents=True)
    index_file.write_text(
        '[{"route_id": "json-route", "topic": "JSON主题", "authority_path": "rules/test.md"}]',
        encoding="utf-8",
    )

    context = build_routed_memory_context(
        root=tmp_path,
        query="JSON主题",
        options=RouteContextOptions(
            index_path="memory/routing/INDEX.json",
        ),
    )

    assert context.routes_count == 1
    assert context.matches[0]["route_id"] == "json-route"

def test_build_routed_memory_context_duplicate_authority_path(tmp_path):
    """测试多条路由指向同一文件时去重。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    rules_dir.joinpath("shared.md").write_text("shared", encoding="utf-8")

    index_file = tmp_path / "memory" / "routing" / "INDEX.md"
    index_file.parent.mkdir(parents=True)
    index_file.write_text(
        "## route-1\ntopic: t1\ntrigger_keywords: 测试关键词\nauthority_path: rules/shared.md\n"
        "---\n"
        "## route-2\ntopic: t2\ntrigger_keywords: 测试关键词\nauthority_path: rules/shared.md\n",
        encoding="utf-8",
    )

    context = build_routed_memory_context(
        root=tmp_path,
        query="测试关键词",
        options=RouteContextOptions(
            index_path="memory/routing/INDEX.md",
        ),
    )

    # 两条路由匹配（相同的触发词）
    assert context.routes_count == 2
    assert len(context.matches) == 2
    # 候选路径去重 - 虽然两条路由都匹配，但指向同一文件
    assert context.candidate_paths.count("rules/shared.md") == 1

def test_build_routed_memory_context_pathlib_root(tmp_path):
    """测试 Path 对象作为 root。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    rules_dir.joinpath("test.md").write_text("pathlib test", encoding="utf-8")

    index_file = tmp_path / "memory" / "routing" / "INDEX.md"
    index_file.parent.mkdir(parents=True)
    index_file.write_text(
        "## route-1\ntopic: t\nauthority_path: rules/test.md\n",
        encoding="utf-8",
    )

    context = build_routed_memory_context(
        root=tmp_path,  # Path 对象
        query="测试",
        options=RouteContextOptions(
            index_path="memory/routing/INDEX.md",
        ),
    )

    assert context.routes_count == 1

def test_build_routed_memory_context_whitespace_mode(tmp_path):
    """测试空白字符 mode 被正确处理。"""
    index_file = tmp_path / "memory" / "routing" / "INDEX.md"
    index_file.parent.mkdir(parents=True)
    index_file.write_text("## route-1\ntopic: t1\n", encoding="utf-8")

    context = build_routed_memory_context(
        root=tmp_path,
        query="测试",
        options=RouteContextOptions(
            index_path="memory/routing/INDEX.md",
            mode="  soft  ",
        ),
    )

    # 空白被去除后应该有效
    assert len(context.findings) == 0 or "mode must be one of" not in context.findings[0]

def test_build_routed_memory_context_limit_results(tmp_path):
    """测试 limit 参数限制返回结果数。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()

    index_file = tmp_path / "memory" / "routing" / "INDEX.md"
    index_file.parent.mkdir(parents=True)

    routes_text = "## route-1\ntopic: 测试\ntrigger_keywords: 测试\nauthority_path: rules/1.md\n---\n"
    for i in range(2, 11):
        rules_dir.joinpath(f"{i}.md").write_text(f"rule {i}", encoding="utf-8")
        routes_text += f"## route-{i}\ntopic: 测试\ntrigger_keywords: 测试\nauthority_path: rules/{i}.md\n---\n"

    index_file.write_text(routes_text, encoding="utf-8")

    context = build_routed_memory_context(
        root=tmp_path,
        query="测试",
        options=RouteContextOptions(
            index_path="memory/routing/INDEX.md",
            limit=3,
        ),
    )

    assert len(context.matches) == 3

def test_build_routed_memory_context_receipt_has_content_hash(tmp_path):
    """测试成功读取的文件 receipt 包含 content hash。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    rules_dir.joinpath("test.md").write_text("内容", encoding="utf-8")

    index_file = tmp_path / "memory" / "routing" / "INDEX.md"
    index_file.parent.mkdir(parents=True)
    index_file.write_text(
        "## route-1\ntopic: t\ntrigger_keywords: 测试关键词\nauthority_path: rules/test.md\n",
        encoding="utf-8",
    )

    context = build_routed_memory_context(
        root=tmp_path,
        query="测试关键词",
        options=RouteContextOptions(
            index_path="memory/routing/INDEX.md",
        ),
    )

    assert context.receipts[0]["content_hash"] != ""
    assert len(context.receipts[0]["content_hash"]) == 64  # SHA256 hex

def test_build_routed_memory_context_authority_escape_finding(tmp_path):
    """测试 authority 路径越界时产生 finding。"""
    index_file = tmp_path / "memory" / "routing" / "INDEX.md"
    index_file.parent.mkdir(parents=True)
    index_file.write_text(
        "## escape-route\ntopic: t\nauthority_path: ../outside.md\n",
        encoding="utf-8",
    )

    context = build_routed_memory_context(
        root=tmp_path,
        query="测试",
        options=RouteContextOptions(
            index_path="memory/routing/INDEX.md",
        ),
    )

    assert any("escapes root" in f for f in context.findings)
