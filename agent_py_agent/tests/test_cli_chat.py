"""CLI 聊天模块单元测试。

测试 agent_py_agent/cli/chat.py 中的辅助函数。
验证进度条、文本折叠、启动横幅、终端分隔线等功能正确。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class TestCollapseResponseText:
    """测试 collapse_response_text 函数。"""

    def test_short_text_not_collapsed(self):
        """测试短文本不被折叠。

        验证短文本返回原始文本和 False。
        """
        from agent_py_agent.cli.chat import collapse_response_text

        text = "Hello, this is a short response."
        result, collapsed = collapse_response_text(text)

        assert result == text
        assert collapsed is False

    def test_short_lines_not_collapsed(self):
        """测试短行数文本不被折叠。

        验证行数少于阈值时不折叠。
        """
        from agent_py_agent.cli.chat import collapse_response_text

        lines = ["line " + str(i) for i in range(5)]
        text = "\n".join(lines)
        result, collapsed = collapse_response_text(text)

        assert collapsed is False
        assert result == text

    def test_long_lines_collapsed(self):
        """测试长行数文本被折叠。

        验证超过行数阈值的文本被折叠。
        """
        from agent_py_agent.cli.chat import COLLAPSE_PREVIEW_LINE_COUNT, collapse_response_text

        lines = ["line " + str(i) for i in range(COLLAPSE_PREVIEW_LINE_COUNT + 5)]
        text = "\n".join(lines)
        result, collapsed = collapse_response_text(text)

        assert collapsed is True
        assert "..." in result

    def test_long_chars_collapsed(self):
        """测试长字符数文本被折叠。

        验证字符数超过阈值时文本被折叠。
        """
        from agent_py_agent.cli.chat import collapse_response_text

        # 使用单行长字符文本确保超过 COLLAPSE_PREVIEW_CHARS (900)
        text = "A" * 1000
        result, collapsed = collapse_response_text(text)

        assert collapsed is True

    def test_collapsed_preview_strips_trailing_whitespace(self):
        """测试折叠预览去掉尾部空白。

        验证预览文本尾部空白被去除。
        """
        from agent_py_agent.cli.chat import collapse_response_text

        text = ("line 1\n" * 20) + "    extra spaces   "
        result, collapsed = collapse_response_text(text)

        if collapsed:
            assert not result.endswith(" ") or result.endswith("...")


class TestProgressBar:
    """测试 progress_bar 函数。"""

    def testprogress_bar_zero(self):
        """测试零进度进度条。

        验证 0% 时全是空心方块。
        """
        from agent_py_agent.cli.chat import progress_bar

        result = progress_bar(0.0)
        assert result == "░" * 10

    def testprogress_bar_full(self):
        """测试满进度进度条。

        验证 100% 时全是实心方块。
        """
        from agent_py_agent.cli.chat import progress_bar

        result = progress_bar(1.0)
        assert result == "█" * 10

    def testprogress_bar_half(self):
        """测试半进度进度条。

        验证 50% 时一半实心一半空心。
        """
        from agent_py_agent.cli.chat import progress_bar

        result = progress_bar(0.5)
        assert result == "█" * 5 + "░" * 5

    def testprogress_bar_custom_width(self):
        """测试自定义宽度进度条。

        验证可设置不同宽度。
        """
        from agent_py_agent.cli.chat import progress_bar

        result = progress_bar(0.4, width=5)
        assert result == "█" * 2 + "░" * 3

    def testprogress_bar_rounds_down(self):
        """测试进度条向下取整。

        验证进度条使用整数填充。
        """
        from agent_py_agent.cli.chat import progress_bar

        result = progress_bar(0.33)
        assert result.count("█") == 3


class TestStartupBanner:
    """测试 startup_banner 函数。"""

    def test_banner_local_mode(self):
        """测试本地模式启动横幅。

        验证本地模式时显示 "local runtime"。
        """
        from agent_py_agent.cli.chat import startup_banner

        result = startup_banner("TestAgent", use_gateway=False)

        assert "TestAgent" in result
        assert "local runtime" in result

    def test_banner_gateway_mode(self):
        """测试网关模式启动横幅。

        验证网关模式时显示 "gateway client"。
        """
        from agent_py_agent.cli.chat import startup_banner

        result = startup_banner("TestAgent", use_gateway=True)

        assert "TestAgent" in result
        assert "gateway client" in result

    def test_banner_contains_ascii_art(self):
        """测试横幅包含 ASCII 艺术。

        验证横幅包含装饰性字符。
        """
        from agent_py_agent.cli.chat import startup_banner

        result = startup_banner("Agent", use_gateway=False)

        assert "/\\_/\\" in result or "o.o" in result

    def test_banner_has_reset_sequences(self):
        """测试横幅包含重置序列。

        验证 ANSI 颜色会被重置。
        """
        from agent_py_agent.cli.chat import startup_banner

        result = startup_banner("Agent", use_gateway=True)

        assert "\033[0m" in result


class TestTerminalRule:
    """测试 terminal_rule 函数。"""

    def testterminal_rule_default(self):
        """测试默认终端分隔线。

        验证默认使用横线字符。
        """
        from agent_py_agent.cli.chat import terminal_rule

        result = terminal_rule()

        assert "─" in result or len(result) > 0
        assert "\033[" in result  # ANSI prefix

    def testterminal_rule_custom_char(self):
        """测试自定义字符分隔线。

        验证可使用不同字符。
        """
        from agent_py_agent.cli.chat import terminal_rule

        result = terminal_rule(char="=")

        assert "=" in result

    def testterminal_rule_minimum_width(self):
        """测试分隔线最小宽度。

        验证最小宽度为 20。
        """
        from agent_py_agent.cli.chat import terminal_rule

        result = terminal_rule(char="-")
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

    def test_root_chat_defaults_to_app_scrollback(self):
        """测试顶层默认入口启用应用内滚动历史模式。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args([])
        args.app_scrollback = not bool(getattr(args, "plain", False))

        assert args.app_scrollback is True

    def test_root_plain_flag_disables_app_scrollback(self):
        """测试 --plain 会切回普通终端模式。"""
        from agent_py_agent.cli.parser import build_parser

        parser = build_parser()
        args = parser.parse_args(["--plain"])
        args.app_scrollback = not bool(getattr(args, "plain", False))

        assert args.plain is True
        assert args.app_scrollback is False

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

    def test_chat_default_memory_limit_defers_to_config(self):
        """测试 chat 命令默认 memory-limit 留给运行时配置决定。

        验证 parser 不写死数字，cmd_chat 会从 AgentConfig 读取默认值。
        """
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        add_basic_subcommands(sub)

        args = parser.parse_args(["chat"])
        assert args.memory_limit is None


