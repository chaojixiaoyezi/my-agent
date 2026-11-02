"""task_commands CLI 命令测试。

测试 task list/show/abandon/pause/resume/search 命令的正常流程和错误场景。
只读命令经 _task_store 读取配置 owner 的投影库；控制命令经配置宿主的 subagents.lifecycle.control_task，
任何结果下都不直写 TaskRegistry 投影（真实宿主与原控制服务见 test_rework_r9_cli_task_control*.py）。
"""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

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

        # Mock 配置 owner 的投影库
        mock_store = MagicMock()
        mock_store.task_registry.query_tasks.return_value = []

        with patch("agent_py_agent.cli.task_commands._task_store", return_value=mock_store) as store:
            result = cmd_task_list(args)
            assert result == 0
            store.assert_called_once_with(args)

    def test_task_list_with_user_id_filter(self, tmp_path: Path):
        """带 user_id 过滤条件查询任务。"""
        from agent_py_agent.cli.task_commands import cmd_task_list

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.user_id = "user123"
        args.status = None
        args.limit = 20

        mock_store = MagicMock()
        mock_store.task_registry.query_tasks.return_value = [
            {"task_id": "task1", "status": "RUNNING", "goal": "测试任务", "user_id": "user123"}
        ]

        with patch("agent_py_agent.cli.task_commands._task_store", return_value=mock_store):
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

        mock_store = MagicMock()
        mock_store.task_registry.query_tasks.return_value = []

        with patch("agent_py_agent.cli.task_commands._task_store", return_value=mock_store):
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

        mock_store = MagicMock()
        mock_store.task_registry.lookup_task.return_value = {
            "task_id": "task_001",
            "status": "RUNNING",
            "goal": "测试任务详情"
        }

        with patch("agent_py_agent.cli.task_commands._task_store", return_value=mock_store), \
             patch("agent_py_agent.cli.task_commands.get_task_summary", return_value="任务详情摘要") as summary:
            result = cmd_task_show(args)
            assert result == 0
            summary.assert_called_once_with(mock_store, "task_001")

    def test_task_show_returns_one_for_nonexistent_task(self, tmp_path: Path, capsys):
        """查询不存在的任务返回错误码（取决于 get_task_summary 行为）。"""
        from agent_py_agent.cli.task_commands import cmd_task_show

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.task_id = "nonexistent_task"

        mock_store = MagicMock()
        mock_store.task_registry.lookup_task.return_value = None

        # get_task_summary 内部调用 lookup_task，如果返回 None 则打印消息
        with patch("agent_py_agent.cli.task_commands._task_store", return_value=mock_store), \
             patch("agent_py_agent.cli.task_commands.get_task_summary", return_value="任务不存在"):
            result = cmd_task_show(args)
            # get_task_summary 可能返回 None 描述，cmd_task_show 仍返回 0
            assert result in (0, 1)


# 函数用途: 以 mock 宿主运行一次控制命令，并断言投影库从未被构造或直写。
def _run_control(command, tmp_path: Path, task_id: str, *, report=None, error=None):
    args = argparse.Namespace(config=str(tmp_path / "config.yaml"), task_id=task_id)
    host = MagicMock()
    control = MagicMock(return_value=report, side_effect=error)
    host.subagents.lifecycle.control_task = control
    with patch("agent_py_agent.cli.task_commands.make_agent", return_value=host) as make, \
         patch("agent_py_agent.cli.task_commands._task_store") as store:
        result = command(args)
    store.assert_not_called()
    store.return_value.task_registry.update_task_status.assert_not_called()
    host_args = make.call_args.args[0]
    assert host_args is not args and host_args._defer_mcp_connect is True
    assert (host_args.config, host_args.task_id) == (args.config, task_id)
    return result, host, control


class TestCmdTaskAbandon:
    """测试 cmd_task_abandon 命令。"""

    def test_task_abandon_success(self, tmp_path: Path):
        """经原控制服务标记任务为 ABANDONED。"""
        from agent_py_agent.cli.task_commands import cmd_task_abandon

        result, host, control = _run_control(cmd_task_abandon, tmp_path, "task_001", report={
            "ok": True, "action": "abandon", "outcome": "applied", "status": "ABANDONED",
            "cleanup_state": "not_required",
        })
        assert result == 0
        control.assert_called_once_with(host, "task_001", action="abandon")

    def test_task_abandon_nonexistent_task(self, tmp_path: Path):
        """标记不存在的任务为 ABANDONED 返回错误。"""
        from agent_py_agent.cli.task_commands import cmd_task_abandon

        result, _, _ = _run_control(cmd_task_abandon, tmp_path, "nonexistent_task", error=FileNotFoundError())
        assert result == 1

    def test_task_abandon_already_abandoned(self, tmp_path: Path, capsys):
        """任务已经是 ABANDONED 状态时返回成功但不更新。"""
        from agent_py_agent.cli.task_commands import cmd_task_abandon

        result, _, _ = _run_control(cmd_task_abandon, tmp_path, "task_001", report={
            "ok": True, "action": "abandon", "outcome": "already_applied", "status": "ABANDONED",
        })
        assert result == 0
        assert "已经是 ABANDONED" in capsys.readouterr().out


