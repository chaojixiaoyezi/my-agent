"""memory_doctor 模块测试。

测试 cmd_memory_doctor、_build_routing_doctor、_build_archive_doctor、_archive_dir_payload 等函数。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.cli.memory_doctor import (
    _archive_dir_payload,
    _archive_file_payload,
    _build_archive_doctor,
    _build_routing_doctor,
    _config_warnings,
    _index_payload,
    _memory_config_payload,
    _resolve_index_path,
    _route_payload,
)

# ── _resolve_index_path 测试 ───────────────────────────────────────────────

def test_resolve_index_path_default():
    """测试默认索引路径。"""
    root = Path("/tmp/project")
    result = _resolve_index_path(root, None)
    expected = root / "memory" / "routing" / "INDEX.md"
    assert result == expected.resolve()


def test_resolve_index_path_relative():
    """测试相对路径解析。"""
    root = Path("/tmp/project")
    result = _resolve_index_path(root, "custom/index.md")
    assert result == (root / "custom" / "index.md").resolve()


def test_resolve_index_path_absolute():
    """测试绝对路径直接返回。"""
    root = Path("/tmp/project")
    abs_path = "/absolute/path.md"
    result = _resolve_index_path(root, abs_path)
    assert result == Path(abs_path).resolve()


def test_resolve_index_path_expands_user():
    """测试波浪号路径展开。"""
    root = Path("/tmp/project")
    result = _resolve_index_path(root, "~/memory/INDEX.md")
    assert str(result).startswith(str(Path.home()))


def test_resolve_index_path_falls_back_to_home_route_index(tmp_path):
    """项目没有路由索引时，默认使用 ~/.my-agent 里的 HOT/lessons 路由索引。"""
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    root = tmp_path / "project"
    root.mkdir()
    home = ensure_my_agent_home(tmp_path / "home")

    result = _resolve_index_path(root, None, home_paths=home)

    assert result == home.memory_routing_index_md


def test_resolve_index_path_falls_back_to_provider_owner_route_index(tmp_path):
    """外部 owner 默认使用自己的 memory/routing/INDEX.md，不读取本地主账号索引。"""
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        ensure_owner_home,
        home_paths_with_owner,
    )

    root = tmp_path / "project"
    root.mkdir()
    home = ensure_my_agent_home(tmp_path / "home")
    owner = ensure_owner_home(home.root, OwnerIdentity.provider_user("feishu", "ou_123"))
    owner_home = home_paths_with_owner(home, owner)

    result = _resolve_index_path(root, None, home_paths=owner_home)

    assert result == owner_home.owner_memory_routing_index_md


# ── _build_routing_doctor 测试 ───────────────────────────────────────────────

def test_build_routing_doctor_missing_index(tmp_path):
    """测试索引文件缺失时。"""
    root = tmp_path / "project"
    root.mkdir()
    index_path = root / "memory" / "routing" / "NOTFOUND.md"

    result = _build_routing_doctor(root, index_path)

    assert result["load_error"] != ""
    assert "not found" in result["load_error"]
    assert result["route_count"] == 0


def test_build_routing_doctor_invalid_json(tmp_path):
    """测试无效 JSON 索引（Markdown 解析器不会抛出，忽略无效行）。"""
    root = tmp_path / "project"
    root.mkdir()
    index_file = root / "memory" / "routing" / "INDEX.md"
    index_file.parent.mkdir(parents=True)
    # Markdown 解析器遇到无效格式行时不抛出，而是跳过
    index_file.write_text("not valid json {", encoding="utf-8")

    result = _build_routing_doctor(root, index_file)

    assert result["load_error"] == ""
    assert result["route_count"] == 0


def test_build_routing_doctor_valid_index(tmp_path):
    """测试有效索引加载。"""
    root = tmp_path / "project"
    root.mkdir()
    index_file = root / "memory" / "routing" / "INDEX.md"
    index_file.parent.mkdir(parents=True)
    index_file.write_text(
        "## route-1\ntopic: 测试主题\ntrigger_keywords: 测试关键词\n",
        encoding="utf-8",
    )

    result = _build_routing_doctor(root, index_file)

    assert result["load_error"] == ""
    assert result["route_count"] == 1
    assert result["routes"][0]["route_id"] == "route-1"


def test_build_routing_doctor_validation_warnings(tmp_path):
    """测试路由验证警告。"""
    root = tmp_path / "project"
    root.mkdir()
    index_file = root / "memory" / "routing" / "INDEX.md"
    index_file.parent.mkdir(parents=True)
    # 缺少触发词的路由会产生警告
    index_file.write_text(
        "## empty-route\ntopic: 无触发词\n",
        encoding="utf-8",
    )

    result = _build_routing_doctor(root, index_file)

    assert len(result["validation_warnings"]) > 0


# ── _build_archive_doctor 测试 ───────────────────────────────────────────────

def test_build_archive_doctor_basic(tmp_path):
    """测试基本归档诊断。"""
    root = tmp_path / "project"
    root.mkdir()
    mock_config = MagicMock()
    mock_config.memory_hook_retention_days = 30
    mock_config.memory_hook_enabled = True
    mock_config.memory_hook_archive_level = 5
    mock_config.memory_archive_level = 3

    result = _build_archive_doctor(root, mock_config)

    assert "retention" in result
    assert "hook" in result
    assert "raw" in result
    assert result["retention"]["memory_hook_retention_days"] == 30


def test_build_archive_doctor_missing_config_fields(tmp_path):
    """测试缺少配置字段时的默认值。"""
    root = tmp_path / "project"
    root.mkdir()
    mock_config = MagicMock(spec=[])  # 空 spec，无属性

    result = _build_archive_doctor(root, mock_config)

    assert result["retention"]["memory_hook_retention_days"] == 7
    assert result["retention"]["memory_hook_enabled"] is True


# ── _archive_dir_payload 测试 ───────────────────────────────────────────────

def test_archive_dir_payload_no_directory(tmp_path):
    """测试目录不存在时。"""
    directory = tmp_path / "nonexistent"
    today_path = directory / "today.jsonl"

    result = _archive_dir_payload(directory, today_path)

    assert result["exists"] is False
    assert result["file_count"] == 0
    assert result["recent_files"] == []


def test_archive_dir_payload_with_files(tmp_path):
    """测试有文件时。"""
    directory = tmp_path / "archive"
    directory.mkdir()
    today_path = directory / "2024-01-01.jsonl"
    (directory / "file1.jsonl").write_text("content", encoding="utf-8")
    (directory / "file2.jsonl").write_text("content", encoding="utf-8")

    result = _archive_dir_payload(directory, today_path)

    assert result["exists"] is True
    assert result["file_count"] == 2
    assert len(result["recent_files"]) == 2


def test_archive_dir_payload_recent_files_limit(tmp_path):
    """测试最近文件数量限制。"""
    directory = tmp_path / "archive"
    directory.mkdir()
    today_path = directory / "2024-01-01.jsonl"
    # 创建 10 个文件
    for i in range(10):
        (directory / f"file{i}.jsonl").write_text(f"content{i}", encoding="utf-8")

    result = _archive_dir_payload(directory, today_path)

    # 应该只返回 5 个最近文件
    assert result["file_count"] == 10
    assert len(result["recent_files"]) == 5


# ── _archive_file_payload 测试 ───────────────────────────────────────────────

def test_archive_file_payload_basic(tmp_path):
    """测试基本文件元数据。"""
    file_path = tmp_path / "test.jsonl"
    file_path.write_text("test content", encoding="utf-8")

    result = _archive_file_payload(file_path)

    assert result["name"] == "test.jsonl"
    assert result["path"] == str(file_path)
    assert result["size_bytes"] == 12
    assert result["modified_at"] > 0


# ── _memory_config_payload 测试 ───────────────────────────────────────────────

def test_memory_config_payload_basic():
    """测试基本配置载荷。"""
    mock_config = MagicMock()
    mock_config.memory_archive_level = 3
    mock_config.memory_hook_enabled = True
    mock_config.memory_hook_archive_level = 5
    mock_config.memory_hook_retention_days = 30
    mock_config.memory_rule_routing_enabled = True
    mock_config.memory_rule_routing_mode = "soft"
    mock_config.memory_rule_auto_read_limit = 3
    mock_config.memory_rule_receipt_enabled = True

    result = _memory_config_payload(mock_config)

    assert result["memory_archive_level"] == 3
    assert result["memory_hook_enabled"] is True
    assert result["memory_rule_routing_enabled"] is True


# ── _config_warnings 测试 ───────────────────────────────────────────────────

def test_config_warnings_empty():
    """测试空警告列表。"""
    mock_config = MagicMock()
    mock_config.memory_config_warnings = []

    result = _config_warnings(mock_config)

    assert result == []


def test_config_warnings_dict_format():
    """测试字典格式警告。"""
    mock_config = MagicMock()
    mock_config.memory_config_warnings = [
        {"field_name": "test", "reason": "test error", "fallback_value": "default"}
    ]

    result = _config_warnings(mock_config)

    assert len(result) == 1
    assert result[0]["field_name"] == "test"


def test_config_warnings_object_with_to_dict():
    """测试带 to_dict 方法的对象警告。"""
    mock_config = MagicMock()
    warning = MagicMock()
    warning.to_dict.return_value = {"field_name": "obj", "reason": "obj error"}
    mock_config.memory_config_warnings = [warning]

    result = _config_warnings(mock_config)

    assert len(result) == 1
    warning.to_dict.assert_called_once()


def test_config_warnings_object_with_dataclass_fields():
    """测试带 __dataclass_fields__ 的对象警告。"""
    mock_config = MagicMock()
    warning = MagicMock()
    warning.__dataclass_fields__ = {"field": None}
    warning.field = "value"
    mock_config.memory_config_warnings = [warning]

    result = _config_warnings(mock_config)

    assert len(result) == 1


def test_config_warnings_fallback_to_string():
    """测试无法识别时回退到字符串。"""
    mock_config = MagicMock()
    mock_config.memory_config_warnings = ["simple string warning", 123]

    result = _config_warnings(mock_config)

    assert result[0]["message"] == "simple string warning"
    assert result[1]["message"] == "123"


# ── _index_payload 测试 ─────────────────────────────────────────────────────

def test_index_payload_exists(tmp_path):
    """测试存在的索引。"""
    index_file = tmp_path / "INDEX.md"
    index_file.write_text("content", encoding="utf-8")

    result = _index_payload(index_file)

    assert result["path"] == str(index_file)
    assert result["exists"] is True
    assert result["is_file"] is True


def test_index_payload_not_exists(tmp_path):
    """测试不存在的索引。"""
    index_file = tmp_path / "NOTFOUND.md"

    result = _index_payload(index_file)

    assert result["exists"] is False


# ── _route_payload 测试 ─────────────────────────────────────────────────────

def test_route_payload_basic():
    """测试基本路由载荷。"""
    from agent_py_agent.agent.memory_routing.models import MemoryRoute

    route = MemoryRoute(
        route_id="route-1",
        topic="测试主题",
        trigger_keywords=["测试", "关键词"],
        aliases=["alias1"],
        when_to_read="需要时读取",
        authority_path="rules/test.md",
        scope="project",
        priority=50,
        stale_check="每季度检查",
        last_verified_at="2024-01-01",
        source_path="/path/to/index",
    )

    result = _route_payload(route)

    assert result["route_id"] == "route-1"
    assert result["topic"] == "测试主题"
    assert result["trigger_keywords"] == ["测试", "关键词"]
    assert result["priority"] == 50


# ── 边界场景测试 ──────────────────────────────────────────────────────────

def test_build_routing_doctor_empty_index(tmp_path):
    """测试空索引文件。"""
    root = tmp_path / "project"
    root.mkdir()
    index_file = root / "memory" / "routing" / "INDEX.md"
    index_file.parent.mkdir(parents=True)
    index_file.write_text("", encoding="utf-8")

    result = _build_routing_doctor(root, index_file)

    assert result["load_error"] == ""
    assert result["route_count"] == 0


def test_archive_dir_payload_non_jsonl_files_ignored(tmp_path):
    """测试非 JSONL 文件被忽略。"""
    directory = tmp_path / "archive"
    directory.mkdir()
    (directory / "readme.txt").write_text("readme", encoding="utf-8")
    (directory / "data.jsonl").write_text("{}", encoding="utf-8")

    result = _archive_dir_payload(directory, directory / "today.jsonl")

    assert result["file_count"] == 1
    assert result["recent_files"][0]["name"] == "data.jsonl"


def test_memory_config_payload_missing_fields():
    """测试缺少字段时 AttributeError 被抛出。"""
    mock_config = MagicMock(spec=[])  # 空 spec，无属性

    with pytest.raises(AttributeError):
        _memory_config_payload(mock_config)