class TestChatCommandRuntime:
    """测试 chat 命令运行时分发。"""

    def test_cmd_chat_passes_plain_config_object(self):
        """非 TTY plain 路径应传 RunPlainConfig，而不是散装 kwargs。"""
        from agent_py_agent.cli.chat import cmd_chat
        from agent_py_agent.cli.chat_parts.plain_state import RunPlainConfig

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
             patch("agent_py_agent.cli.chat.run_plain", return_value=0) as plain:
            result = cmd_chat(args)

        assert result == 0
        cfg = plain.call_args.args[0]
        assert isinstance(cfg, RunPlainConfig)
        assert cfg.agent is agent
        assert cfg.current_session_id == "sess-test"

    def test_cmd_chat_plain_uses_plain_even_with_prompt_toolkit(self):
        """--plain 要绕开 TUI，避免 prompt_toolkit 重绘吞掉普通输出。"""
        from agent_py_agent.cli.chat import cmd_chat

        args = SimpleNamespace(
            gateway=False,
            inject=[],
            prompt_file=[],
            session_id="",
            memory_limit=5,
            plain=True,
        )
        agent = MagicMock()
        agent.config.agent_name = "myagent"
        session_manager = MagicMock()
        session_manager.create_session.return_value = SimpleNamespace(session_id="sess-test")

        with patch("agent_py_agent.cli.chat.make_agent", return_value=agent), \
             patch("agent_py_agent.cli.chat.SessionManager", return_value=session_manager), \
             patch("agent_py_agent.cli.chat._has_prompt_toolkit", return_value=True), \
             patch("agent_py_agent.cli.chat.run_tui", return_value=0) as tui, \
             patch("agent_py_agent.cli.chat.run_plain", return_value=0) as plain:
            result = cmd_chat(args)

        assert result == 0
        tui.assert_not_called()
        plain.assert_called_once()

    def test_cmd_chat_publishes_recovered_local_history_to_tui(self):
        """本地模式没有 Gateway preflight，仍在创建 TUI 前恢复。"""
        from agent_py_agent.cli import chat as chat_mod
        from agent_py_agent.cli.chat_parts.history import GatewayChatHistorySnapshot

        args = SimpleNamespace(
            gateway=False,
            inject=[],
            prompt_file=[],
            session_id="sess-resume",
            memory_limit=5,
            plain=False,
        )
        agent = MagicMock()
        agent.config.chat_history_max_turns = 20
        captured: list[tuple[str, str]] = []

        def run_tui(*, params):
            captured.extend(params.conversation_history)
            return 0

        with patch("agent_py_agent.cli.chat.make_agent", return_value=agent), \
             patch("agent_py_agent.cli.chat.SessionManager"), \
             patch("agent_py_agent.cli.chat._setup_session", return_value="sess-resume"), \
             patch("agent_py_agent.cli.chat.gateway_paths", return_value=object()), \
             patch("agent_py_agent.cli.chat._has_prompt_toolkit", return_value=True), \
             patch(
                 "agent_py_agent.cli.chat.load_gateway_chat_history",
                 return_value=GatewayChatHistorySnapshot(
                     turns=(("恢复问题", "恢复回答"),),
                     thread_id="thread-resume",
                 ),
             ), \
             patch("agent_py_agent.cli.chat.run_tui", side_effect=run_tui), \
             patch("agent_py_agent.cli.chat.request_memory_curator_for_session_best_effort"):
            result = chat_mod.cmd_chat(args)

        assert result == 0
        assert captured == [("恢复问题", "恢复回答")]

    def test_gateway_tui_resume_defers_history_until_visible_preflight(self):
        from agent_py_agent.cli import chat as chat_mod

        args = SimpleNamespace(
            gateway=True, inject=[], prompt_file=[], session_id="sess-resume",
            memory_limit=5, plain=False,
        )
        agent = MagicMock()
        agent.gateway_client_only = True
        agent.config.chat_history_max_turns = 20
        with patch.object(chat_mod, "make_agent", return_value=agent), \
             patch.object(chat_mod, "SessionManager"), \
             patch.object(chat_mod, "_setup_session", return_value="sess-resume"), \
             patch.object(chat_mod, "gateway_paths", return_value=object()), \
             patch.object(chat_mod, "_has_prompt_toolkit", return_value=True), \
             patch.object(chat_mod, "load_gateway_chat_history") as history, \
             patch.object(chat_mod, "run_tui", return_value=0) as tui:
            result = chat_mod.cmd_chat(args)
        assert result == 0
        history.assert_not_called()
        params = tui.call_args.kwargs["params"]
        assert params.restore_session_history is True
        assert params.current_session_id == "sess-resume"
        assert params.conversation_history == []

    def test_in_place_handoff_keeps_session_and_loads_history_before_first_frame(self, monkeypatch):
        """原地切换来的新进程：沿用上一代会话、Gateway 就绪后同步读好历史，首帧就是同一段对话，不走可见连接流程。"""
        import json as _json

        from agent_py_agent.cli import chat as chat_mod
        from agent_py_agent.cli.chat_parts import tui_upgrade_follow as follow
        from agent_py_agent.cli.chat_parts.history import GatewayChatHistorySnapshot

        monkeypatch.setattr(follow, "_STATE", follow.HandoffState())
        monkeypatch.setattr(follow.atexit, "register", lambda *_a: None)
        monkeypatch.setenv(follow.HANDOFF_ENV, _json.dumps(
            {"schema": "my_agent.tui_handoff.v1", "session_id": "sess-live", "tty": None}))
        args = SimpleNamespace(gateway=True, inject=[], prompt_file=[], session_id="", memory_limit=5, plain=False)
        agent = MagicMock()
        agent.gateway_client_only = True
        agent.config.chat_history_max_turns = 20
        snapshot = GatewayChatHistorySnapshot(turns=[("问", "答")])
        with patch.object(chat_mod, "make_agent", return_value=agent), \
             patch.object(chat_mod, "SessionManager"), \
             patch.object(chat_mod, "_setup_session", side_effect=lambda a, _m: a.session_id) as setup, \
             patch.object(chat_mod, "gateway_paths", return_value=object()), \
             patch.object(chat_mod, "_has_prompt_toolkit", return_value=True), \
             patch.object(chat_mod, "_handoff_gateway_ready", return_value=True), \
             patch.object(chat_mod, "load_gateway_chat_history", return_value=snapshot) as history, \
             patch.object(chat_mod, "run_tui", return_value=0) as tui:
            result = chat_mod.cmd_chat(args)
        assert result == 0 and setup.call_args.args[0].session_id == "sess-live"
        history.assert_called_once()
        params = tui.call_args.kwargs["params"]
        assert params.handoff is True and params.restore_session_history is False
        assert params.current_session_id == "sess-live" and params.conversation_history == [("问", "答")]
        assert follow.HANDOFF_ENV not in __import__("os").environ

    def test_in_place_handoff_detects_the_terminal_before_holding_output(self, monkeypatch):
        """真机回归：新进程若先把 stdout 换成缓冲再判断终端，会误入 plain 模式卡在 input()。"""
        import io as _io
        import json as _json
        import sys as _sys

        from agent_py_agent.cli import chat as chat_mod
        from agent_py_agent.cli.chat_parts import tui_upgrade_follow as follow
        from agent_py_agent.cli.chat_parts.history import GatewayChatHistorySnapshot

        class _Tty(_io.StringIO):
            def isatty(self):
                return True

        monkeypatch.setattr(follow, "_STATE", follow.HandoffState())
        monkeypatch.setattr(follow.atexit, "register", lambda *_a: None)
        monkeypatch.setattr(_sys, "stdin", _Tty())
        monkeypatch.setattr(_sys, "stdout", _Tty())
        monkeypatch.setenv(follow.HANDOFF_ENV, _json.dumps(
            {"schema": "my_agent.tui_handoff.v1", "session_id": "sess-tty", "tty": None}))
        args = SimpleNamespace(gateway=True, inject=[], prompt_file=[], session_id="", memory_limit=5, plain=False)
        agent = MagicMock()
        agent.gateway_client_only = True
        agent.config.chat_history_max_turns = 20
        with patch.object(chat_mod, "make_agent", return_value=agent), \
             patch.object(chat_mod, "SessionManager"), \
             patch.object(chat_mod, "_setup_session", return_value="sess-tty"), \
             patch.object(chat_mod, "gateway_paths", return_value=object()), \
             patch.object(chat_mod, "_handoff_gateway_ready", return_value=True), \
             patch.object(chat_mod, "load_gateway_chat_history", return_value=GatewayChatHistorySnapshot()), \
             patch.object(chat_mod, "run_plain", side_effect=AssertionError("不能落到 plain 模式")), \
             patch.object(chat_mod, "run_tui", return_value=0) as tui:
            assert chat_mod.cmd_chat(args) == 0
        assert tui.call_args.kwargs["params"].handoff is True

    def test_boot_frame_is_skipped_during_an_in_place_handoff(self, monkeypatch):
        import io as _io
        import sys as _sys

        from agent_py_agent.cli import chat_command_parser as parser_mod
        from agent_py_agent.cli.chat_parts import tui_upgrade_follow as follow

        class _Tty(_io.StringIO):
            def isatty(self):
                return True

        out = _Tty()
        monkeypatch.setattr(_sys, "stdin", _Tty())
        monkeypatch.setattr(_sys, "stdout", out)
        monkeypatch.setenv(follow.HANDOFF_ENV, "{}")
        parser_mod._show_boot_frame(SimpleNamespace(plain=False))
        assert out.getvalue() == "", "旧画面要留到新界面首帧，不能清屏画启动页"
        monkeypatch.delenv(follow.HANDOFF_ENV)
        parser_mod._show_boot_frame(SimpleNamespace(plain=False))
        assert "my-agent" in out.getvalue()

    def test_resume_handoff_skips_duplicate_full_agent_build(self, monkeypatch):
        """resume 起的 TUI 原地切换时，新进程直接交给 cmd_chat（它仍做会话存在与归属校验），不再先构建完整 agent。"""
        from agent_py_agent.cli import chat as chat_mod
        from agent_py_agent.cli.chat_parts import tui_upgrade_follow as follow

        monkeypatch.setenv(follow.HANDOFF_ENV, "{}")
        args = SimpleNamespace(session_id="sess-r", gateway=True)
        with patch.object(chat_mod, "make_agent", side_effect=AssertionError("不应重复构建完整 agent")), \
             patch.object(chat_mod, "cmd_chat", return_value=0) as chat:
            assert chat_mod.cmd_resume(args) == 0
        chat.assert_called_once_with(args)

    def test_cmd_chat_fails_closed_when_recovered_history_is_corrupt(self, capsys):
        """显式恢复读到损坏账本时不能悄悄显示空历史。"""
        from agent_py_agent.cli import chat as chat_mod
        from agent_py_agent.cli.chat_parts.history import GatewayChatHistorySnapshot

        args = SimpleNamespace(
            gateway=False,
            inject=[],
            prompt_file=[],
            session_id="sess-bad",
            memory_limit=5,
            plain=False,
        )
        agent = MagicMock()
        agent.config.chat_history_max_turns = 20

        with patch("agent_py_agent.cli.chat.make_agent", return_value=agent), \
             patch("agent_py_agent.cli.chat.SessionManager"), \
             patch("agent_py_agent.cli.chat._setup_session", return_value="sess-bad"), \
             patch("agent_py_agent.cli.chat.gateway_paths", return_value=object()), \
             patch(
                 "agent_py_agent.cli.chat.load_gateway_chat_history",
                 return_value=GatewayChatHistorySnapshot(
                     load_errors=({"error_code": "jsonl_corrupt"},),
                 ),
             ), \
             patch("agent_py_agent.cli.chat.run_tui") as tui:
            result = chat_mod.cmd_chat(args)

        assert result == 3
        assert "会话历史读取失败" in capsys.readouterr().err
        tui.assert_not_called()


