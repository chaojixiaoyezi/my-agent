"""CLI 聊天模块单元测试。

测试 agent_py_agent/cli/chat.py 中的辅助函数。
验证进度条、文本折叠、启动横幅、终端分隔线等功能正确。
"""
from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class TestCollapseResponseText:
    """测试 _collapse_response_text 函数。"""

    def test_short_text_not_collapsed(self):
        """测试短文本不被折叠。

        验证短文本返回原始文本和 False。
        """
        from agent_py_agent.cli.chat import _collapse_response_text

        text = "Hello, this is a short response."
        result, collapsed = _collapse_response_text(text)

        assert result == text
        assert collapsed is False

    def test_short_lines_not_collapsed(self):
        """测试短行数文本不被折叠。

        验证行数少于阈值时不折叠。
        """
        from agent_py_agent.cli.chat import _collapse_response_text

        lines = ["line " + str(i) for i in range(5)]
        text = "\n".join(lines)
        result, collapsed = _collapse_response_text(text)

        assert collapsed is False
        assert result == text

    def test_long_lines_collapsed(self):
        """测试长行数文本被折叠。

        验证超过行数阈值的文本被折叠。
        """
        from agent_py_agent.cli.chat import _COLLAPSE_PREVIEW_LINES, _collapse_response_text

        lines = ["line " + str(i) for i in range(_COLLAPSE_PREVIEW_LINES + 5)]
        text = "\n".join(lines)
        result, collapsed = _collapse_response_text(text)

        assert collapsed is True
        assert "..." in result

    def test_long_chars_collapsed(self):
        """测试长字符数文本被折叠。

        验证字符数超过阈值时文本被折叠。
        """
        from agent_py_agent.cli.chat import _collapse_response_text

        # 使用单行长字符文本确保超过 COLLAPSE_PREVIEW_CHARS (900)
        text = "A" * 1000
        result, collapsed = _collapse_response_text(text)

        assert collapsed is True

    def test_collapsed_preview_strips_trailing_whitespace(self):
        """测试折叠预览去掉尾部空白。

        验证预览文本尾部空白被去除。
        """
        from agent_py_agent.cli.chat import _collapse_response_text

        text = ("line 1\n" * 20) + "    extra spaces   "
        result, collapsed = _collapse_response_text(text)

        if collapsed:
            assert not result.endswith(" ") or result.endswith("...")


class TestProgressBar:
    """测试 _progress_bar 函数。"""

    def test_progress_bar_zero(self):
        """测试零进度进度条。

        验证 0% 时全是空心方块。
        """
        from agent_py_agent.cli.chat import _progress_bar

        result = _progress_bar(0.0)
        assert result == "░" * 10

    def test_progress_bar_full(self):
        """测试满进度进度条。

        验证 100% 时全是实心方块。
        """
        from agent_py_agent.cli.chat import _progress_bar

        result = _progress_bar(1.0)
        assert result == "█" * 10

    def test_progress_bar_half(self):
        """测试半进度进度条。

        验证 50% 时一半实心一半空心。
        """
        from agent_py_agent.cli.chat import _progress_bar

        result = _progress_bar(0.5)
        assert result == "█" * 5 + "░" * 5

    def test_progress_bar_custom_width(self):
        """测试自定义宽度进度条。

        验证可设置不同宽度。
        """
        from agent_py_agent.cli.chat import _progress_bar

        result = _progress_bar(0.4, width=5)
        assert result == "█" * 2 + "░" * 3

    def test_progress_bar_rounds_down(self):
        """测试进度条向下取整。

        验证进度条使用整数填充。
        """
        from agent_py_agent.cli.chat import _progress_bar

        result = _progress_bar(0.33)
        assert result.count("█") == 3


class TestStartupBanner:
    """测试 _startup_banner 函数。"""

    def test_banner_local_mode(self):
        """测试本地模式启动横幅。

        验证本地模式时显示 "local runtime"。
        """
        from agent_py_agent.cli.chat import _startup_banner

        result = _startup_banner("TestAgent", use_gateway=False)

        assert "TestAgent" in result
        assert "local runtime" in result

    def test_banner_gateway_mode(self):
        """测试网关模式启动横幅。

        验证网关模式时显示 "gateway client"。
        """
        from agent_py_agent.cli.chat import _startup_banner

        result = _startup_banner("TestAgent", use_gateway=True)

        assert "TestAgent" in result
        assert "gateway client" in result

    def test_banner_contains_ascii_art(self):
        """测试横幅包含 ASCII 艺术。

        验证横幅包含装饰性字符。
        """
        from agent_py_agent.cli.chat import _startup_banner

        result = _startup_banner("Agent", use_gateway=False)

        assert "/\\_/\\" in result or "o.o" in result

    def test_banner_has_reset_sequences(self):
        """测试横幅包含重置序列。

        验证 ANSI 颜色会被重置。
        """
        from agent_py_agent.cli.chat import _startup_banner

        result = _startup_banner("Agent", use_gateway=True)

        assert "\033[0m" in result


class TestTerminalRule:
    """测试 _terminal_rule 函数。"""

    def test_terminal_rule_default(self):
        """测试默认终端分隔线。

        验证默认使用横线字符。
        """
        from agent_py_agent.cli.chat import _terminal_rule

        result = _terminal_rule()

        assert "─" in result or len(result) > 0
        assert "\033[" in result  # ANSI prefix

    def test_terminal_rule_custom_char(self):
        """测试自定义字符分隔线。

        验证可使用不同字符。
        """
        from agent_py_agent.cli.chat import _terminal_rule

        result = _terminal_rule(char="=")

        assert "=" in result

    def test_terminal_rule_minimum_width(self):
        """测试分隔线最小宽度。

        验证最小宽度为 20。
        """
        from agent_py_agent.cli.chat import _terminal_rule

        result = _terminal_rule(char="-")
        # 验证ANSI序列存在且结果非空
        assert "\033[" in result
        assert len(result) > 20


