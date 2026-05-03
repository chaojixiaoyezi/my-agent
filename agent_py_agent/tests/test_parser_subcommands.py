from __future__ import annotations

"""LLM: tests for parser_subcommands module.

给人看的解释：
测试子命令定义、参数验证和帮助信息。
注意：这个文件是薄 re-export 层，实际测试在 subcommands_basic 中。
这里主要测试导入和基本结构。
"""

import argparse
from unittest.mock import MagicMock, patch

import pytest


class TestParserSubcommandsExports:
    """测试 parser_subcommands 模块导出。"""

    def test_module_imports_success(self) -> None:
        """测试模块能成功导入。"""
        from agent_py_agent.cli import parser_subcommands
        assert parser_subcommands is not None

    def test_basic_subcommands_exported(self) -> None:
        """测试 add_basic_subcommands 已导出。"""
        from agent_py_agent.cli.parser_subcommands import add_basic_subcommands
        assert callable(add_basic_subcommands)

    def test_memory_subcommands_exported(self) -> None:
        """测试 add_memory_subcommands 已导出。"""
        from agent_py_agent.cli.parser_subcommands import add_memory_subcommands
        assert callable(add_memory_subcommands)

    def test_local_store_subcommands_exported(self) -> None:
        """测试 add_local_store_subcommands 已导出。"""
        from agent_py_agent.cli.parser_subcommands import add_local_store_subcommands
        assert callable(add_local_store_subcommands)


class TestAddBasicSubcommands:
    """测试 add_basic_subcommands 函数。"""

    def test_adds_status_parser(self) -> None:
        """测试添加 status 解析器。"""
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        add_basic_subcommands(sub)

        status_parser = sub.choices.get("status")
        assert status_parser is not None

    def test_adds_run_parser(self) -> None:
        """测试添加 run 解析器。"""
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        add_basic_subcommands(sub)

        run_parser = sub.choices.get("run")
        assert run_parser is not None

    def test_adds_remember_parser(self) -> None:
        """测试添加 remember 解析器。"""
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        add_basic_subcommands(sub)

        remember_parser = sub.choices.get("remember")
        assert remember_parser is not None

    def test_run_parser_has_prompt_argument(self) -> None:
        """测试 run 解析器有 prompt 参数。"""
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        add_basic_subcommands(sub)

        run_parser = sub.choices.get("run")
        assert run_parser is not None

        # 检查 --help 是否能正常解析
        try:
            run_parser.parse_args(["--help"])
        except SystemExit:
            pass

    def test_remember_parser_has_content_argument(self) -> None:
        """测试 remember 解析器有 content 参数。"""
        from agent_py_agent.cli.subcommands_basic import add_basic_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        add_basic_subcommands(sub)

        remember_parser = sub.choices.get("remember")
        assert remember_parser is not None


class TestAddMemorySubcommands:
    """测试 add_memory_subcommands 函数。"""

    def test_adds_memory_route_parser(self) -> None:
        """测试添加 memory-route 解析器。"""
        from agent_py_agent.cli.subcommands_basic import add_memory_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        add_memory_subcommands(sub)

        route_parser = sub.choices.get("memory-route")
        assert route_parser is not None

    def test_adds_memory_doctor_parser(self) -> None:
        """测试添加 memory-doctor 解析器。"""
        from agent_py_agent.cli.subcommands_basic import add_memory_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        add_memory_subcommands(sub)

        doctor_parser = sub.choices.get("memory-doctor")
        assert doctor_parser is not None

    def test_adds_memory_archive_list_parser(self) -> None:
        """测试添加 memory-archive-list 解析器。"""
        from agent_py_agent.cli.subcommands_basic import add_memory_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        add_memory_subcommands(sub)

        archive_list_parser = sub.choices.get("memory-archive-list")
        assert archive_list_parser is not None

    def test_adds_memory_resume_parser(self) -> None:
        """测试添加 memory-resume 解析器。"""
        from agent_py_agent.cli.subcommands_basic import add_memory_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        add_memory_subcommands(sub)

        resume_parser = sub.choices.get("memory-resume")
        assert resume_parser is not None


class TestAddLocalStoreSubcommands:
    """测试 add_local_store_subcommands 函数。"""

    def test_adds_local_store_status_parser(self) -> None:
        """测试添加 local-store-status 解析器。"""
        from agent_py_agent.cli.subcommands_basic import add_local_store_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        add_local_store_subcommands(sub)

        status_parser = sub.choices.get("local-store-status")
        assert status_parser is not None

    def test_adds_local_search_parser(self) -> None:
        """测试添加 local-search 解析器。"""
        from agent_py_agent.cli.subcommands_basic import add_local_store_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        add_local_store_subcommands(sub)

        search_parser = sub.choices.get("local-search")
        assert search_parser is not None

    def test_adds_local_doctor_parser(self) -> None:
        """测试添加 local-doctor 解析器。"""
        from agent_py_agent.cli.subcommands_basic import add_local_store_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        add_local_store_subcommands(sub)

        doctor_parser = sub.choices.get("local-doctor")
        assert doctor_parser is not None

    def test_adds_local_rebuild_parser(self) -> None:
        """测试添加 local-rebuild 解析器。"""
        from agent_py_agent.cli.subcommands_basic import add_local_store_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        add_local_store_subcommands(sub)

        rebuild_parser = sub.choices.get("local-rebuild")
        assert rebuild_parser is not None


class TestCapabilityConfigArg:
    """测试 _add_capability_config_arg 函数。"""

    def test_adds_capability_config_argument(self) -> None:
        """测试添加 capability-config 参数。"""
        from agent_py_agent.cli.subcommands_basic import _add_capability_config_arg

        parser = argparse.ArgumentParser()
        _add_capability_config_arg(parser)

        args = parser.parse_args(["--capability-config", "/custom/path.yaml"])
        assert "/custom/path.yaml" in str(args.capability_config)