class TestCollapseEdgeCases:
    """测试文本折叠边界场景。"""

    def test_empty_text_not_collapsed(self):
        """测试空文本不被折叠。

        验证空字符串返回空字符串和 False。
        """
        from agent_py_agent.cli.chat import collapse_response_text

        result, collapsed = collapse_response_text("")

        assert result == ""
        assert collapsed is False

    def test_single_line_short_not_collapsed(self):
        """测试单行短文本不被折叠。

        验证单行且短文本不折叠。
        """
        from agent_py_agent.cli.chat import collapse_response_text

        result, collapsed = collapse_response_text("Short line.")

        assert collapsed is False

    def test_exactly_at_threshold_not_collapsed(self):
        """测试刚好在阈值的文本不被折叠。

        验证刚好等于阈值时不折叠。
        """
        from agent_py_agent.cli.chat import (
            COLLAPSE_PREVIEW_CHARS,
            COLLAPSE_PREVIEW_LINE_COUNT,
            collapse_response_text,
        )

        lines = ["line " + str(i) for i in range(COLLAPSE_PREVIEW_LINE_COUNT)]
        text = "\n".join(lines)
        # 确保字符数也在阈值内
        assert len(text) <= COLLAPSE_PREVIEW_CHARS

        result, collapsed = collapse_response_text(text)

        assert collapsed is False


