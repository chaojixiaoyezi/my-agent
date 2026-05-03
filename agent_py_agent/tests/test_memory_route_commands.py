"""memory_route_commands CLI 命令测试。

测试 memory-route/memory-doctor 命令中的路由相关功能。
注意：memory_commands.py 中已经包含 route 相关命令，这里测试辅助函数。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


class TestResolveIndexPath:
    """测试 _resolve_index_path 辅助函数。"""

    def test_resolve_index_path_default(self, tmp_path: Path):
        """测试默认索引路径解析。"""
        from agent_py_agent.cli.memory_commands import _resolve_index_path

        result = _resolve_index_path(tmp_path, None)
        expected = tmp_path / "memory" / "routing" / "INDEX.md"
        assert result == expected

    def test_resolve_index_path_relative(self, tmp_path: Path):
        """测试相对路径解析。"""
        from agent_py_agent.cli.memory_commands import _resolve_index_path

        result = _resolve_index_path(tmp_path, "custom/index.md")
        expected = tmp_path / "custom" / "index.md"
        assert result == expected

    def test_resolve_index_path_absolute(self, tmp_path: Path):
        """测试绝对路径解析。"""
        from agent_py_agent.cli.memory_commands import _resolve_index_path

        abs_path = tmp_path / "absolute" / "index.md"
        result = _resolve_index_path(tmp_path, str(abs_path))
        assert result == abs_path.resolve()


class TestResolveRouteMode:
    """测试 _resolve_route_mode 辅助函数。"""

    def test_resolve_route_mode_explicit(self):
        """测试显式传入的 mode。"""
        from agent_py_agent.cli.memory_commands import _resolve_route_mode

        mock_config = MagicMock()
        mock_config.memory_rule_routing_mode = "soft"

        result = _resolve_route_mode("strict", mock_config)
        assert result == "strict"

    def test_resolve_route_mode_from_config(self):
        """测试从配置读取 mode。"""
        from agent_py_agent.cli.memory_commands import _resolve_route_mode

        mock_config = MagicMock()
        mock_config.memory_rule_routing_mode = "strict"

        result = _resolve_route_mode(None, mock_config)
        assert result == "strict"

    def test_resolve_route_mode_default_fallback(self):
        """测试配置为空时的默认回退。"""
        from agent_py_agent.cli.memory_commands import _resolve_route_mode

        mock_config = MagicMock()
        mock_config.memory_rule_routing_mode = None

        result = _resolve_route_mode(None, mock_config)
        assert result == "soft"


class TestResolveAutoReadLimit:
    """测试 _resolve_auto_read_limit 辅助函数。"""

    def test_resolve_auto_read_limit_explicit(self):
        """测试显式传入的限制值。"""
        from agent_py_agent.cli.memory_commands import _resolve_auto_read_limit

        mock_config = MagicMock()
        mock_config.memory_rule_auto_read_limit = 3

        result = _resolve_auto_read_limit(10, mock_config)
        assert result == 10

    def test_resolve_auto_read_limit_from_config(self):
        """测试从配置读取限制值。"""
        from agent_py_agent.cli.memory_commands import _resolve_auto_read_limit

        mock_config = MagicMock()
        mock_config.memory_rule_auto_read_limit = 5

        result = _resolve_auto_read_limit(None, mock_config)
        assert result == 5


class TestBuildRoutingDoctor:
    """测试 _build_routing_doctor 辅助函数。"""

    def test_build_routing_doctor_index_not_exists(self, tmp_path: Path):
        """测试索引文件不存在的场景。"""
        from agent_py_agent.cli.memory_commands import _build_routing_doctor

        index_path = tmp_path / "nonexistent" / "INDEX.md"
        result = _build_routing_doctor(tmp_path, index_path)

        assert result["load_error"] != ""
        assert result["index"]["exists"] is False

    def test_build_routing_doctor_valid_index(self, tmp_path: Path):
        """测试有效索引文件的加载。"""
        from agent_py_agent.cli.memory_commands import _build_routing_doctor

        # 创建临时索引文件
        index_dir = tmp_path / "memory" / "routing"
        index_dir.mkdir(parents=True)
        index_path = index_dir / "INDEX.md"
        index_path.write_text("# Route Index\n", encoding="utf-8")

        result = _build_routing_doctor(tmp_path, index_path)

        assert result["load_error"] == ""
        assert result["index"]["exists"] is True


class TestBuildArchiveDoctor:
    """测试 _build_archive_doctor 辅助函数。"""

    def test_build_archive_doctor_basic(self, tmp_path: Path):
        """测试归档目录诊断。"""
        from agent_py_agent.cli.memory_commands import _build_archive_doctor

        mock_config = MagicMock()
        mock_config.memory_hook_retention_days = 7
        mock_config.memory_hook_enabled = True
        mock_config.memory_hook_archive_level = 3
        mock_config.memory_archive_level = 3

        result = _build_archive_doctor(tmp_path, mock_config)

        assert "retention" in result
        assert "hook" in result
        assert "raw" in result
        assert "snapshots" in result


class TestConfigWarnings:
    """测试 _config_warnings 辅助函数。"""

    def test_config_warnings_empty(self):
        """测试空 warnings 列表。"""
        from agent_py_agent.cli.memory_commands import _config_warnings

        mock_config = MagicMock()
        mock_config.memory_config_warnings = []

        result = _config_warnings(mock_config)
        assert result == []

    def test_config_warnings_dict_items(self):
        """测试字典格式的 warnings。"""
        from agent_py_agent.cli.memory_commands import _config_warnings

        mock_config = MagicMock()
        mock_config.memory_config_warnings = [
            {"field_name": "memory_hook_enabled", "reason": "配置值无效", "fallback_value": True}
        ]

        result = _config_warnings(mock_config)
        assert len(result) == 1
        assert result[0]["field_name"] == "memory_hook_enabled"

    def test_config_warnings_string_items(self):
        """测试字符串格式的 warnings。"""
        from agent_py_agent.cli.memory_commands import _config_warnings

        mock_config = MagicMock()
        mock_config.memory_config_warnings = ["警告信息"]

        result = _config_warnings(mock_config)
        assert len(result) == 1
        assert "message" in result[0]


class TestMemoryConfigPayload:
    """测试 _memory_config_payload 辅助函数。"""

    def test_memory_config_payload_fields(self):
        """测试配置字段序列化。"""
        from agent_py_agent.cli.memory_commands import _memory_config_payload

        mock_config = MagicMock()
        mock_config.memory_archive_level = 3
        mock_config.memory_hook_enabled = True
        mock_config.memory_hook_archive_level = 3
        mock_config.memory_hook_retention_days = 7
        mock_config.memory_rule_routing_enabled = True
        mock_config.memory_rule_routing_mode = "soft"
        mock_config.memory_rule_auto_read_limit = 3
        mock_config.memory_rule_receipt_enabled = True

        result = _memory_config_payload(mock_config)

        assert result["memory_archive_level"] == 3
        assert result["memory_hook_enabled"] is True
        assert result["memory_rule_routing_enabled"] is True


class TestIndexPayload:
    """测试 _index_payload 辅助函数。"""

    def test_index_payload_exists(self, tmp_path: Path):
        """测试存在的索引文件。"""
        from agent_py_agent.cli.memory_commands import _index_payload

        index_file = tmp_path / "INDEX.md"
        index_file.write_text("# Index\n", encoding="utf-8")

        result = _index_payload(index_file)

        assert result["exists"] is True
        assert result["is_file"] is True
        assert result["path"] == str(index_file)

    def test_index_payload_not_exists(self, tmp_path: Path):
        """测试不存在的索引文件。"""
        from agent_py_agent.cli.memory_commands import _index_payload

        index_file = tmp_path / "nonexistent.md"

        result = _index_payload(index_file)

        assert result["exists"] is False
        assert result["is_file"] is False