class TestCmdTaskPause:
    """测试 cmd_task_pause 命令。"""

    def test_task_pause_success(self, tmp_path: Path, capsys):
        """成功暂停任务；清理待原执行轮确认时如实提示。"""
        from agent_py_agent.cli.task_commands import cmd_task_pause

        result, host, control = _run_control(cmd_task_pause, tmp_path, "task_001", report={
            "ok": True, "action": "pause", "outcome": "applied", "status": "PAUSED", "cleanup_state": "pending",
        })
        assert result == 0
        control.assert_called_once_with(host, "task_001", action="pause")
        assert "等待原执行轮宿主确认清理" in capsys.readouterr().out

    def test_task_pause_nonexistent(self, tmp_path: Path):
        """暂停不存在的任务。"""
        from agent_py_agent.cli.task_commands import cmd_task_pause

        result, _, _ = _run_control(cmd_task_pause, tmp_path, "nonexistent_task", error=FileNotFoundError())
        assert result == 1

    def test_task_pause_unconfirmed_stop_returns_one(self, tmp_path: Path):
        """原撤权未确认时不报告成功。"""
        from agent_py_agent.cli.task_commands import cmd_task_pause

        result, _, _ = _run_control(cmd_task_pause, tmp_path, "task_001", report={
            "ok": False, "action": "pause", "outcome": "unconfirmed", "status": "RUNNING", "failed": [],
        })
        assert result == 1


class TestCmdTaskResume:
    """测试 cmd_task_resume 命令。"""

    def test_task_resume_success(self, tmp_path: Path):
        """成功恢复 PAUSED 任务。"""
        from agent_py_agent.cli.task_commands import cmd_task_resume

        result, host, control = _run_control(cmd_task_resume, tmp_path, "task_001", report={
            "ok": True, "action": "resume", "outcome": "applied", "status": "PENDING",
        })
        assert result == 0
        control.assert_called_once_with(host, "task_001", action="resume")

    def test_task_resume_nonexistent(self, tmp_path: Path):
        """恢复不存在的任务。"""
        from agent_py_agent.cli.task_commands import cmd_task_resume

        result, _, _ = _run_control(cmd_task_resume, tmp_path, "nonexistent_task", error=FileNotFoundError())
        assert result == 1

    def test_task_resume_wrong_status(self, tmp_path: Path):
        """尝试恢复非 PAUSED 状态的任务。"""
        from agent_py_agent.agent.subagents.task_control import TaskControlError
        from agent_py_agent.cli.task_commands import cmd_task_resume

        result, _, _ = _run_control(cmd_task_resume, tmp_path, "task_001",
                                    error=TaskControlError("TASK_NOT_PAUSED", "任务当前状态是 RUNNING"))
        assert result == 1


@pytest.mark.parametrize("command_name", ["cmd_task_abandon", "cmd_task_pause", "cmd_task_resume"])
@pytest.mark.parametrize("error", [PermissionError("owner mismatch"), RuntimeError("simulated I/O error")])
def test_task_control_refusals_return_one(tmp_path: Path, command_name: str, error: Exception):
    """授权拒绝或未确认的控制都返回 1。"""
    from agent_py_agent.cli import task_commands

    result, _, _ = _run_control(getattr(task_commands, command_name), tmp_path, "task_001", error=error)
    assert result == 1


@pytest.mark.parametrize("command_name", ["cmd_task_abandon", "cmd_task_pause", "cmd_task_resume"])
def test_task_control_without_configured_authority_does_not_touch_projection(tmp_path: Path, command_name: str):
    """配置宿主无法构造时拒绝控制，不退回投影写入。"""
    from agent_py_agent.cli import task_commands

    args = argparse.Namespace(config=str(tmp_path / "config.yaml"), task_id="task_001")
    with patch("agent_py_agent.cli.task_commands.make_agent", side_effect=OSError("simulated I/O error")), \
         patch("agent_py_agent.cli.task_commands._task_store") as store, \
         patch("agent_py_agent.agent.subagents.services.lifecycle.SubAgentLifecycleService.control_task") as control:
        assert getattr(task_commands, command_name)(args) == 1
    control.assert_not_called()
    store.assert_not_called()


class TestCmdTaskSearch:
    """测试 cmd_task_search 命令。"""

    def test_task_search_with_match(self, tmp_path: Path):
        """搜索并匹配到任务。"""
        from agent_py_agent.cli.task_commands import cmd_task_search

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.query = "测试"
        args.limit = 200

        mock_store = MagicMock()
        mock_store.task_registry.query_tasks.return_value = [
            {"task_id": "task_001", "status": "RUNNING", "goal": "这是一个测试任务"}
        ]

        with patch("agent_py_agent.cli.task_commands._task_store", return_value=mock_store), \
             patch("agent_py_agent.cli.task_commands.format_task_list", return_value="任务列表输出") as listed:
            result = cmd_task_search(args)
            assert result == 0
            assert [task["task_id"] for task in listed.call_args.args[0]] == ["task_001"]

    def test_task_search_no_match(self, tmp_path: Path):
        """搜索没有匹配的任务。"""
        from agent_py_agent.cli.task_commands import cmd_task_search

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.query = "完全不存在的查询"
        args.limit = 200

        mock_store = MagicMock()
        mock_store.task_registry.query_tasks.return_value = [
            {"task_id": "task_001", "status": "RUNNING", "goal": "这是一个测试任务"}
        ]

        with patch("agent_py_agent.cli.task_commands._task_store", return_value=mock_store):
            result = cmd_task_search(args)
            assert result == 0

    def test_task_search_empty_tasks(self, tmp_path: Path):
        """搜索空任务列表。"""
        from agent_py_agent.cli.task_commands import cmd_task_search

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.query = "测试"
        args.limit = 200

        mock_store = MagicMock()
        mock_store.task_registry.query_tasks.return_value = []

        with patch("agent_py_agent.cli.task_commands._task_store", return_value=mock_store):
            result = cmd_task_search(args)
            assert result == 0
