from __future__ import annotations

"""LLM: tests for subcommands_basic module.

给人看的解释：
测试基础子命令（run、chat、status）的注册和参数配置。
"""

import argparse
import pytest
from unittest.mock import MagicMock, patch


class TestSubcommandsBasicImports:
    """测试模块导入。"""

    def test_module_imports_success(self) -> None:
        """测试模块能成功导入。"""
        from agent_py_agent.cli.subcommands_basic import (
            add_basic_subcommands,
            add_memory_subcommands,
            add_local_store_subcommands,
        )
        assert callable(add_basic_subcommands)
        assert callable(add_memory_subcommands)
        assert callable(add_local_store_subcommands)

    def test_capability_config_arg_imported(self) -> None:
        """测试 _add_capability_config_arg 已导入。"""
        from agent_py_agent.cli.subcommands_basic import _add_capability_config_arg
        assert callable(_add_capability_config_arg)


class TestAddBasicSubcommands:
    """测试 add_basic_subcommands 函数。"""

    @pytest.fixture
    def parser_with_subcommands(self) -> argparse.ArgumentParser:
        """创建带有子命令的解析器。"""
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands
        add_basic_subcommands(sub)
        return parser

    def test_status_parser_exists(self, parser_with_subcommands: argparse.ArgumentParser) -> None:
        """测试 status 解析器存在。"""
        assert "status" in parser_with_subcommands._subparsers._actions[1].choices

    def test_run_parser_exists(self, parser_with_subcommands: argparse.ArgumentParser) -> None:
        """测试 run 解析器存在。"""
        assert "run" in parser_with_subcommands._subparsers._actions[1].choices

    def test_remember_parser_exists(self, parser_with_subcommands: argparse.ArgumentParser) -> None:
        """测试 remember 解析器存在。"""
        assert "remember" in parser_with_subcommands._subparsers._actions[1].choices

    def test_chat_parser_exists(self, parser_with_subcommands: argparse.ArgumentParser) -> None:
        """测试 chat 解析器存在。"""
        assert "chat" in parser_with_subcommands._subparsers._actions[1].choices

    def test_timeline_parser_exists(self, parser_with_subcommands: argparse.ArgumentParser) -> None:
        """测试 timeline 解析器存在。"""
        assert "timeline" in parser_with_subcommands._subparsers._actions[1].choices

    def test_memory_list_parser_exists(self, parser_with_subcommands: argparse.ArgumentParser) -> None:
        """测试 memory-list 解析器存在。"""
        assert "memory-list" in parser_with_subcommands._subparsers._actions[1].choices

    def test_memory_search_parser_exists(self, parser_with_subcommands: argparse.ArgumentParser) -> None:
        """测试 memory-search 解析器存在。"""
        assert "memory-search" in parser_with_subcommands._subparsers._actions[1].choices


class TestStatusParser:
    """测试 status 子命令解析器。"""

    @pytest.fixture
    def parser_with_basic(self) -> argparse.ArgumentParser:
        """创建带有 basic subcommands 的解析器。"""
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands
        add_basic_subcommands(sub)
        return parser

    def test_status_default_limit(self, parser_with_basic: argparse.ArgumentParser) -> None:
        """测试 status 默认 limit 值。"""
        args = parser_with_basic.parse_args(["status"])
        assert args.limit == 5

    def test_status_custom_limit(self, parser_with_basic: argparse.ArgumentParser) -> None:
        """测试 status 自定义 limit 值。"""
        args = parser_with_basic.parse_args(["status", "--limit", "10"])
        assert args.limit == 10

    def test_status_recent_flag(self, parser_with_basic: argparse.ArgumentParser) -> None:
        """测试 status 的 --recent 标志。"""
        args = parser_with_basic.parse_args(["status", "--recent"])
        assert args.recent is True

    def test_status_json_flag(self, parser_with_basic: argparse.ArgumentParser) -> None:
        """测试 status 的 --json 标志。"""
        args = parser_with_basic.parse_args(["status", "--json"])
        assert args.json is True


class TestRunParser:
    """测试 run 子命令解析器。"""

    @pytest.fixture
    def parser_with_basic(self) -> argparse.ArgumentParser:
        """创建带有 basic subcommands 的解析器。"""
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands
        add_basic_subcommands(sub)
        return parser

    def test_run_prompt_positional(self, parser_with_basic: argparse.ArgumentParser) -> None:
        """测试 run 的 prompt 位置参数。"""
        args = parser_with_basic.parse_args(["run", "测试任务"])
        assert args.prompt == "测试任务"

    def test_run_inject_flag(self, parser_with_basic: argparse.ArgumentParser) -> None:
        """测试 run 的 --inject 标志。"""
        args = parser_with_basic.parse_args(["run", "test", "--inject", "extra"])
        assert args.inject == ["extra"]

    def test_run_multiple_inject_flags(self, parser_with_basic: argparse.ArgumentParser) -> None:
        """测试 run 的多个 --inject 标志。"""
        args = parser_with_basic.parse_args(["run", "test", "--inject", "a", "--inject", "b"])
        assert args.inject == ["a", "b"]

    def test_run_save_flag(self, parser_with_basic: argparse.ArgumentParser) -> None:
        """测试 run 的 --save 标志。"""
        args = parser_with_basic.parse_args(["run", "test", "--save"])
        assert args.save is True

    def test_run_no_save_flag(self, parser_with_basic: argparse.ArgumentParser) -> None:
        """测试 run 的 --no-save 标志。"""
        args = parser_with_basic.parse_args(["run", "test", "--no-save"])
        assert args.save is False

    def test_run_show_prompt_flag(self, parser_with_basic: argparse.ArgumentParser) -> None:
        """测试 run 的 --show-prompt 标志。"""
        args = parser_with_basic.parse_args(["run", "test", "--show-prompt"])
        assert args.show_prompt is True


