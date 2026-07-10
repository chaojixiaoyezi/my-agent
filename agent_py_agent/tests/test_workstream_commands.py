"""workstream_commands CLI 命令测试。

测试 workstream 命令（暂无专门的 workstream_commands.py，测试 local_commands 中的本地状态命令）。

注意：经过检查，my-agent 项目中没有单独的 workstream_commands.py 文件。
workstream 功能可能通过其他命令实现，这里测试 local_commands 中相关的本地状态功能。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# 由于没有独立的 workstream_commands.py，测试 local_commands 中的相关功能
class TestLocalStoreCommandsForWorkstream:
    """测试本地状态相关的命令（workstream 依赖这些基础命令）。"""

    def test_local_store_status_for_workstream(self, tmp_path: Path):
        """工作流状态检查依赖本地存储状态。"""
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

    def test_timeline_for_workstream(self, tmp_path: Path):
        """工作流时间线用于追踪任务进度。"""
        from agent_py_agent.cli.local_commands import cmd_timeline

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.limit = 20
        args.source_type = None
        args.event_type = None
        args.details = False
        args.json = False

        mock_agent = MagicMock()
        mock_agent.local_store.timeline.return_value = []

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent):
            result = cmd_timeline(args)
            assert result == 0


class TestStatusCommandsForWorkstream:
    """测试状态命令（workstream 需要这些来显示任务状态）。"""

    def test_status_shows_active_work(self, tmp_path: Path):
        """状态命令显示进行中的工作。"""
        from agent_py_agent.cli.local_commands import cmd_status

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.limit = 5
        args.recent = False
        args.json = False

        mock_agent = MagicMock()
        mock_agent.config.agent_name = "test_agent"
        mock_agent.root = tmp_path
        mock_agent.config.gateway_stale_seconds = 300
        mock_agent.config.auto_detect_work_on_startup = False
        mock_agent.local_store.stats.return_value = {"record_count": 100, "event_count": 50, "fts5_enabled": True, "db_path": str(tmp_path / "store.db")}
        mock_agent.subagents.board.build_board.return_value = MagicMock(summary={"total": 0}, hot_list=[], recent=[])
        mock_agent.local_store.timeline.return_value = []

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.local_commands.gateway_paths", return_value=MagicMock(root=tmp_path, state=tmp_path / "state.json", heartbeat=tmp_path / "heartbeat.json")), \
             patch("agent_py_agent.cli.local_commands.gateway_running", return_value=(None, False)), \
             patch("agent_py_agent.cli.local_commands.read_json_file", return_value={}), \
             patch("agent_py_agent.cli.local_commands.gateway_request_counts", return_value={}), \
             patch("agent_py_agent.cli.local_commands.build_status_suggestions", return_value=[]):
            result = cmd_status(args)
            assert result == 0

    def test_status_json_output(self, tmp_path: Path):
        """JSON 格式输出用于脚本处理。"""
        from agent_py_agent.cli.local_commands import cmd_status

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.limit = 5
        args.recent = False
        args.json = True

        mock_agent = MagicMock()
        mock_agent.config.agent_name = "test_agent"
        mock_agent.root = tmp_path
        mock_agent.config.gateway_stale_seconds = 300
        mock_agent.config.auto_detect_work_on_startup = False
        mock_agent.local_store.stats.return_value = {"record_count": 100, "event_count": 50, "fts5_enabled": True, "db_path": str(tmp_path / "store.db")}
        mock_agent.subagents.board.build_board.return_value = MagicMock(summary={"total": 0}, hot_list=[], recent=[])
        mock_agent.local_store.timeline.return_value = []

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.local_commands.gateway_paths", return_value=MagicMock(root=tmp_path, state=tmp_path / "state.json", heartbeat=tmp_path / "heartbeat.json")), \
             patch("agent_py_agent.cli.local_commands.gateway_running", return_value=(None, False)), \
             patch("agent_py_agent.cli.local_commands.read_json_file", return_value={}), \
             patch("agent_py_agent.cli.local_commands.gateway_request_counts", return_value={}), \
             patch("agent_py_agent.cli.local_commands.build_status_suggestions", return_value=[]):
            result = cmd_status(args)
            assert result == 0


class TestLocalDoctorForWorkstream:
    """测试本地诊断命令（workstream 需要健康检查）。"""

    def test_local_doctor_ok(self, tmp_path: Path):
        """本地诊断正常状态。"""
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

    def test_local_doctor_with_issues(self, tmp_path: Path):
        """本地诊断发现问题。"""
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
            "ok": False,
            "stats": {"record_count": 100},
            "source_counts": {"memory": 50},
            "workspace_scope": {"workspace_root": str(tmp_path), "mode": "implicit_cwd"},
            "checks": [
                {"name": "test_check", "ok": False, "severity": "warning", "message": "发现问题"}
            ],
            "suggestions": ["建议执行修复"]
        }

        with patch("agent_py_agent.cli.local_repair_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.local_repair_commands.build_local_doctor_report", return_value=mock_report):
            result = cmd_local_doctor(args)
            assert result == 1


class TestLocalSearchForWorkstream:
    """测试本地搜索（workstream 需要搜索任务）。"""

    def test_local_search_returns_results(self, tmp_path: Path):
        """搜索返回结果。"""
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

    def test_local_search_no_results(self, tmp_path: Path):
        """搜索无结果。"""
        from agent_py_agent.cli.local_commands import cmd_local_search

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.query = "完全不存在的查询"
        args.limit = 10
        args.source_type = None
        args.visibility = None
        args.preview_chars = 500

        mock_agent = MagicMock()
        mock_agent.local_store.search.return_value = []

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent):
            result = cmd_local_search(args)
            assert result == 0


class TestMemoryCommandsForWorkstream:
    """测试记忆命令（workstream 需要持久化上下文）。"""

    def test_memory_list_for_workstream(self, tmp_path: Path):
        """列出记忆用于恢复上下文。"""
        from agent_py_agent.cli.local_commands import cmd_memory_list

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.limit = 20

        mock_record = MagicMock()
        mock_record.__dict__ = {"id": "mem_001", "content": "测试记忆", "kind": "note"}

        mock_agent = MagicMock()
        mock_agent.memory.all.return_value = [mock_record]

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent):
            result = cmd_memory_list(args)
            assert result == 0

    def test_memory_search_for_workstream(self, tmp_path: Path):
        """搜索记忆用于上下文恢复。"""
        from agent_py_agent.cli.local_commands import cmd_memory_search

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.query = "测试"
        args.limit = 5

        mock_agent = MagicMock()
        mock_agent.recall.return_value = []

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent):
            result = cmd_memory_search(args)
            assert result == 0


class TestLocalRebuildForWorkstream:
    """测试重建命令（workstream 需要重建索引）。"""

    def test_local_rebuild_memory(self, tmp_path: Path):
        """重建 memory 索引。"""
        from agent_py_agent.cli.local_commands import cmd_local_rebuild

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.source = ["memory"]
        args.reset = False

        mock_agent = MagicMock()

        with patch("agent_py_agent.cli.local_repair_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.local_repair_commands.rebuild_local_store", return_value={"memory": 50}):
            result = cmd_local_rebuild(args)
            assert result == 0

    def test_local_rebuild_all(self, tmp_path: Path):
        """重建所有索引。"""
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
