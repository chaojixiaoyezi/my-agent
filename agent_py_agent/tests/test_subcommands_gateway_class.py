"""CLI 网关子命令测试。

测试 agent_py_agent/cli/subcommands_gateway.py 中的子命令注册函数。
验证网关相关命令（start、stop、status、restart）参数解析正确。
"""
from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest


class TestGatewaySubcommandRegistration:
    """测试网关子命令注册功能。"""

    def test_add_gateway_subcommands_creates_expected_commands(self):
        """测试网关子命令注册创建预期的命令。

        验证所有网关子命令都能被正确注册到解析器。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        add_gateway_subcommands(sub)

        # gateway 是一个子解析器，需要从 gateway 的子解析器中查找
        gateway_parser = parser._subparsers._actions[1].choices["gateway"]
        gateway_subparsers = gateway_parser._subparsers._actions[1]

        # 验证所有预期的子命令都存在
        expected_commands = [
            "start",
            "run",
            "status",
            "stop",
            "restart",
            "logs",
            "ask",
            "result",
        ]

        for cmd in expected_commands:
            assert cmd in gateway_subparsers.choices, f"Command {cmd} not found"

    def test_gateway_start_has_force_flag(self):
        """测试 gateway start 命令有 --force 标志。

        验证 --force 标志正确工作。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "start", "--force"])
        assert args.force is True

    def test_gateway_run_does_not_expose_dispatch_planner_flags(self):
        """The formal Gateway is not a process-wide subagent daemon."""
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        with pytest.raises(SystemExit):
            parser.parse_args(["gateway", "run", "--planner"])

    def test_gateway_stop_has_timeout_flag(self):
        """测试 gateway stop 命令有 --timeout 标志。

        验证 --timeout 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "stop", "--timeout", "30.0"])
        assert args.timeout == 30.0

    def test_gateway_stop_has_kill_flag(self):
        """测试 gateway stop 命令有 --kill 标志。

        验证 --kill 标志正确工作。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "stop", "--kill"])
        assert args.kill is True

    def test_gateway_stop_has_reason_flag(self):
        """测试 gateway stop 命令有 --reason 标志。

        验证 --reason 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "stop", "--reason", "维护停机"])
        assert args.reason == "维护停机"

    def test_gateway_restart_has_timeout_flag(self):
        """测试 gateway restart 命令有 --timeout 标志。

        验证 --timeout 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "restart", "--timeout", "60.0"])
        assert args.timeout == 60.0

    def test_gateway_restart_has_force_flag(self):
        """测试 gateway restart 命令有 --force 标志。

        验证 --force 标志正确工作。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "restart", "--force"])
        assert args.force is True

    def test_gateway_logs_has_lines_flag(self):
        """测试 gateway logs 命令有 --lines 标志。

        验证 --lines 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "logs", "--lines", "100"])
        assert args.lines == 100

    def test_gateway_logs_default_lines(self):
        """测试 gateway logs 命令默认行数。

        验证默认 lines 值为 80。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "logs"])
        assert args.lines == 80


class TestGatewayAskCommand:
    """测试 gateway ask 命令。"""

    def test_gateway_ask_has_prompt_argument(self):
        """测试 gateway ask 命令有 prompt 参数。

        验证 prompt 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "ask", "测试提示"])
        assert args.prompt == "测试提示"

    def test_gateway_ask_has_inject_flag(self):
        """测试 gateway ask 命令有 --inject 标志。

        验证 --inject 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args([
            "gateway", "ask", "测试",
            "--inject", "额外提示1",
            "--inject", "额外提示2"
        ])
        assert args.inject == ["额外提示1", "额外提示2"]

    def test_gateway_ask_has_no_save_flag(self):
        """测试 gateway ask 命令有 --no-save 标志。

        验证 --no-save 标志正确工作。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "ask", "测试", "--no-save"])
        assert args.no_save is True

    def test_gateway_ask_has_timeout_flag(self):
        """测试 gateway ask 命令有 --timeout 标志。

        验证 --timeout 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "ask", "测试", "--timeout", "120.0"])
        assert args.timeout == 120.0

    def test_gateway_ask_has_no_wait_flag(self):
        """测试 gateway ask 命令有 --no-wait 标志。

        验证 --no-wait 标志正确工作。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "ask", "测试", "--no-wait"])
        assert args.no_wait is True

    def test_gateway_ask_has_json_flag(self):
        """测试 gateway ask 命令有 --json 标志。

        验证 --json 标志正确工作。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "ask", "测试", "--json"])
        assert args.json is True


class TestGatewayResultCommand:
    """测试 gateway result 命令。"""

    def test_gateway_result_has_request_id_argument(self):
        """测试 gateway result 命令有 request_id 参数。

        验证 request_id 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "result", "req-123"])
        assert args.request_id == "req-123"

    def test_gateway_result_has_show_prompt_flag(self):
        """测试 gateway result 命令有 --show-prompt 标志。

        验证 --show-prompt 标志正确工作。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "result", "req-123", "--show-prompt"])
        assert args.show_prompt is True


class TestSupervisorSubcommands:
    """测试 supervisor 相关子命令。"""

    def test_supervisor_start_command_exists(self):
        """测试 supervisor-start 命令存在。

        验证命令被正确注册。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "supervisor-start"])
        assert hasattr(args, "func")

    def test_supervisor_stop_command_has_timeout(self):
        """测试 supervisor-stop 命令有 --timeout 参数。

        验证 --timeout 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "supervisor-stop", "--timeout", "30.0"])
        assert args.timeout == 30.0

    def test_start_all_command_exists(self):
        """测试 start-all 命令存在。

        验证命令被正确注册。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "start-all"])
        assert hasattr(args, "func")

    def test_start_all_has_adapter_choice(self):
        """测试 start-all 命令有 --adapter 选项。

        验证 --adapter 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_gateway import add_gateway_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="gateway_command")
        add_gateway_subcommands(sub)

        args = parser.parse_args(["gateway", "start-all", "--adapter", "qq"])
        assert args.adapter == "qq"