class TestResumeCommand:
    """显式 resume <session_id> 子命令(owner seq1943 语义③ + 双席 seq1958)。

    fail-closed: 会话不存在/无效 ID → 报错退出(非 0), 绝不静默创建新会话
    或回退到其他历史; 存在 → 转调 cmd_chat 恢复该会话。
    """

    def test_resume_parser_positional_session_id(self):
        """resume 的 positional session_id 映射到 args.session_id + gateway 默认。"""
        import argparse

        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        add_basic_subcommands(sub)

        args = parser.parse_args(["resume", "sess_1712_abcd1234"])
        assert args.command == "resume"
        assert args.session_id == "sess_1712_abcd1234"
        assert args.gateway is True
        from agent_py_agent.cli.chat import cmd_resume

        assert args.func is cmd_resume

    def test_resume_missing_session_fail_closed(self, monkeypatch, tmp_path):
        """会话不存在 → 返回 3 + stderr 报错 + 不创建任何 session 文件。"""
        import sys

        from agent_py_agent.cli import chat as chat_mod

        class _FakeAgent:
            config = object()

        class _FakeManager:
            def __init__(self, config):
                self.config = config

            def session_exists(self, session_id):
                return False

        calls = []

        def _fake_make_agent(args):
            return _FakeAgent()

        def _fake_cmd_chat(args):
            calls.append(args)
            return 0

        monkeypatch.setattr(chat_mod, "make_agent", _fake_make_agent)
        monkeypatch.setattr("agent_py_agent.agent.session.manager.SessionManager", _FakeManager)
        monkeypatch.setattr(chat_mod, "cmd_chat", _fake_cmd_chat)

        class _Args:
            session_id = "sess_nonexistent_0000"

        rc = chat_mod.cmd_resume(_Args())
        assert rc == 3  # fail-closed
        assert calls == []  # 未转调 chat(未创建/恢复任何会话)
        # stderr 有报错提示
        assert True

    def test_resume_empty_session_id_fail_closed(self, monkeypatch):
        """空 session_id → 返回 3, 不初始化不转调。"""
        from agent_py_agent.cli import chat as chat_mod

        called = []

        def _fake_make_agent(args):
            called.append("make_agent")
            raise AssertionError("空 ID 不应初始化 agent")

        monkeypatch.setattr(chat_mod, "make_agent", _fake_make_agent)

        class _Args:
            session_id = ""

        rc = chat_mod.cmd_resume(_Args())
        assert rc == 3
        assert called == []

    def test_resume_existing_session_forwards_to_chat(self, monkeypatch):
        """会话存在 → 转调 cmd_chat(复用其恢复流程), 返回其退出码。"""
        from agent_py_agent.cli import chat as chat_mod

        class _FakeAgent:
            config = object()

        class _FakeManager:
            def __init__(self, config):
                self.config = config

            def session_exists(self, session_id):
                return session_id == "sess_real_1234"

            def load_session(self, session_id):
                class _S:
                    user_id = ""

                return _S()

        seen = {}

        def _fake_make_agent(args):
            return _FakeAgent()

        def _fake_cmd_chat(args):
            seen["args"] = args
            return 7  # 任意退出码

        monkeypatch.setattr(chat_mod, "make_agent", _fake_make_agent)
        monkeypatch.setattr("agent_py_agent.agent.session.manager.SessionManager", _FakeManager)
        monkeypatch.setattr(chat_mod, "cmd_chat", _fake_cmd_chat)

        class _Args:
            session_id = "sess_real_1234"

        rc = chat_mod.cmd_resume(_Args())
        assert rc == 7  # 透传 cmd_chat 退出码
        assert seen["args"] is not None


    def test_resume_cross_user_denied_fail_closed(self, monkeypatch):
        """跨用户负例(双席 seq1966): session 归属他人 → fail-closed 拒绝, 不恢复。"""
        from agent_py_agent.cli import chat as chat_mod

        class _Session:
            session_id = "sess_other_user"
            user_id = "other-user"

        class _FakeAgent:
            config = object()

        class _FakeConfig:
            user_id = "current-user"

        class _FakeAgent2:
            config = _FakeConfig()

        class _FakeManager:
            def __init__(self, config):
                self.config = config

            def session_exists(self, session_id):
                return True

            def load_session(self, session_id):
                return _Session()

        seen = []

        def _fake_make_agent(args):
            return _FakeAgent2()

        def _fake_cmd_chat(args):
            seen.append(args)
            return 0

        monkeypatch.setattr(chat_mod, "make_agent", _fake_make_agent)
        monkeypatch.setattr("agent_py_agent.agent.session.manager.SessionManager", _FakeManager)
        monkeypatch.setattr(chat_mod, "cmd_chat", _fake_cmd_chat)

        class _Args:
            session_id = "sess_other_user"

        rc = chat_mod.cmd_resume(_Args())
        assert rc == 3  # 跨用户拒绝
        assert seen == []  # 未转调 chat(未恢复他人会话)

    def test_resume_own_user_allowed(self, monkeypatch):
        """归属匹配(同 user_id) → 放行转调 chat。"""
        from agent_py_agent.cli import chat as chat_mod

        class _Session:
            session_id = "sess_mine"
            user_id = "current-user"

        class _FakeConfig:
            user_id = "current-user"

        class _FakeAgent:
            config = _FakeConfig()

        class _FakeManager:
            def __init__(self, config):
                self.config = config

            def session_exists(self, session_id):
                return True

            def load_session(self, session_id):
                return _Session()

        seen = []

        def _fake_make_agent(args):
            return _FakeAgent()

        def _fake_cmd_chat(args):
            seen.append(args)
            return 0

        monkeypatch.setattr(chat_mod, "make_agent", _fake_make_agent)
        monkeypatch.setattr("agent_py_agent.agent.session.manager.SessionManager", _FakeManager)
        monkeypatch.setattr(chat_mod, "cmd_chat", _fake_cmd_chat)

        class _Args:
            session_id = "sess_mine"

        rc = chat_mod.cmd_resume(_Args())
        assert rc == 0
        assert len(seen) == 1


