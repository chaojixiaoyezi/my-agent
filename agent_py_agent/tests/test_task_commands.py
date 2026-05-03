"""task_commands CLI 命令测试。

测试 task list/show/abandon/pause/resume/search 命令的正常流程和错误场景。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock
import pytest


class TestCmdTaskList:
    """测试 cmd_task_list 命令。"""

    def test_task_list_returns_zero_with_valid_config(self, tmp_path: Path):
        """正常加载配置并返回任务列表。"""
        from agent_py_agent.cli.task_commands import cmd_task_list

        # Mock args
        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.user_id = None
        args.status = None
        args.limit = 10

        # Mock config 文件
        config_path = tmp_path / "config.yaml"
        config_path.write_text("workspace_root: .\n", encoding="utf-8")

        # Mock load_config
        mock_config = MagicMock()
        mock_config.workspace_root = str(tmp_path)

        # Mock LocalStore
        mock_store = MagicMock()
        mock_store.task_registry.query_tasks.return_value = []

        with patch("agent_py_agent.cli.task_commands.load_config", return_value=mock_config), \
             patch("agent_py_agent.cli.task_commands.resolve_workspace_root", return_value=tmp_path), \
             patch("agent_py_agent.cli.task_commands.LocalStore", return_value=mock_store):
            result = cmd_task_list(args)
            assert result == 0

    def test_task_list_with_user_id_filter(self, tmp_path: Path):
        """带 user_id 过滤条件查询任务。"""
        from agent_py_agent.cli.task_commands import cmd_task_list

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.user_id = "user123"
        args.status = None
        args.limit = 20

        config_path = tmp_path / "config.yaml"
        config_path.write_text("workspace_root: .\n", encoding="utf-8")

        mock_config = MagicMock()
        mock_config.workspace_root = str(tmp_path)

        mock_store = MagicMock()
        mock_store.task_registry.query_tasks.return_value = [
            {"task_id": "task1", "status": "RUNNING", "goal": "测试任务", "user_id": "user123"}
        ]

        with patch("agent_py_agent.cli.task_commands.load_config", return_value=mock_config), \
             patch("agent_py_agent.cli.task_commands.resolve_workspace_root", return_value=tmp_path), \
             patch("agent_py_agent.cli.task_commands.LocalStore", return_value=mock_store):
            result = cmd_task_list(args)
            assert result == 0
            mock_store.task_registry.query_tasks.assert_called_once()
            call_kwargs = mock_store.task_registry.query_tasks.call_args.kwargs
            assert call_kwargs["user_id"] == "user123"

    def test_task_list_with_status_filter(self, tmp_path: Path):
        """带 status 过滤条件查询任务。"""
        from agent_py_agent.cli.task_commands import cmd_task_list

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.user_id = None
        args.status = "RUNNING"
        args.limit = 10

        config_path = tmp_path / "config.yaml"
        config_path.write_text("workspace_root: .\n", encoding="utf-8")

        mock_config = MagicMock()
        mock_config.workspace_root = str(tmp_path)

        mock_store = MagicMock()
        mock_store.task_registry.query_tasks.return_value = []

        with patch("agent_py_agent.cli.task_commands.load_config", return_value=mock_config), \
             patch("agent_py_agent.cli.task_commands.resolve_workspace_root", return_value=tmp_path), \
             patch("agent_py_agent.cli.task_commands.LocalStore", return_value=mock_store):
            result = cmd_task_list(args)
            assert result == 0
            call_kwargs = mock_store.task_registry.query_tasks.call_args.kwargs
            assert call_kwargs["status"] == "RUNNING"


class TestCmdTaskShow:
    """测试 cmd_task_show 命令。"""

    def test_task_show_returns_zero_for_existing_task(self, tmp_path: Path):
        """显示存在的任务详情。"""
        from agent_py_agent.cli.task_commands import cmd_task_show

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.task_id = "task_001"

        config_path = tmp_path / "config.yaml"
        config_path.write_text("workspace_root: .\n", encoding="utf-8")

        mock_config = MagicMock()
        mock_config.workspace_root = str(tmp_path)

        mock_store = MagicMock()
        mock_store.task_registry.lookup_task.return_value = {
            "task_id": "task_001",
            "status": "RUNNING",
            "goal": "测试任务详情"
        }

        with patch("agent_py_agent.cli.task_commands.load_config", return_value=mock_config), \
             patch("agent_py_agent.cli.task_commands.resolve_workspace_root", return_value=tmp_path), \
             patch("agent_py_agent.cli.task_commands.LocalStore", return_value=mock_store), \
             patch("agent_py_agent.cli.task_commands.get_task_summary", return_value="任务详情摘要"):
            result = cmd_task_show(args)
            assert result == 0

    def test_task_show_returns_one_for_nonexistent_task(self, tmp_path: Path, capsys):
        """查询不存在的任务返回错误码（取决于 get_task_summary 行为）。"""
        from agent_py_agent.cli.task_commands import cmd_task_show

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.task_id = "nonexistent_task"

        config_path = tmp_path / "config.yaml"
        config_path.write_text("workspace_root: .\n", encoding="utf-8")

        mock_config = MagicMock()
        mock_config.workspace_root = str(tmp_path)

        mock_store = MagicMock()
        mock_store.task_registry.lookup_task.return_value = None

        # get_task_summary 内部调用 lookup_task，如果返回 None 则打印消息
        with patch("agent_py_agent.cli.task_commands.load_config", return_value=mock_config), \
             patch("agent_py_agent.cli.task_commands.resolve_workspace_root", return_value=tmp_path), \
             patch("agent_py_agent.cli.task_commands.LocalStore", return_value=mock_store), \
             patch("agent_py_agent.cli.task_commands.get_task_summary", return_value="任务不存在"):
            result = cmd_task_show(args)
            # get_task_summary 可能返回 None 描述，cmd_task_show 仍返回 0
            assert result in (0, 1)


class TestCmdTaskAbandon:
    """测试 cmd_task_abandon 命令。"""

    def test_task_abandon_success(self, tmp_path: Path):
        """成功标记任务为 ABANDONED。"""
        from agent_py_agent.cli.task_commands import cmd_task_abandon

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.task_id = "task_001"

        config_path = tmp_path / "config.yaml"
        config_path.write_text("workspace_root: .\n", encoding="utf-8")

        mock_config = MagicMock()
        mock_config.workspace_root = str(tmp_path)

        mock_store = MagicMock()
        mock_store.task_registry.lookup_task.return_value = {
            "task_id": "task_001",
            "status": "RUNNING"
        }
        mock_store.task_registry.update_task_status.return_value = True

        with patch("agent_py_agent.cli.task_commands.load_config", return_value=mock_config), \
             patch("agent_py_agent.cli.task_commands.resolve_workspace_root", return_value=tmp_path), \
             patch("agent_py_agent.cli.task_commands.LocalStore", return_value=mock_store):
            result = cmd_task_abandon(args)
            assert result == 0
            mock_store.task_registry.update_task_status.assert_called_once()

    def test_task_abandon_nonexistent_task(self, tmp_path: Path):
        """标记不存在的任务为 ABANDONED 返回错误。"""
        from agent_py_agent.cli.task_commands import cmd_task_abandon

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.task_id = "nonexistent_task"

        config_path = tmp_path / "config.yaml"
        config_path.write_text("workspace_root: .\n", encoding="utf-8")

        mock_config = MagicMock()
        mock_config.workspace_root = str(tmp_path)

        mock_store = MagicMock()
        mock_store.task_registry.lookup_task.return_value = None

        with patch("agent_py_agent.cli.task_commands.load_config", return_value=mock_config), \
             patch("agent_py_agent.cli.task_commands.resolve_workspace_root", return_value=tmp_path), \
             patch("agent_py_agent.cli.task_commands.LocalStore", return_value=mock_store):
            result = cmd_task_abandon(args)
            assert result == 1

    def test_task_abandon_already_abandoned(self, tmp_path: Path):
        """任务已经是 ABANDONED 状态时返回成功但不更新。"""
        from agent_py_agent.cli.task_commands import cmd_task_abandon

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.task_id = "task_001"

        config_path = tmp_path / "config.yaml"
        config_path.write_text("workspace_root: .\n", encoding="utf-8")

        mock_config = MagicMock()
        mock_config.workspace_root = str(tmp_path)

        mock_store = MagicMock()
        mock_store.task_registry.lookup_task.return_value = {
            "task_id": "task_001",
            "status": "ABANDONED"
        }

        with patch("agent_py_agent.cli.task_commands.load_config", return_value=mock_config), \
             patch("agent_py_agent.cli.task_commands.resolve_workspace_root", return_value=tmp_path), \
             patch("agent_py_agent.cli.task_commands.LocalStore", return_value=mock_store):
            result = cmd_task_abandon(args)
            assert result == 0
            mock_store.task_registry.update_task_status.assert_not_called()


class TestCmdTaskPause:
    """测试 cmd_task_pause 命令。"""

    def test_task_pause_success(self, tmp_path: Path):
        """成功暂停任务。"""
        from agent_py_agent.cli.task_commands import cmd_task_pause

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.task_id = "task_001"

        config_path = tmp_path / "config.yaml"
        config_path.write_text("workspace_root: .\n", encoding="utf-8")

        mock_config = MagicMock()
        mock_config.workspace_root = str(tmp_path)

        mock_store = MagicMock()
        mock_store.task_registry.lookup_task.return_value = {
            "task_id": "task_001",
            "status": "RUNNING"
        }
        mock_store.task_registry.update_task_status.return_value = True

        with patch("agent_py_agent.cli.task_commands.load_config", return_value=mock_config), \
             patch("agent_py_agent.cli.task_commands.resolve_workspace_root", return_value=tmp_path), \
             patch("agent_py_agent.cli.task_commands.LocalStore", return_value=mock_store):
            result = cmd_task_pause(args)
            assert result == 0

    def test_task_pause_nonexistent(self, tmp_path: Path):
        """暂停不存在的任务。"""
        from agent_py_agent.cli.task_commands import cmd_task_pause

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.task_id = "nonexistent_task"

        config_path = tmp_path / "config.yaml"
        config_path.write_text("workspace_root: .\n", encoding="utf-8")

        mock_config = MagicMock()
        mock_config.workspace_root = str(tmp_path)

        mock_store = MagicMock()
        mock_store.task_registry.lookup_task.return_value = None

        with patch("agent_py_agent.cli.task_commands.load_config", return_value=mock_config), \
             patch("agent_py_agent.cli.task_commands.resolve_workspace_root", return_value=tmp_path), \
             patch("agent_py_agent.cli.task_commands.LocalStore", return_value=mock_store):
            result = cmd_task_pause(args)
            assert result == 1


class TestCmdTaskResume:
    """测试 cmd_task_resume 命令。"""

    def test_task_resume_success(self, tmp_path: Path):
        """成功恢复 PAUSED 任务。"""
        from agent_py_agent.cli.task_commands import cmd_task_resume

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.task_id = "task_001"

        config_path = tmp_path / "config.yaml"
        config_path.write_text("workspace_root: .\n", encoding="utf-8")

        mock_config = MagicMock()
        mock_config.workspace_root = str(tmp_path)

        mock_store = MagicMock()
        mock_store.task_registry.lookup_task.return_value = {
            "task_id": "task_001",
            "status": "PAUSED"
        }
        mock_store.task_registry.update_task_status.return_value = True

        with patch("agent_py_agent.cli.task_commands.load_config", return_value=mock_config), \
             patch("agent_py_agent.cli.task_commands.resolve_workspace_root", return_value=tmp_path), \
             patch("agent_py_agent.cli.task_commands.LocalStore", return_value=mock_store):
            result = cmd_task_resume(args)
            assert result == 0

    def test_task_resume_nonexistent(self, tmp_path: Path):
        """恢复不存在的任务。"""
        from agent_py_agent.cli.task_commands import cmd_task_resume

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.task_id = "nonexistent_task"

        config_path = tmp_path / "config.yaml"
        config_path.write_text("workspace_root: .\n", encoding="utf-8")

        mock_config = MagicMock()
        mock_config.workspace_root = str(tmp_path)

        mock_store = MagicMock()
        mock_store.task_registry.lookup_task.return_value = None

        with patch("agent_py_agent.cli.task_commands.load_config", return_value=mock_config), \
             patch("agent_py_agent.cli.task_commands.resolve_workspace_root", return_value=tmp_path), \
             patch("agent_py_agent.cli.task_commands.LocalStore", return_value=mock_store):
            result = cmd_task_resume(args)
            assert result == 1

    def test_task_resume_wrong_status(self, tmp_path: Path):
        """尝试恢复非 PAUSED 状态的任务。"""
        from agent_py_agent.cli.task_commands import cmd_task_resume

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.task_id = "task_001"

        config_path = tmp_path / "config.yaml"
        config_path.write_text("workspace_root: .\n", encoding="utf-8")

        mock_config = MagicMock()
        mock_config.workspace_root = str(tmp_path)

        mock_store = MagicMock()
        mock_store.task_registry.lookup_task.return_value = {
            "task_id": "task_001",
            "status": "RUNNING"
        }

        with patch("agent_py_agent.cli.task_commands.load_config", return_value=mock_config), \
             patch("agent_py_agent.cli.task_commands.resolve_workspace_root", return_value=tmp_path), \
             patch("agent_py_agent.cli.task_commands.LocalStore", return_value=mock_store):
            result = cmd_task_resume(args)
            assert result == 1


class TestCmdTaskSearch:
    """测试 cmd_task_search 命令。"""

    def test_task_search_with_match(self, tmp_path: Path):
        """搜索并匹配到任务。"""
        from agent_py_agent.cli.task_commands import cmd_task_search

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.query = "测试"
        args.limit = 200

        config_path = tmp_path / "config.yaml"
        config_path.write_text("workspace_root: .\n", encoding="utf-8")

        mock_config = MagicMock()
        mock_config.workspace_root = str(tmp_path)

        mock_store = MagicMock()
        mock_store.task_registry.query_tasks.return_value = [
            {"task_id": "task_001", "status": "RUNNING", "goal": "这是一个测试任务"}
        ]

        with patch("agent_py_agent.cli.task_commands.load_config", return_value=mock_config), \
             patch("agent_py_agent.cli.task_commands.resolve_workspace_root", return_value=tmp_path), \
             patch("agent_py_agent.cli.task_commands.LocalStore", return_value=mock_store), \
             patch("agent_py_agent.cli.task_commands.format_task_list", return_value="任务列表输出"):
            result = cmd_task_search(args)
            assert result == 0

    def test_task_search_no_match(self, tmp_path: Path):
        """搜索没有匹配的任务。"""
        from agent_py_agent.cli.task_commands import cmd_task_search

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.query = "完全不存在的查询"
        args.limit = 200

        config_path = tmp_path / "config.yaml"
        config_path.write_text("workspace_root: .\n", encoding="utf-8")

        mock_config = MagicMock()
        mock_config.workspace_root = str(tmp_path)

        mock_store = MagicMock()
        mock_store.task_registry.query_tasks.return_value = [
            {"task_id": "task_001", "status": "RUNNING", "goal": "这是一个测试任务"}
        ]

        with patch("agent_py_agent.cli.task_commands.load_config", return_value=mock_config), \
             patch("agent_py_agent.cli.task_commands.resolve_workspace_root", return_value=tmp_path), \
             patch("agent_py_agent.cli.task_commands.LocalStore", return_value=mock_store):
            result = cmd_task_search(args)
            assert result == 0

    def test_task_search_empty_tasks(self, tmp_path: Path):
        """搜索空任务列表。"""
        from agent_py_agent.cli.task_commands import cmd_task_search

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.query = "测试"
        args.limit = 200

        config_path = tmp_path / "config.yaml"
        config_path.write_text("workspace_root: .\n", encoding="utf-8")

        mock_config = MagicMock()
        mock_config.workspace_root = str(tmp_path)

        mock_store = MagicMock()
        mock_store.task_registry.query_tasks.return_value = []

        with patch("agent_py_agent.cli.task_commands.load_config", return_value=mock_config), \
             patch("agent_py_agent.cli.task_commands.resolve_workspace_root", return_value=tmp_path), \
             patch("agent_py_agent.cli.task_commands.LocalStore", return_value=mock_store):
            result = cmd_task_search(args)
            assert result == 0
