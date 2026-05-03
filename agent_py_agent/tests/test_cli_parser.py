from __future__ import annotations

"""LLM: tests for CLI parser module.

给人看的解释：
测试 CLI 参数解析器的命令注册、参数解析和子命令路由功能。
"""

import argparse
from unittest.mock import MagicMock, patch

import pytest


class TestBuildParser:
    """测试 build_parser 构建的解析器。"""

    def test_parser_creates_without_error(self) -> None:
        """测试解析器能成功创建。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        assert isinstance(parser, argparse.ArgumentParser)
        assert parser.prog == "my-agent"

    def test_default_command_is_cmd_default(self) -> None:
        """测试未指定命令时的默认处理函数。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        defaults = parser._defaults
        assert "func" in defaults
        assert defaults["func"].__name__ == "cmd_default"

    def test_parser_has_global_config_argument(self) -> None:
        """测试全局 --config 参数。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["--config", "/path/to/config.yaml"])
        assert args.config == "/path/to/config.yaml"

    def test_parser_has_subparsers(self) -> None:
        """测试解析器包含子命令解析器。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        assert parser._subparsers is not None


class TestSubcommandRegistration:
    """测试子命令注册。"""

    def test_status_subcommand_registered(self) -> None:
        """测试 status 子命令已注册。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        # 通过解析命令来验证子命令存在
        args = parser.parse_args(["status"])
        assert args.command == "status"

    def test_run_subcommand_registered(self) -> None:
        """测试 run 子命令已注册。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["run", "test"])
        assert args.command == "run"

    def test_chat_subcommand_registered(self) -> None:
        """测试 chat 子命令已注册。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["chat"])
        assert args.command == "chat"

    def test_subagents_subcommand_registered(self) -> None:
        """测试 subagents 子命令已注册。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["subagents"])
        assert args.command == "subagents"

    def test_gateway_subcommand_registered(self) -> None:
        """测试 gateway 子命令已注册。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["gateway"])
        assert args.command == "gateway"

    def test_memory_subcommands_registered(self) -> None:
        """测试 memory 相关子命令已注册。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["memory-route", "test"])
        assert args.command == "memory-route"

    def test_task_subcommands_registered(self) -> None:
        """测试 task 相关子命令已注册。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["task-show", "task-123"])
        assert args.command == "task-show"


class TestRunSubcommand:
    """测试 run 子命令参数解析。"""

    def test_run_with_prompt(self) -> None:
        """测试 run 子命令的基本解析。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["run", "测试 prompt"])
        assert args.prompt == "测试 prompt"
        assert args.func is not None

    def test_run_with_save_flag(self) -> None:
        """测试 run 子命令的 --save 参数。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["run", "test", "--save"])
        assert args.save is True

    def test_run_with_no_save_flag(self) -> None:
        """测试 run 子命令的 --no-save 参数。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["run", "test", "--no-save"])
        assert args.save is False

    def test_run_with_inject(self) -> None:
        """测试 run 子命令的 --inject 参数。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["run", "test", "--inject", "额外内容"])
        assert args.inject == ["额外内容"]

    def test_run_with_multiple_injects(self) -> None:
        """测试 run 子命令的多个 --inject 参数。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["run", "test", "--inject", "内容1", "--inject", "内容2"])
        assert args.inject == ["内容1", "内容2"]


class TestStatusSubcommand:
    """测试 status 子命令参数解析。"""

    def test_status_default_limit(self) -> None:
        """测试 status 默认 limit。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["status"])
        assert args.limit == 5

    def test_status_with_limit(self) -> None:
        """测试 status 自定义 limit。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["status", "--limit", "10"])
        assert args.limit == 10

    def test_status_with_recent(self) -> None:
        """测试 status 的 --recent 参数。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["status", "--recent"])
        assert args.recent is True


class TestChatSubcommand:
    """测试 chat 子命令参数解析。"""

    def test_chat_default_memory_limit(self) -> None:
        """测试 chat 默认 memory-limit。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["chat"])
        assert args.memory_limit == 5

    def test_chat_with_no_save(self) -> None:
        """测试 chat 的 --no-save 参数。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["chat", "--no-save"])
        assert args.no_save is True

    def test_chat_with_gateway(self) -> None:
        """测试 chat 的 --gateway 参数。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["chat", "--gateway"])
        assert args.gateway is True


class TestDispatchSubcommand:
    """测试 subagents-dispatch 子命令参数解析。"""

    def test_dispatch_default_dry_run(self) -> None:
        """测试 dispatch 默认 dry-run 模式。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["subagents-dispatch"])
        assert args.apply is False

    def test_dispatch_with_apply(self) -> None:
        """测试 dispatch 的 --apply 参数。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["subagents-dispatch", "--apply"])
        assert args.apply is True

    def test_dispatch_with_max_runners(self) -> None:
        """测试 dispatch 的 --max-runners 参数。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["subagents-dispatch", "--max-runners", "5"])
        assert args.max_runners == 5

    def test_dispatch_with_planner(self) -> None:
        """测试 dispatch 的 --planner 参数。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["subagents-dispatch", "--planner"])
        assert args.planner is True


class TestParserDefaults:
    """测试解析器的默认值配置。"""

    def test_parser_missing_command_uses_default(self) -> None:
        """测试未提供命令时使用默认函数。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args([])
        assert args.command is None

    def test_parser_unknown_command_error(self) -> None:
        """测试未知命令时的错误处理。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["unknown-command"])