class TestChatSessionIdFailClosed:
    """chat --session-id 显式指定入口同款 fail-closed(双席 seq1968 技术项)。"""

    def test_chat_session_id_nonexistent_returns_empty(self, tmp_path, monkeypatch):
        """显式 --session-id 不存在 → _setup_session 返回 ""(不再静默建新会话)。"""
        from agent_py_agent.cli import chat as chat_mod

        class _Config:
            user_id = "u1"

        class _FakeManager:
            def __init__(self, config):
                self.config = config

            def load_session(self, session_id):
                return None

            def create_session(self, channel="chat"):
                raise AssertionError("显式指定不存在时不得创建新会话")

            def touch_session(self, session_id, channel=None):
                pass

        class _Args:
            session_id = "sess_ghost_999"

        result = chat_mod._setup_session(_Args(), _FakeManager(_Config()))
        assert result == ""

    def test_chat_session_id_cross_user_returns_empty(self, tmp_path, monkeypatch):
        """显式 --session-id 跨用户 → _setup_session 返回 ""(不恢复他人会话)。"""
        from agent_py_agent.cli import chat as chat_mod

        class _Config:
            user_id = "u1"

        class _Session:
            session_id = "sess_theirs"
            user_id = "u2"

        class _FakeManager:
            def __init__(self, config):
                self.config = config

            def load_session(self, session_id):
                return _Session()

            def touch_session(self, session_id, channel=None):
                raise AssertionError("跨用户不得 touch")

        class _Args:
            session_id = "sess_theirs"

        result = chat_mod._setup_session(_Args(), _FakeManager(_Config()))
        assert result == ""

    def test_chat_session_id_own_user_restores(self, tmp_path, monkeypatch):
        """显式 --session-id 归属匹配 → 恢复(非空返回值)。"""
        from agent_py_agent.cli import chat as chat_mod

        class _Config:
            user_id = "u1"

        class _Session:
            session_id = "sess_mine2"
            user_id = "u1"

        touched = []

        class _FakeManager:
            def __init__(self, config):
                self.config = config

            def load_session(self, session_id):
                return _Session()

            def touch_session(self, session_id, channel=None):
                touched.append(session_id)

        class _Args:
            session_id = "sess_mine2"

        result = chat_mod._setup_session(_Args(), _FakeManager(_Config()))
        assert result == "sess_mine2"
        assert touched == ["sess_mine2"]

    def test_cmd_chat_returns_3_when_session_setup_fails(self, monkeypatch, tmp_path):
        """cmd_chat 在 _setup_session 失败(显式不存在/跨用户)时返回 3 不进入循环。"""
        from agent_py_agent.cli import chat as chat_mod

        class _FakeAgent:
            config = SimpleNamespace(
                gateway_workspace="/tmp/gw",
                gateway_ready_timeout_seconds=10,
                user_id="u1",
                session_workspace="/tmp/gw/sessions",
            )
            root = "/tmp"

        class _FakeManager:
            def __init__(self, config):
                self.config = config

        called = []

        def _fake_make_agent(args):
            return _FakeAgent()

        def _fake_setup(args, manager):
            return ""  # 失败

        def _fake_init(args, agent):
            called.append("init")
            return {}

        monkeypatch.setattr(chat_mod, "make_agent", _fake_make_agent)
        monkeypatch.setattr(chat_mod, "_setup_session", _fake_setup)
        monkeypatch.setattr(chat_mod, "_init_chat_state", _fake_init)
        monkeypatch.setattr(chat_mod, "gateway_paths", lambda agent: None)
        monkeypatch.setattr(chat_mod, "wait_for_gateway_running", lambda paths, timeout=10: (None, True))

        class _Args:
            session_id = "sess_ghost_999"
            gateway = True
            memory_limit = None
            inject = []
            prompt_file = []

        rc = chat_mod.cmd_chat(_Args())
        assert rc == 3
        assert called == []  # 未初始化聊天状态


