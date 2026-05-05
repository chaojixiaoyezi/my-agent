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

def test_resolve_root_valid_directory(tmp_path):
    """测试有效目录解析。"""
    root, error = _resolve_root(tmp_path)
    assert root is not None
    assert error == ""


def test_resolve_root_nonexistent():
    """测试不存在路径。"""
    root, error = _resolve_root("/nonexistent/path/xyz")
    assert root is None
    assert "does not exist" in error


def test_resolve_root_file_not_directory(tmp_path):
    """测试文件路径返回错误。"""
    file_path = tmp_path / "file.txt"
    file_path.write_text("not a directory")

    root, error = _resolve_root(file_path)
    assert root is None
    assert "not a directory" in error


def test_resolve_root_string_path(tmp_path):
    """测试字符串路径。"""
    root, error = _resolve_root(str(tmp_path))
    assert root is not None


def test_resolve_root_symlink_points_to_nonexistent(tmp_path):
    """测试符号链接指向不存在目标。"""
    link_path = tmp_path / "broken_link"
    try:
        link_path.symlink_to("/nonexistent/target")
    except OSError:
        pytest.skip("symlink creation not supported")

    root, error = _resolve_root(link_path)
    assert root is None
    assert "does not exist" in error


# ── _resolve_relative_path 测试 ────────────────────────────────────────────

def test_resolve_relative_path_valid(tmp_path):
    """测试有效相对路径解析。"""
    root = tmp_path / "project"
    root.mkdir()
    (root / "rules").mkdir()

    resolved, normalized, error = _resolve_relative_path(
        root, "rules/test.md", label="test_path"
    )

    assert resolved is not None
    assert normalized == "rules/test.md"
    assert error == ""


def test_resolve_relative_path_absolute_rejected(tmp_path):
    """测试绝对路径被拒绝。"""
    root = tmp_path / "project"
    root.mkdir()

    resolved, normalized, error = _resolve_relative_path(
        root, "/absolute/path", label="test_path"
    )

    assert resolved is None
    assert "must be relative" in error


def test_resolve_relative_path_escape_root(tmp_path):
    """测试越界路径被拒绝。"""
    root = tmp_path / "project"
    root.mkdir()

    resolved, normalized, error = _resolve_relative_path(
        root, "../etc/passwd", label="test_path"
    )

    assert resolved is None
    assert "escapes root" in error


def test_resolve_relative_path_empty_returns_error(tmp_path):
    """测试空路径返回错误。"""
    root = tmp_path / "project"
    root.mkdir()

    resolved, normalized, error = _resolve_relative_path(root, "", label="empty_path")

    assert resolved is None
    assert "is empty" in error


def test_resolve_relative_path_whitespace_stripped(tmp_path):
    """测试空白被去除。"""
    root = tmp_path / "project"
    root.mkdir()

    resolved, normalized, error = _resolve_relative_path(
        root, "  rules/test.md  ", label="test"
    )

    assert resolved is not None
    assert normalized == "rules/test.md"


def test_resolve_relative_path_dotdot_in_middle(tmp_path):
    """测试路径中间的 .. 被正确解析。"""
    root = tmp_path / "project"
    root.mkdir()
    (root / "rules" / "sub").mkdir(parents=True)

    resolved, normalized, error = _resolve_relative_path(
        root, "rules/../rules/sub/../test.md", label="test"
    )

    assert resolved is not None
    assert "test.md" in normalized


# ── build_routed_memory_context 测试 ───────────────────────────────────────

def test_build_routed_memory_context_disabled():
    """测试 disabled 时返回空 context。"""
    context = build_routed_memory_context(
        root="/tmp",
        query="测试查询",
        options=RouteContextOptions(
            enabled=False,
        ),
    )
    assert context.enabled is False
    assert context.matches == []


def test_build_routed_memory_context_nonexistent_root():
    """测试不存在根目录返回 finding。"""
    context = build_routed_memory_context(
        root="/nonexistent/root",
        query="测试",
    )
    assert len(context.findings) > 0
    assert "does not exist" in context.findings[0]


def test_build_routed_memory_context_invalid_mode(tmp_path):
    """测试无效 mode 返回 finding。"""
    index_file = tmp_path / "memory" / "routing" / "INDEX.md"
    index_file.parent.mkdir(parents=True)
    index_file.write_text("## route-1\ntopic: t1\n", encoding="utf-8")

    context = build_routed_memory_context(
        root=tmp_path,
        query="测试",
        options=RouteContextOptions(
            index_path="memory/routing/INDEX.md",
            mode="invalid_mode",
        ),
    )
    assert len(context.findings) > 0
    assert "mode must be one of" in context.findings[0]


def test_build_routed_memory_context_missing_index(tmp_path):
    """测试索引文件不存在返回 finding。"""
    context = build_routed_memory_context(
        root=tmp_path,
        query="测试",
        options=RouteContextOptions(
            index_path="memory/routing/NOTFOUND.md",
        ),
    )
    assert len(context.findings) > 0
    assert "does not exist" in context.findings[0]