class TestRememberParser:
    """测试 remember 子命令解析器。"""

    @pytest.fixture
    def parser_with_basic(self) -> argparse.ArgumentParser:
        """创建带有 basic subcommands 的解析器。"""
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands
        add_basic_subcommands(sub)
        return parser

    def test_remember_content_positional(self, parser_with_basic: argparse.ArgumentParser) -> None:
        """测试 remember 的 content 位置参数。"""
        args = parser_with_basic.parse_args(["remember", "记忆内容"])
        assert args.content == "记忆内容"

    def test_remember_default_kind(self, parser_with_basic: argparse.ArgumentParser) -> None:
        """测试 remember 的默认 kind 值。"""
        args = parser_with_basic.parse_args(["remember", "内容"])
        assert args.kind == "note"

    def test_remember_custom_kind(self, parser_with_basic: argparse.ArgumentParser) -> None:
        """测试 remember 的自定义 kind 值。"""
        args = parser_with_basic.parse_args(["remember", "内容", "--kind", "fact"])
        assert args.kind == "fact"


class TestMemorySubcommands:
    """测试 memory 相关子命令解析器。"""

    @pytest.fixture
    def parser_with_memory(self) -> argparse.ArgumentParser:
        """创建带有 memory subcommands 的解析器。"""
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        from agent_py_agent.cli.subcommands_basic import add_memory_subcommands
        add_memory_subcommands(sub)
        return parser

    def test_memory_route_parser_exists(self, parser_with_memory: argparse.ArgumentParser) -> None:
        """测试 memory-route 解析器存在。"""
        assert "memory-route" in parser_with_memory._subparsers._actions[1].choices

    def test_memory_doctor_parser_exists(self, parser_with_memory: argparse.ArgumentParser) -> None:
        """测试 memory-doctor 解析器存在。"""
        assert "memory-doctor" in parser_with_memory._subparsers._actions[1].choices

    def test_memory_archive_list_parser_exists(self, parser_with_memory: argparse.ArgumentParser) -> None:
        """测试 memory-archive-list 解析器存在。"""
        assert "memory-archive-list" in parser_with_memory._subparsers._actions[1].choices

    def test_memory_resume_parser_exists(self, parser_with_memory: argparse.ArgumentParser) -> None:
        """测试 memory-resume 解析器存在。"""
        assert "memory-resume" in parser_with_memory._subparsers._actions[1].choices

    def test_memory_route_mode_choices(self, parser_with_memory: argparse.ArgumentParser) -> None:
        """测试 memory-route 的 mode 参数选项。"""
        args = parser_with_memory.parse_args(["memory-route", "query", "--mode", "soft"])
        assert args.mode == "soft"

    def test_memory_archive_list_layer_choices(self, parser_with_memory: argparse.ArgumentParser) -> None:
        """测试 memory-archive-list 的 layer 参数选项。"""
        args = parser_with_memory.parse_args(["memory-archive-list", "--layer", "raw"])
        assert args.layer == "raw"


class TestLocalStoreSubcommands:
    """测试 local-store 相关子命令解析器。"""

    @pytest.fixture
    def parser_with_local(self) -> argparse.ArgumentParser:
        """创建带有 local-store subcommands 的解析器。"""
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        from agent_py_agent.cli.subcommands_basic import add_local_store_subcommands
        add_local_store_subcommands(sub)
        return parser

    def test_local_store_status_parser_exists(self, parser_with_local: argparse.ArgumentParser) -> None:
        """测试 local-store-status 解析器存在。"""
        assert "local-store-status" in parser_with_local._subparsers._actions[1].choices

    def test_local_search_parser_exists(self, parser_with_local: argparse.ArgumentParser) -> None:
        """测试 local-search 解析器存在。"""
        assert "local-search" in parser_with_local._subparsers._actions[1].choices

    def test_local_doctor_parser_exists(self, parser_with_local: argparse.ArgumentParser) -> None:
        """测试 local-doctor 解析器存在。"""
        assert "local-doctor" in parser_with_local._subparsers._actions[1].choices

    def test_local_rebuild_parser_exists(self, parser_with_local: argparse.ArgumentParser) -> None:
        """测试 local-rebuild 解析器存在。"""
        assert "local-rebuild" in parser_with_local._subparsers._actions[1].choices

    def test_local_search_query_positional(self, parser_with_local: argparse.ArgumentParser) -> None:
        """测试 local-search 的 query 位置参数。"""
        args = parser_with_local.parse_args(["local-search", "搜索词"])
        assert args.query == "搜索词"

    def test_local_rebuild_source_choices(self, parser_with_local: argparse.ArgumentParser) -> None:
        """测试 local-rebuild 的 source 参数选项。"""
        args = parser_with_local.parse_args(["local-rebuild", "--source", "memory"])
        assert args.source == ["memory"]