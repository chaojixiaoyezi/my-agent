# LLM: 使用隔离夹具保留原业务断言，不以替代环境声称真实用户链路通过。
# 模块用途: 验证记忆路由和规范诊断辅助函数。
"""memory_route_commands CLI 命令测试。

测试 memory-route/memory-doctor 命令中的路由相关功能。
注意：memory_commands.py 中已经包含 route 相关命令，这里测试辅助函数。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# LLM: 维持原临时归档夹具与字段断言，不读取真实用户归档。
# 类用途: 核对规范服务的原归档摘要合同。
class TestBuildArchiveDoctor:
    """测试 _build_archive_doctor 辅助函数。"""

    # LLM: 不改变原样本和摘要断言，仅迁规范服务读取点。
    # 函数用途: 验证原归档文件摘要字段。
    def test_build_archive_doctor_basic(self, tmp_path: Path):
        """测试归档目录诊断。"""
        from agent_py_agent.agent.user_space.home_doctor import _build_archive_doctor

        mock_config = MagicMock()
        mock_config.memory_hook_enabled = True
        mock_config.memory_archive_level = 3

        result = _build_archive_doctor(tmp_path, mock_config)

        assert "retention" in result
        assert "hook" in result
        assert "raw" in result
        assert "snapshots" in result


class TestConfigWarnings:
    """测试 _config_warnings 辅助函数。"""

    def test_config_warnings_string_items(self):
        """测试字符串格式的 warnings。"""
        from agent_py_agent.cli.memory_commands import _config_warnings

        mock_config = MagicMock()
        mock_config.memory_config_warnings = ["警告信息"]

        result = _config_warnings(mock_config)
        assert len(result) == 1
        assert "message" in result[0]


# LLM: 摘要仅限原四字段，不扩大配置或敏感值展示。
# 类用途: 核对记忆诊断的原配置摘要。
class TestMemoryConfigPayload:
    """测试 _memory_config_payload 辅助函数。"""

    # LLM: 保留原字段和值断言，不用新形状掩盖配置丢失。
    # 函数用途: 验证规范服务的四项配置摘要。
    def test_memory_config_payload_fields(self):
        """测试配置字段序列化。"""
        from agent_py_agent.agent.user_space.home_doctor import _memory_config_payload

        mock_config = MagicMock()
        mock_config.memory_archive_level = 3
        mock_config.memory_hook_enabled = True
        mock_config.memory_rule_routing_mode = "soft"
        mock_config.memory_rule_auto_read_limit = 3

        result = _memory_config_payload(mock_config)

        assert result["memory_archive_level"] == 3
        assert result["memory_hook_enabled"] is True
        assert result["memory_rule_routing_mode"] == "soft"
