"""local_store_commands CLI 命令测试。

测试 local-store status/search/rebuild 命令。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestCmdLocalStoreStatus:
    """测试 cmd_local_store_status 命令。"""

    def test_local_store_status_basic(self, tmp_path: Path):
        """正常显示本地事实源状态。"""
        from agent_py_agent.cli.local_commands import cmd_local_store_status

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")

        mock_agent = MagicMock()
        mock_agent.local_store.stats.return_value = {
            "record_count": 100,
            "event_count": 50,
            "fts5_enabled": True,
            "db_path": str(tmp_path / "store.db")
        }

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent):
            result = cmd_local_store_status(args)
            assert result == 0


class TestCmdLocalSearch:
    """测试 cmd_local_search 命令。"""

    def test_local_search_basic(self, tmp_path: Path):
        """正常搜索本地事实源。"""
        from agent_py_agent.cli.local_commands import cmd_local_search

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.query = "测试"
        args.limit = 10
        args.source_type = None
        args.visibility = None
        args.preview_chars = 500

        mock_hit = MagicMock()
        mock_hit.__dict__ = {"id": "hit_001", "content": "测试内容", "source_type": "memory"}

        mock_agent = MagicMock()
        mock_agent.local_store.search.return_value = [mock_hit]

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent):
            result = cmd_local_search(args)
            assert result == 0

    def test_local_search_with_source_filter(self, tmp_path: Path):
        """带 source_type 过滤搜索。"""
        from agent_py_agent.cli.local_commands import cmd_local_search

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.query = "测试"
        args.limit = 10
        args.source_type = "gateway_request"
        args.visibility = None
        args.preview_chars = 500

        mock_agent = MagicMock()
        mock_agent.local_store.search.return_value = []

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent):
            result = cmd_local_search(args)
            assert result == 0
            mock_agent.local_store.search.assert_called_once()

    def test_local_search_truncate_preview(self, tmp_path: Path):
        """验证预览内容截断。"""
        from agent_py_agent.cli.local_commands import cmd_local_search

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.query = "测试"
        args.limit = 5
        args.source_type = None
        args.visibility = None
        args.preview_chars = 10  # 截断到10字符

        long_content = "这是一段很长的测试内容"
        mock_hit = MagicMock()
        mock_hit.__dict__ = {"id": "hit_001", "content": long_content, "source_type": "memory"}

        mock_agent = MagicMock()
        mock_agent.local_store.search.return_value = [mock_hit]

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent):
            result = cmd_local_search(args)
            assert result == 0


class TestCmdLocalDoctor:
    """测试 cmd_local_doctor 命令。"""

    def test_local_doctor_basic(self, tmp_path: Path):
        """正常执行本地诊断。"""
        from agent_py_agent.cli.local_commands import cmd_local_doctor

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.limit = 20
        args.repair = False
        args.json = False
        args.workspace_root = ""

        mock_agent = MagicMock()
        mock_agent.local_store.stats.return_value = {"record_count": 100}
        mock_agent.config.gateway_request_max_attempts = 3
        mock_agent.config.gateway_processing_timeout_seconds = 300

        mock_report = {
            "ok": True,
            "stats": {"record_count": 100},
            "source_counts": {"memory": 50, "gateway": 30},
            "workspace_scope": {"workspace_root": str(tmp_path), "mode": "implicit_cwd"},
            "checks": [],
            "suggestions": []
        }

        with patch("agent_py_agent.cli.local_repair_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.local_repair_commands.build_local_doctor_report", return_value=mock_report):
            result = cmd_local_doctor(args)
            assert result == 0

    def test_local_doctor_with_repair(self, tmp_path: Path):
        """带修复选项的诊断。"""
        from agent_py_agent.cli.local_commands import cmd_local_doctor

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.limit = 20
        args.repair = True
        args.json = False
        args.workspace_root = ""

        mock_agent = MagicMock()
        mock_agent.local_store.stats.return_value = {"record_count": 100}
        mock_agent.config.gateway_request_max_attempts = 3
        mock_agent.config.gateway_processing_timeout_seconds = 300

        mock_report = {
            "ok": True,
            "stats": {"record_count": 100},
            "source_counts": {"memory": 50},
            "workspace_scope": {"workspace_root": str(tmp_path), "mode": "implicit_cwd"},
            "checks": [],
            "suggestions": []
        }

        with patch("agent_py_agent.cli.local_repair_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.local_repair_commands.recover_gateway_processing_requests", return_value={}), \
             patch("agent_py_agent.cli.local_repair_commands.build_local_doctor_report", return_value=mock_report):
            result = cmd_local_doctor(args)
            assert result == 0

    def test_local_doctor_json_output(self, tmp_path: Path):
        """JSON 输出格式。"""
        from agent_py_agent.cli.local_commands import cmd_local_doctor

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.limit = 20
        args.repair = False
        args.json = True
        args.workspace_root = ""

        mock_agent = MagicMock()
        mock_agent.local_store.stats.return_value = {"record_count": 100}
        mock_agent.config.gateway_request_max_attempts = 3
        mock_agent.config.gateway_processing_timeout_seconds = 300

        mock_report = {
            "ok": True,
            "stats": {"record_count": 100},
            "source_counts": {},
            "workspace_scope": {"workspace_root": str(tmp_path), "mode": "implicit_cwd"},
            "checks": [],
            "suggestions": []
        }

        with patch("agent_py_agent.cli.local_repair_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.local_repair_commands.build_local_doctor_report", return_value=mock_report):
            result = cmd_local_doctor(args)
            assert result == 0


class TestCmdLocalRebuild:
    """测试 cmd_local_rebuild 命令。"""

    def test_local_rebuild_memory_source(self, tmp_path: Path):
        """重建 memory 来源。"""
        from agent_py_agent.cli.local_commands import cmd_local_rebuild

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.source = ["memory"]
        args.reset = False

        mock_agent = MagicMock()
        mock_agent.memory.index_all.return_value = 50

        with patch("agent_py_agent.cli.local_repair_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.local_repair_commands.rebuild_local_store", return_value={"memory": 50}):
            result = cmd_local_rebuild(args)
            assert result == 0

    def test_local_rebuild_all_sources(self, tmp_path: Path):
        """重建所有来源。"""
        from agent_py_agent.cli.local_commands import cmd_local_rebuild

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.source = ["all"]
        args.reset = True

        mock_agent = MagicMock()

        with patch("agent_py_agent.cli.local_repair_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.local_repair_commands.rebuild_local_store", return_value={"memory": 50, "gateway": 30, "subagent": 20}):
            result = cmd_local_rebuild(args)
            assert result == 0

    def test_local_rebuild_unknown_source(self, tmp_path: Path):
        """未知来源返回错误。"""
        from agent_py_agent.cli.local_commands import cmd_local_rebuild

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.source = ["unknown_source"]
        args.reset = False

        mock_agent = MagicMock()

        with patch("agent_py_agent.cli.local_repair_commands.make_agent", return_value=mock_agent):
            result = cmd_local_rebuild(args)
            assert result == 2


class TestCmdLocalIndexMemory:
    """测试 cmd_local_index_memory 命令。"""

    def test_local_index_memory_basic(self, tmp_path: Path):
        """正常索引记忆。"""
        from agent_py_agent.cli.local_commands import cmd_local_index_memory

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")

        mock_agent = MagicMock()
        mock_agent.memory.index_all.return_value = 100
        mock_agent.local_store.stats.return_value = {"record_count": 200}

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent):
            result = cmd_local_index_memory(args)
            assert result == 0