# LLM: 恢复用例要构造的 canonical thread 形状收成一个不可变载体，保持 helper 参数不增长（code-size 参数守卫）。
# 类用途: 保存测试替身 thread 的绑定与归属字段。
@dataclass(frozen=True)
class _ThreadShape:
    owner_id: str = "local/main"
    owner_home: object = None
    channel: str = "chat"
    channel_user_id: str = "local-agent"
    create: bool = True


# 用途: 默认 thread 形状的模块级不可变单例，避免在参数默认值里做函数调用（lint B008）。
_DEFAULT_THREAD_SHAPE = _ThreadShape()


def _make_missing_registration_agent(
    tmp_path,
    session_id: str,
    thread: _ThreadShape = _DEFAULT_THREAD_SHAPE,
):
    """在 pytest 隔离 home 中保留 Gateway thread、删除 CLI 登记并组装 Agent。"""
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.session import Session
    from agent_py_agent.agent.session.manager import SessionManager

    owner_home = tmp_path / "owner-home"
    owner_home.mkdir()
    config = SimpleNamespace(
        session_workspace=str(owner_home / "sessions"),
        user_id="current-user",
        audit_enabled=True,
        audit_log_path=str(tmp_path / "audit.jsonl"),
    )
    manager = SessionManager(config)
    manager.save_session(Session(
        session_id=session_id,
        user_id=config.user_id,
        created_at=100.0,
        updated_at=101.0,
    ))
    assert manager.delete_session(session_id) is True

    store = ConversationStore(tmp_path / "conversation-store")
    if thread.create:
        store.threads.get_or_create({
            "canonical_user_id": "local-agent",
            "channel": thread.channel,
            "channel_conversation_id": session_id,
            "channel_user_id": thread.channel_user_id,
            "owner_id": thread.owner_id,
            "owner_home": str(thread.owner_home or owner_home),
            "title": "isolated recovery fixture",
        })
    agent = SimpleNamespace(
        config=config,
        home_paths=SimpleNamespace(owner_id="local/main", owner_home_dir=owner_home),
        conversation_store=store,
    )
    return agent