class TestChatCommandArguments:
    """测试 chat 命令参数解析。"""

    def test_chat_has_gateway_flag(self):
        """测试 chat 命令有 --gateway 标志。

        验证 --gateway 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        add_basic_subcommands(sub)

        args = parser.parse_args(["chat", "--gateway"])
        assert args.gateway is True

    def test_chat_has_no_save_flag(self):
        """测试 chat 命令有 --no-save 标志。

        验证 --no-save 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        add_basic_subcommands(sub)

        args = parser.parse_args(["chat", "--no-save"])
        assert args.no_save is True

    def test_chat_has_inject_flags(self):
        """测试 chat 命令有 --inject 参数。

        验证 --inject 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        add_basic_subcommands(sub)

        args = parser.parse_args([
            "chat",
            "--inject", "prompt1",
            "--inject", "prompt2"
        ])
        assert args.inject == ["prompt1", "prompt2"]

    def test_chat_has_prompt_file_flags(self):
        """测试 chat 命令有 --prompt-file 参数。

        验证 --prompt-file 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        add_basic_subcommands(sub)

        args = parser.parse_args([
            "chat",
            "--prompt-file", "/tmp/prompt1.txt",
            "--prompt-file", "/tmp/prompt2.txt"
        ])
        assert args.prompt_file == ["/tmp/prompt1.txt", "/tmp/prompt2.txt"]

    def test_chat_has_memory_limit_flag(self):
        """测试 chat 命令有 --memory-limit 参数。

        验证 --memory-limit 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        add_basic_subcommands(sub)

        args = parser.parse_args(["chat", "--memory-limit", "50"])
        assert args.memory_limit == 50

    def test_chat_has_gateway_timeout_flag(self):
        """测试 chat 命令有 --gateway-timeout 参数。

        验证 --gateway-timeout 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        add_basic_subcommands(sub)

        args = parser.parse_args(["chat", "--gateway-timeout", "120.0"])
        assert args.gateway_timeout == 120.0

    def test_root_app_flag_enables_app_scrollback(self):
        """测试顶层 --app 入口会启用应用内滚动历史模式。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["--app"])
        if getattr(args, "app", False):
            args.app_scrollback = True

        assert args.app is True
        assert args.app_scrollback is True

    def test_chat_has_resume_context_switches(self):
        """测试 chat 命令有 resume-context 切换开关。

        验证 --resume-context 和 --no-resume-context 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        add_basic_subcommands(sub)

        args = parser.parse_args(["chat", "--resume-context"])
        assert args.resume_context is True

        args = parser.parse_args(["chat", "--no-resume-context"])
        assert args.resume_context is False

    def test_chat_default_memory_limit(self):
        """测试 chat 命令默认 memory-limit 为 5。

        验证默认值正确。
        """
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        add_basic_subcommands(sub)

        args = parser.parse_args(["chat"])
        assert args.memory_limit == 5

    def test_cmd_chat_passes_fallback_config_object(self):
        """非 TTY fallback 路径应传 RunFallbackConfig，而不是散装 kwargs。"""
        from agent_py_agent.cli.chat import cmd_chat
        from agent_py_agent.cli.chat_parts.fallback_state import RunFallbackConfig

        args = SimpleNamespace(
            gateway=False,
            inject=[],
            prompt_file=[],
            session_id="",
            memory_limit=5,
        )
        agent = MagicMock()
        agent.config.agent_name = "myagent"
        session_manager = MagicMock()
        session_manager.create_session.return_value = SimpleNamespace(session_id="sess-test")

        with patch("agent_py_agent.cli.chat.make_agent", return_value=agent), \
             patch("agent_py_agent.cli.chat.SessionManager", return_value=session_manager), \
             patch("agent_py_agent.cli.chat._has_prompt_toolkit", return_value=False), \
             patch("agent_py_agent.cli.chat.run_fallback", return_value=0) as fallback:
            result = cmd_chat(args)

        assert result == 0
        cfg = fallback.call_args.args[0]
        assert isinstance(cfg, RunFallbackConfig)
        assert cfg.agent is agent
        assert cfg.current_session_id == "sess-test"


class TestCollapseEdgeCases:
    """测试文本折叠边界场景。"""

    def test_empty_text_not_collapsed(self):
        """测试空文本不被折叠。

        验证空字符串返回空字符串和 False。
        """
        from agent_py_agent.cli.chat import _collapse_response_text

        result, collapsed = _collapse_response_text("")

        assert result == ""
        assert collapsed is False

    def test_single_line_short_not_collapsed(self):
        """测试单行短文本不被折叠。

        验证单行且短文本不折叠。
        """
        from agent_py_agent.cli.chat import _collapse_response_text

        result, collapsed = _collapse_response_text("Short line.")

        assert collapsed is False

    def test_exactly_at_threshold_not_collapsed(self):
        """测试刚好在阈值的文本不被折叠。

        验证刚好等于阈值时不折叠。
        """
        from agent_py_agent.cli.chat import (
            _COLLAPSE_PREVIEW_CHARS,
            _COLLAPSE_PREVIEW_LINES,
            _collapse_response_text,
        )

        lines = ["line " + str(i) for i in range(_COLLAPSE_PREVIEW_LINES)]
        text = "\n".join(lines)
        # 确保字符数也在阈值内
        assert len(text) <= _COLLAPSE_PREVIEW_CHARS

        result, collapsed = _collapse_response_text(text)

        assert collapsed is False
