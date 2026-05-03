"""memory_archive_commands CLI 命令测试。

测试 memory archive list/search/resume 命令。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


class TestCmdMemoryArchiveList:
    """测试 cmd_memory_archive_list 命令。"""

    def test_memory_archive_list_basic(self, tmp_path: Path):
        """正常列出归档记录。"""
        from agent_py_agent.cli.memory_archive_commands import cmd_memory_archive_list

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.layer = "all"
        args.date = None
        args.level = None
        args.limit = 20
        args.json = False

        mock_agent = MagicMock()
        mock_agent.root = tmp_path

        with patch("agent_py_agent.cli.memory_archive_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.memory_archive_commands.collect_archive_records", return_value=[]):
            result = cmd_memory_archive_list(args)
            assert result == 0

    def test_memory_archive_list_with_layer_filter(self, tmp_path: Path):
        """按 layer 过滤归档记录。"""
        from agent_py_agent.cli.memory_archive_commands import cmd_memory_archive_list

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.layer = "raw"
        args.date = None
        args.level = None
        args.limit = 20
        args.json = False

        mock_agent = MagicMock()
        mock_agent.root = tmp_path

        with patch("agent_py_agent.cli.memory_archive_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.memory_archive_commands.collect_archive_records", return_value=[]):
            result = cmd_memory_archive_list(args)
            assert result == 0

    def test_memory_archive_list_with_date_filter(self, tmp_path: Path):
        """按日期过滤归档记录。"""
        from agent_py_agent.cli.memory_archive_commands import cmd_memory_archive_list

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.layer = "all"
        args.date = "2024-01-01"
        args.level = None
        args.limit = 20
        args.json = False

        mock_agent = MagicMock()
        mock_agent.root = tmp_path

        with patch("agent_py_agent.cli.memory_archive_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.memory_archive_commands.collect_archive_records", return_value=[]):
            result = cmd_memory_archive_list(args)
            assert result == 0

    def test_memory_archive_list_json_output(self, tmp_path: Path):
        """使用 JSON 输出格式。"""
        from agent_py_agent.cli.memory_archive_commands import cmd_memory_archive_list

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.layer = "all"
        args.date = None
        args.level = None
        args.limit = 20
        args.json = True

        mock_agent = MagicMock()
        mock_agent.root = tmp_path

        with patch("agent_py_agent.cli.memory_archive_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.memory_archive_commands.collect_archive_records", return_value=[]):
            result = cmd_memory_archive_list(args)
            assert result == 0


class TestCmdMemoryArchiveSearch:
    """测试 cmd_memory_archive_search 命令。"""

    def test_memory_archive_search_basic(self, tmp_path: Path):
        """正常搜索归档记录。"""
        from agent_py_agent.cli.memory_archive_commands import cmd_memory_archive_search

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.query = "测试"
        args.layer = "all"
        args.date = None
        args.since = None
        args.until = None
        args.session_id = None
        args.request_id = None
        args.run_id = None
        args.task_id = None
        args.speaker = None
        args.target = None
        args.action = None
        args.status = None
        args.tool_name = None
        args.source = None
        args.level = None
        args.limit = 20
        args.json = False

        mock_agent = MagicMock()
        mock_agent.root = tmp_path

        with patch("agent_py_agent.cli.memory_archive_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.memory_archive_commands.collect_archive_records", return_value=[]), \
             patch("agent_py_agent.cli.memory_archive_commands.archive_filters_from_args", return_value={}), \
             patch("agent_py_agent.cli.memory_archive_commands.filter_archive_records", return_value=[]):
            result = cmd_memory_archive_search(args)
            assert result == 0

    def test_memory_archive_search_with_filters(self, tmp_path: Path):
        """带字段过滤搜索归档。"""
        from agent_py_agent.cli.memory_archive_commands import cmd_memory_archive_search

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.query = ""
        args.layer = "hook"
        args.date = None
        args.since = "2024-01-01"
        args.until = "2024-12-31"
        args.session_id = "session_001"
        args.request_id = None
        args.run_id = None
        args.task_id = None
        args.speaker = "user"
        args.target = None
        args.action = None
        args.status = None
        args.tool_name = None
        args.source = None
        args.level = 3
        args.limit = 10
        args.json = False

        mock_agent = MagicMock()
        mock_agent.root = tmp_path

        with patch("agent_py_agent.cli.memory_archive_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.memory_archive_commands.collect_archive_records", return_value=[]), \
             patch("agent_py_agent.cli.memory_archive_commands.archive_filters_from_args", return_value={"speaker": "user"}), \
             patch("agent_py_agent.cli.memory_archive_commands.filter_archive_records", return_value=[]):
            result = cmd_memory_archive_search(args)
            assert result == 0


class TestCmdMemoryResume:
    """测试 cmd_memory_resume 命令。"""

    def test_memory_resume_basic(self, tmp_path: Path):
        """正常生成恢复线索。"""
        from agent_py_agent.cli.memory_archive_commands import cmd_memory_resume

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.query = ""
        args.layer = "all"
        args.date = None
        args.since = None
        args.until = None
        args.session_id = None
        args.request_id = None
        args.run_id = None
        args.task_id = None
        args.speaker = None
        args.target = None
        args.action = None
        args.status = None
        args.tool_name = None
        args.source = None
        args.level = None
        args.limit = 20
        args.context_only = False
        args.json = False

        mock_agent = MagicMock()
        mock_agent.root = tmp_path
        mock_agent.local_store.search.return_value = []
        mock_agent.local_store.list_recent.return_value = []

        with patch("agent_py_agent.cli.memory_archive_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.memory_archive_commands.collect_archive_records", return_value=[]), \
             patch("agent_py_agent.cli.memory_archive_commands.archive_filters_from_args", return_value={}), \
             patch("agent_py_agent.cli.memory_archive_commands.filter_archive_records", return_value=[]), \
             patch("agent_py_agent.cli.memory_archive_commands.resume_local_query", return_value=None), \
             patch("agent_py_agent.cli.memory_archive_commands.collect_resume_task_ids", return_value=[]), \
             patch("agent_py_agent.cli.memory_archive_commands.collect_task_payloads", return_value=[]), \
             patch("agent_py_agent.cli.memory_archive_commands.collect_gateway_payloads", return_value=[]), \
             patch("agent_py_agent.cli.memory_archive_commands.build_resume_guidance", return_value={"recommended_read_paths": [], "next_actions": [], "archive_match_count": 0, "local_match_count": 0, "task_fact_source_count": 0}), \
             patch("agent_py_agent.cli.memory_archive_commands.build_resume_brief", return_value={"context_block": "", "latest_user_intent": None, "latest_assistant_action": None, "related_ids": [], "likely_task_statuses": [], "authority_note": ""}):
            result = cmd_memory_resume(args)
            assert result == 0

    def test_memory_resume_context_only(self, tmp_path: Path):
        """仅输出恢复上下文块。"""
        from agent_py_agent.cli.memory_archive_commands import cmd_memory_resume

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.query = "测试"
        args.layer = "all"
        args.date = None
        args.since = None
        args.until = None
        args.session_id = None
        args.request_id = None
        args.run_id = None
        args.task_id = None
        args.speaker = None
        args.target = None
        args.action = None
        args.status = None
        args.tool_name = None
        args.source = None
        args.level = None
        args.limit = 20
        args.context_only = True
        args.json = False

        mock_agent = MagicMock()
        mock_agent.root = tmp_path
        mock_agent.local_store.search.return_value = []
        mock_agent.local_store.list_recent.return_value = []

        with patch("agent_py_agent.cli.memory_archive_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.memory_archive_commands.collect_archive_records", return_value=[]), \
             patch("agent_py_agent.cli.memory_archive_commands.archive_filters_from_args", return_value={}), \
             patch("agent_py_agent.cli.memory_archive_commands.filter_archive_records", return_value=[]), \
             patch("agent_py_agent.cli.memory_archive_commands.collect_resume_task_ids", return_value=[]), \
             patch("agent_py_agent.cli.memory_archive_commands.collect_task_payloads", return_value=[]), \
             patch("agent_py_agent.cli.memory_archive_commands.collect_gateway_payloads", return_value=[]), \
             patch("agent_py_agent.cli.memory_archive_commands.build_resume_guidance", return_value={"recommended_read_paths": [], "next_actions": []}), \
             patch("agent_py_agent.cli.memory_archive_commands.build_resume_brief", return_value={"context_block": "恢复上下文块", "latest_user_intent": None, "latest_assistant_action": None, "related_ids": [], "likely_task_statuses": [], "authority_note": ""}):
            result = cmd_memory_resume(args)
            assert result == 0


class TestPrintArchiveRecordLines:
    """测试 _print_archive_record_lines 辅助函数。"""

    def test_print_empty_records(self, capsys):
        """测试空记录列表的打印。"""
        from agent_py_agent.cli.memory_archive_commands import _print_archive_record_lines

        _print_archive_record_lines([])

        captured = capsys.readouterr()
        assert "- none" in captured.out

    def test_print_records_with_data(self, capsys):
        """测试有数据时的打印。"""
        from agent_py_agent.cli.memory_archive_commands import _print_archive_record_lines

        records = [
            {
                "layer": "raw",
                "id": "rec_001",
                "action": "message",
                "status": "ok",
                "run_id": "run_001",
                "file_path": "/test/events.jsonl",
                "line_no": 10,
                "content_preview": "测试内容"
            }
        ]

        _print_archive_record_lines(records)

        captured = capsys.readouterr()
        assert "raw" in captured.out
        assert "rec_001" in captured.out