def test_build_routed_memory_context_valid_index(tmp_path):
    """测试有效索引文件被正确加载。"""
    index_file = tmp_path / "memory" / "routing" / "INDEX.md"
    index_file.parent.mkdir(parents=True)
    index_file.write_text(
        "## route-1\ntopic: 数据分析\ntrigger_keywords: 分析\nauthority_path: rules/analysis.md\n",
        encoding="utf-8",
    )

    context = build_routed_memory_context(
        root=tmp_path,
        query="需要数据分析工具",
        options=RouteContextOptions(
            index_path="memory/routing/INDEX.md",
            mode="soft",
        ),
    )

    assert context.routes_count == 1
    assert len(context.matches) == 1
    assert context.matches[0]["route_id"] == "route-1"


def test_build_routed_memory_context_auto_read_limit_zero(tmp_path):
    """测试 auto_read_limit=0 时不读取文件。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    rules_dir.joinpath("test.md").write_text("规则内容", encoding="utf-8")

    index_file = tmp_path / "memory" / "routing" / "INDEX.md"
    index_file.parent.mkdir(parents=True)
    index_file.write_text(
        "## route-1\ntopic: t\nauthority_path: rules/test.md\n",
        encoding="utf-8",
    )

    context = build_routed_memory_context(
        root=tmp_path,
        query="测试",
        options=RouteContextOptions(
            index_path="memory/routing/INDEX.md",
            auto_read_limit=0,
        ),
    )

    assert context.required_read_paths == []
    assert context.candidate_paths == []
    assert context.receipts == []


def test_build_routed_memory_context_strict_mode(tmp_path):
    """测试 strict 模式设置 required_read_paths。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    rules_dir.joinpath("test.md").write_text("规则内容", encoding="utf-8")

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
            mode="strict",
            auto_read_limit=3,
        ),
    )

    assert len(context.required_read_paths) == 1


def test_build_routed_memory_context_read_content(tmp_path):
    """测试读取文件内容并注入。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    rules_dir.joinpath("test.md").write_text("这是规则文件内容", encoding="utf-8")

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
            auto_read_limit=3,
            max_chars_per_file=100,
        ),
    )

    assert len(context.injected_sections) == 1
    assert "这是规则文件内容" in context.injected_sections[0]


def test_build_routed_memory_context_truncated_content(tmp_path):
    """测试超长内容被截断。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    rules_dir.joinpath("long.md").write_text("A" * 200, encoding="utf-8")

    index_file = tmp_path / "memory" / "routing" / "INDEX.md"
    index_file.parent.mkdir(parents=True)
    index_file.write_text(
        "## route-1\ntopic: t\ntrigger_keywords: 长文本关键词\nauthority_path: rules/long.md\n",
        encoding="utf-8",
    )

    context = build_routed_memory_context(
        root=tmp_path,
        query="长文本关键词",
        options=RouteContextOptions(
            index_path="memory/routing/INDEX.md",
            auto_read_limit=3,
            max_chars_per_file=50,
        ),
    )

    assert len(context.injected_sections) == 1
    assert "[truncated:" in context.injected_sections[0]


def test_build_routed_memory_context_missing_authority_file(tmp_path):
    """测试权威文件不存在时记录 receipt 但不注入内容。"""
    index_file = tmp_path / "memory" / "routing" / "INDEX.md"
    index_file.parent.mkdir(parents=True)
    index_file.write_text(
        "## route-1\ntopic: t\ntrigger_keywords: 测试关键词\nauthority_path: rules/nonexistent.md\n",
        encoding="utf-8",
    )

    context = build_routed_memory_context(
        root=tmp_path,
        query="测试关键词",
        options=RouteContextOptions(
            index_path="memory/routing/INDEX.md",
            auto_read_limit=3,
        ),
    )

    assert len(context.receipts) == 1
    assert context.receipts[0]["status"] == "missing"


def test_build_routed_memory_context_route_validation(tmp_path):
    """测试路由验证发现的问题进入 findings。"""
    index_file = tmp_path / "memory" / "routing" / "INDEX.md"
    index_file.parent.mkdir(parents=True)
    # 缺少触发词和别名的路由
    index_file.write_text(
        "## empty-route\ntopic: 无触发词\n",
        encoding="utf-8",
    )

    context = build_routed_memory_context(
        root=tmp_path,
        query="测试",
        options=RouteContextOptions(
            index_path="memory/routing/INDEX.md",
        ),
    )

    # findings 应包含验证警告
    assert len(context.findings) > 0


# ── 边界场景测试 ──────────────────────────────────────────────────────────

def test_build_routed_memory_context_empty_query(tmp_path):
    """测试空查询只匹配 inject_mode=always。"""
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    rules_dir.joinpath("always.md").write_text("always", encoding="utf-8")

    index_file = tmp_path / "memory" / "routing" / "INDEX.md"
    index_file.parent.mkdir(parents=True)
    index_file.write_text(
        "## always-route\ntopic: t\ninject_mode: always\nauthority_path: rules/always.md\n",
        encoding="utf-8",
    )

    context = build_routed_memory_context(
        root=tmp_path,
        query="",
        options=RouteContextOptions(
            index_path="memory/routing/INDEX.md",
        ),
    )

    assert len(context.matches) == 1


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