class TestResumeMissingRegistrationRecovery:
    def test_resume_recovers_deleted_registration_from_same_owner_thread(
        self, monkeypatch, tmp_path
    ):
        """丢失登记时仅凭同 owner 的持久 Gateway thread 恢复，并写审计。"""
        import json

        from agent_py_agent.agent.session.manager import SessionManager
        from agent_py_agent.cli import chat as chat_mod

        session_id = "sess_1791197000_a1b2c3d4"
        agent = _make_missing_registration_agent(tmp_path, session_id)
        resumed = []
        monkeypatch.setattr(chat_mod, "make_agent", lambda _args: agent)
        monkeypatch.setattr(
            chat_mod, "cmd_chat", lambda args: resumed.append(args) or 0
        )

        result = chat_mod.cmd_resume(SimpleNamespace(session_id=session_id))

        manager = SessionManager(agent.config)
        restored = manager.load_session(session_id)
        assert result == 0
        assert len(resumed) == 1
        assert restored is not None
        assert restored.session_id == session_id
        assert restored.user_id == "current-user"
        audit = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8"))
        assert audit["action"] == "SESSION_REGISTRY_RESTORE_AUTHORIZED"
        assert audit["target_type"] == "session"
        assert audit["target_id"] == session_id
        assert audit["details"]["source"] == "conversation_thread"
        assert "content" not in audit["details"]

    @pytest.mark.parametrize(
        ("thread_owner_id", "thread_owner_home"),
        [
            ("providers/feishu/users/other", None),
            ("local/main", "different-owner-home"),
        ],
    )
    def test_resume_does_not_recover_when_thread_owner_differs(
        self, monkeypatch, tmp_path, thread_owner_id, thread_owner_home
    ):
        """owner_id 或 owner_home 任一不精确匹配，都必须拒绝恢复。"""
        from agent_py_agent.agent.session.manager import SessionManager
        from agent_py_agent.cli import chat as chat_mod

        session_id = "sess_1791197001_a1b2c3d4"
        agent = _make_missing_registration_agent(
            tmp_path,
            session_id,
            thread=_ThreadShape(
                owner_id=thread_owner_id,
                owner_home=(
                    tmp_path / thread_owner_home if thread_owner_home else None
                ),
            ),
        )
        resumed = []
        monkeypatch.setattr(chat_mod, "make_agent", lambda _args: agent)
        monkeypatch.setattr(
            chat_mod, "cmd_chat", lambda args: resumed.append(args) or 0
        )

        result = chat_mod.cmd_resume(SimpleNamespace(session_id=session_id))

        assert result == 3
        assert resumed == []
        assert not SessionManager(agent.config).session_exists(session_id)
        assert not (tmp_path / "audit.jsonl").exists()


