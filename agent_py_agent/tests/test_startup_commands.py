"""startup_commands CLI 命令测试。

测试 daemon 命令和 startup recovery 相关功能。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestCmdDaemon:
    """测试 cmd_daemon 命令。"""

    def test_daemon_resolves_options(self, tmp_path: Path):
        """测试 daemon 选项解析。"""
        from agent_py_agent.cli.daemon import _resolve_daemon_options, cmd_daemon

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.capability_config = str(tmp_path / "capability.yaml")
        args.apply = False
        args.execute_runners = False
        args.planner = False
        args.interval = 60
        args.max_runners = 1
        args.limit = 10
        args.max_cycles = 1  # 改为 1 避免持续运行
        args.reviewer = None
        args.note = None
        args.instruction = None
        args.max_cards = 0
        args.no_probe = False
        args.take_over_by = None
        args.locked_file = []
        args.force_lock = False
        args.skill_dir = None

        mock_agent = MagicMock()
        mock_agent.config.daemon_apply = False
        mock_agent.config.daemon_execute_runners = False
        mock_agent.config.daemon_planner = False
        mock_agent.config.daemon_interval = 60
        mock_agent.config.daemon_max_runners = 1
        mock_agent.config.daemon_limit = 10
        mock_agent.config.daemon_max_cycles = 1
        mock_agent.config.daemon_reviewer = None
        mock_agent.config.daemon_runner_instruction = None
        mock_agent.config.daemon_max_cards = 0
        mock_agent.config.daemon_probe = True

        # 直接测试 _resolve_daemon_options 函数
        options = _resolve_daemon_options(mock_agent, args)
        assert options.apply is False
        assert options.interval == 60


class TestResolveDaemonMaxRunners:
    """测试 _resolve_daemon_max_runners 辅助函数。"""

    def test_resolve_daemon_max_runners_auto(self):
        """测试 auto 值转换为 1。"""
        from agent_py_agent.cli.daemon import _resolve_daemon_max_runners

        result = _resolve_daemon_max_runners("auto")
        assert result == 1

    def test_resolve_daemon_max_runners_empty_string(self):
        """测试空字符串转换为 1。"""
        from agent_py_agent.cli.daemon import _resolve_daemon_max_runners

        result = _resolve_daemon_max_runners("")
        assert result == 1

    def test_resolve_daemon_max_runners_integer(self):
        """测试整数直接返回。"""
        from agent_py_agent.cli.daemon import _resolve_daemon_max_runners

        result = _resolve_daemon_max_runners(5)
        assert result == 5

    def test_resolve_daemon_max_runners_invalid_string(self):
        """测试无效字符串抛出异常。"""
        from agent_py_agent.cli.daemon import _resolve_daemon_max_runners

        with pytest.raises(ValueError):
            _resolve_daemon_max_runners("invalid")


class TestValidateDaemonNumbers:
    """测试 _validate_daemon_numbers 辅助函数。"""

    def test_validate_daemon_numbers_valid(self):
        """测试有效参数。"""
        from agent_py_agent.cli.daemon import _validate_daemon_numbers

        result = _validate_daemon_numbers(
            interval=60,
            max_runners=2,
            limit=10,
            max_cycles=5,
            max_cards=3
        )
        assert result == ""

    def test_validate_daemon_numbers_negative_interval(self):
        """测试负数 interval。"""
        from agent_py_agent.cli.daemon import _validate_daemon_numbers

        result = _validate_daemon_numbers(
            interval=-1,
            max_runners=2,
            limit=10,
            max_cycles=5,
            max_cards=3
        )
        assert "不能小于 0" in result

    def test_validate_daemon_numbers_negative_max_runners(self):
        """测试负数 max_runners。"""
        from agent_py_agent.cli.daemon import _validate_daemon_numbers

        result = _validate_daemon_numbers(
            interval=60,
            max_runners=-1,
            limit=10,
            max_cycles=5,
            max_cards=3
        )
        assert "不能小于 0" in result

    def test_validate_daemon_numbers_negative_limit(self):
        """测试负数 limit。"""
        from agent_py_agent.cli.daemon import _validate_daemon_numbers

        result = _validate_daemon_numbers(
            interval=60,
            max_runners=2,
            limit=-1,
            max_cycles=5,
            max_cards=3
        )
        assert "不能小于 0" in result

    def test_validate_daemon_numbers_negative_max_cycles(self):
        """测试负数 max_cycles。"""
        from agent_py_agent.cli.daemon import _validate_daemon_numbers

        result = _validate_daemon_numbers(
            interval=60,
            max_runners=2,
            limit=10,
            max_cycles=-1,
            max_cards=3
        )
        assert "不能小于 0" in result

    def test_validate_daemon_numbers_negative_max_cards(self):
        """测试负数 max_cards。"""
        from agent_py_agent.cli.daemon import _validate_daemon_numbers

        result = _validate_daemon_numbers(
            interval=60,
            max_runners=2,
            limit=10,
            max_cycles=5,
            max_cards=-1
        )
        assert "不能小于 0" in result


class TestResolveDaemonOptions:
    """测试 _resolve_daemon_options 函数。"""

    def test_resolve_daemon_options_execute_runners_without_apply(self, tmp_path: Path):
        """测试 execute_runners 必须和 apply 一起使用。"""
        from agent_py_agent.cli.daemon import _resolve_daemon_options

        mock_agent = MagicMock()
        mock_agent.config.daemon_apply = False
        mock_agent.config.daemon_execute_runners = True
        mock_agent.config.daemon_planner = False
        mock_agent.config.daemon_interval = 60
        mock_agent.config.daemon_max_runners = 1
        mock_agent.config.daemon_limit = 10
        mock_agent.config.daemon_max_cycles = 0
        mock_agent.config.daemon_reviewer = None
        mock_agent.config.daemon_runner_instruction = None
        mock_agent.config.daemon_max_cards = 0
        mock_agent.config.daemon_probe = True

        args = MagicMock()
        args.apply = False
        args.execute_runners = True
        args.planner = None
        args.interval = None
        args.max_runners = None
        args.limit = None
        args.max_cycles = None
        args.max_cards = None
        args.reviewer = None
        args.instruction = None
        args.no_probe = False

        with pytest.raises(ValueError, match="必须和.*一起使用"):
            _resolve_daemon_options(mock_agent, args)