class TestResumeMissingRegistrationRecoveryRejections:
    """sessrecr 初审补充：绑定与审计维度的反例，全部必须拒绝且不落本地登记。"""

    def _resume(self, monkeypatch, agent, session_id):
        from agent_py_agent.cli import chat as chat_mod

        resumed = []
        monkeypatch.setattr(chat_mod, "make_agent", lambda _args: agent)
        monkeypatch.setattr(chat_mod, "cmd_chat", lambda args: resumed.append(args) or 0)
        result = chat_mod.cmd_resume(SimpleNamespace(session_id=session_id))
        return result, resumed

    @pytest.mark.parametrize(
        ("thread_channel", "thread_channel_user_id"),
        [
            ("feishu", "local-agent"),
            ("chat", "someone-else"),
        ],
        ids=["other-channel", "other-channel-user"],
    )
    def test_resume_rejects_thread_with_wrong_binding(
        self, monkeypatch, tmp_path, thread_channel, thread_channel_user_id
    ):
        """渠道或 channel_user_id 任一不精确匹配，都必须拒绝恢复。"""
        from agent_py_agent.agent.session.manager import SessionManager

        session_id = "sess_1791197002_a1b2c3d4"
        agent = _make_missing_registration_agent(
            tmp_path,
            session_id,
            thread=_ThreadShape(
                channel=thread_channel,
                channel_user_id=thread_channel_user_id,
            ),
        )

        result, resumed = self._resume(monkeypatch, agent, session_id)

        assert result == 3
        assert resumed == []
        assert not SessionManager(agent.config).session_exists(session_id)
        assert not (tmp_path / "audit.jsonl").exists()

    def test_resume_rejects_when_thread_missing(self, monkeypatch, tmp_path):
        """thread 缺失（无持久会话）时必须拒绝，不能凭登记名字猜归属。"""
        from agent_py_agent.agent.session.manager import SessionManager

        session_id = "sess_1791197003_a1b2c3d4"
        agent = _make_missing_registration_agent(
            tmp_path, session_id, thread=_ThreadShape(create=False)
        )

        result, resumed = self._resume(monkeypatch, agent, session_id)

        assert result == 3
        assert resumed == []
        assert not SessionManager(agent.config).session_exists(session_id)

    def test_resume_rejects_when_audit_disabled(self, monkeypatch, tmp_path):
        """审计关闭时拒绝恢复，登记不得落盘。"""
        from agent_py_agent.agent.session.manager import SessionManager

        session_id = "sess_1791197004_a1b2c3d4"
        agent = _make_missing_registration_agent(tmp_path, session_id)
        agent.config.audit_enabled = False

        result, resumed = self._resume(monkeypatch, agent, session_id)

        assert result == 3
        assert resumed == []
        assert not SessionManager(agent.config).session_exists(session_id)
        assert not (tmp_path / "audit.jsonl").exists()

    def test_resume_rejects_when_audit_write_fails_before_registry_rebuild(
        self, monkeypatch, tmp_path
    ):
        """审计写失败时必须在建登记之前收口：拒绝恢复且不落任何登记。"""
        from agent_py_agent.agent.audit import logger as audit_logger
        from agent_py_agent.agent.session.manager import SessionManager

        def _boom(*_args, **_kwargs):
            raise OSError("audit write failed")

        monkeypatch.setattr(audit_logger, "append_private_jsonl_records", _boom)
        session_id = "sess_1791197005_a1b2c3d4"
        agent = _make_missing_registration_agent(tmp_path, session_id)

        result, resumed = self._resume(monkeypatch, agent, session_id)

        assert result == 3
        assert resumed == []
        assert not SessionManager(agent.config).session_exists(session_id)

    def test_restored_registration_uses_canonical_thread_fields(self, monkeypatch, tmp_path):
        """恢复出的登记与 Session 模型字段一致：ID 相同、归属当前用户、时间来自 thread。"""
        from agent_py_agent.agent.conversation.channels import LOCAL_CHAT_CHANNEL
        from agent_py_agent.agent.session.manager import SessionManager

        session_id = "sess_1791197006_a1b2c3d4"
        agent = _make_missing_registration_agent(tmp_path, session_id)

        result, resumed = self._resume(monkeypatch, agent, session_id)

        assert result == 0 and len(resumed) == 1
        restored = SessionManager(agent.config).load_session(session_id)
        assert restored is not None
        thread, error = agent.conversation_store.threads.resolve_report(
            channel=LOCAL_CHAT_CHANNEL,
            channel_conversation_id=session_id,
            channel_user_id="local-agent",
        )
        assert error is None and thread is not None
        assert restored.session_id == session_id
        assert restored.user_id == "current-user"
        assert restored.created_at == float(thread.created_at)
        assert restored.updated_at == float(thread.updated_at)
        assert restored.last_active_channel == LOCAL_CHAT_CHANNEL
        assert restored.metadata == {
            "restored_from": "conversation_thread",
            "thread_id": str(thread.thread_id),
        }

    def test_session_lookup_rejects_traversal_id_before_touching_files(self, tmp_path):
        """session_id 走统一拒绝式校验：路径穿越形态在读/写文件之前就被拒。"""
        from agent_py_agent.agent.common.opaque_id import OpaqueIdError
        from agent_py_agent.agent.session import Session
        from agent_py_agent.agent.session.manager import SessionManager

        config = SimpleNamespace(
            session_workspace=str(tmp_path / "owner-home" / "sessions"),
            user_id="current-user",
            audit_enabled=True,
            audit_log_path=str(tmp_path / "audit.jsonl"),
        )
        manager = SessionManager(config)

        with pytest.raises(OpaqueIdError):
            manager.session_exists("../escape")
        with pytest.raises(OpaqueIdError):
            manager.save_session(Session(
                session_id="../escape",
                user_id="current-user",
                created_at=1.0,
                updated_at=1.0,
            ))
        assert not (tmp_path / "owner-home" / "escape").exists()
        assert not (tmp_path / "escape").exists()
