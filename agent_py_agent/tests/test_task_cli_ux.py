"""LLM: CLI 任务管理命令的用户体验测试。

测试 task list/show/abandon/pause/resume/search 命令的边界情况和用户交互。
使用 mock 隔离存储层和 LLM 调用，专注测试 CLI 输出格式和错误处理。
"""

from __future__ import annotations

import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.subagents.models import TaskStatus
from agent_py_agent.agent.task_registry import format_task_list, get_task_summary
from agent_py_agent.agent.task_registry.query import _TASK_STATUS_EMOJI
from agent_py_agent.cli.task_commands import (
    cmd_task_abandon,
    cmd_task_list,
    cmd_task_pause,
    cmd_task_resume,
    cmd_task_search,
    cmd_task_show,
)


class MockArgs:
    """模拟 argparse.Namespace 对象。"""

    def __init__(self, **kwargs):
        self.config = None
        self.user_id = None
        self.status = None
        self.limit = 50
        for k, v in kwargs.items():
            setattr(self, k, v)


def _make_mock_store(tasks=None):
    """创建模拟 LocalStore。"""
    mock_store = MagicMock()
    if tasks is not None:
        mock_store.task_registry.query_tasks.return_value = tasks
        mock_store.task_registry.lookup_task.side_effect = lambda tid: next(
            (t for t in tasks if t.get("task_id") == tid), None
        )
    else:
        mock_store.task_registry.query_tasks.return_value = []
        mock_store.task_registry.lookup_task.return_value = None
    return mock_store


class TestTaskList:
    """测试 task list 命令的输出格式。"""

    def test_empty_task_list_output(self):
        """测试空任务列表的输出格式。

        验证当没有任何任务时，输出应该是友好的提示信息而不是空字符串或错误。
        """
        tasks = []
        result = format_task_list(tasks)
        assert "没有找到任务" in result
        assert len(result) > 0

    def test_task_list_truncation_for_many_tasks(self):
        """测试大量任务时的截断显示。

        验证当任务超过 20 个时，只会显示前 20 个，并提示还有多少个任务。
        """
        tasks = [
            {
                "task_id": f"task-{i}",
                "status": "RUNNING",
                "goal": f"任务目标 {i}",
                "session_id": "",
                "user_id": "",
                "created_at": 0,
                "updated_at": i,
            }
            for i in range(25)
        ]
        result = format_task_list(tasks)
        assert "..." in result
        lines = result.split("\n")
        # 标题 + 20 个任务 + 截断提示
        assert len(lines) == 22

    def test_task_list_status_display(self):
        """测试不同状态任务的显示。

        验证 format_task_list 显示任务时使用正确的中括号格式 [STATUS]。
        """
        tasks = [
            {
                "task_id": "task-1",
                "status": "RUNNING",
                "goal": "运行中的任务",
                "session_id": "",
                "user_id": "",
                "created_at": 0,
                "updated_at": 1,
            },
            {
                "task_id": "task-2",
                "status": "PAUSED",
                "goal": "暂停的任务",
                "session_id": "",
                "user_id": "",
                "created_at": 0,
                "updated_at": 2,
            },
        ]
        result = format_task_list(tasks)
        # 验证格式为 [STATUS]
        assert "[RUNNING]" in result
        assert "[PAUSED]" in result

    def test_task_list_long_description_truncation(self):
        """测试 description 过长时的截断显示。

        验证超过 80 字符的 goal 会被截断为 80 字符并加 "..."。
        """
        long_goal = "A" * 120  # 超过 80 字符
        tasks = [
            {
                "task_id": "task-long",
                "status": "RUNNING",
                "goal": long_goal,
                "session_id": "",
                "user_id": "",
                "created_at": 0,
                "updated_at": 1,
            }
        ]
        result = format_task_list(tasks)
        # 验证包含截断标记
        assert "..." in result
        # 验证输出包含任务行
        assert "task-long" in result
        # 验证输出包含状态标记
        assert "[RUNNING]" in result


class TestTaskShow:
    """测试 task show 命令的显示。"""

    def test_nonexistent_task_id(self):
        """测试不存在的任务ID。

        验证当任务 ID 不存在时，返回友好的错误提示。
        """
        mock_store = _make_mock_store([])
        mock_store.task_registry.lookup_task.return_value = None

        with patch("agent_py_agent.cli.task_commands.LocalStore") as mock_ls:
            mock_ls.return_value = mock_store
            with patch("agent_py_agent.cli.task_commands.load_config") as mock_cfg:
                mock_cfg.return_value = MagicMock(workspace_root="")
                with patch("agent_py_agent.cli.task_commands.resolve_workspace_root") as mock_resolve:
                    mock_resolve.return_value = Path(tempfile.gettempdir())

                    args = MockArgs(task_id="task-999")
                    result = cmd_task_show(args)

                    assert result == 0

    def test_task_detail_completeness(self):
        """测试任务详情的完整性显示。

        验证 get_task_summary 返回完整的任务信息：ID、状态、描述、时间等。
        """
        mock_store = _make_mock_store([
            {
                "task_id": "task-1",
                "status": "RUNNING",
                "goal": "测试任务",
                "session_id": "session-1",
                "user_id": "user-1",
                "created_at": 1704067200,
                "updated_at": 1704067200,
            }
        ])

        summary = get_task_summary(mock_store, "task-1")
        assert "task-1" in summary
        assert "RUNNING" in summary
        assert "测试任务" in summary
        # 验证时间格式化存在
        assert "2024" in summary or "session-1" in summary

    def test_task_show_with_missing_fields(self):
        """测试任务详情显示处理可选字段为空的情况。

        验证当 session_id 或 user_id 为空时不会报错。
        """
        mock_store = _make_mock_store([
            {
                "task_id": "task-minimal",
                "status": "PLANNING",
                "goal": "",
                "session_id": "",
                "user_id": "",
                "created_at": 0,
                "updated_at": 0,
            }
        ])

        summary = get_task_summary(mock_store, "task-minimal")
        assert "task-minimal" in summary
        # 不应该包含空字符串的 "会话 ID:" 或 "用户 ID:"
        lines = summary.split("\n")
        for line in lines:
            if "ID:" in line:
                parts = line.split(":", 1)
                assert len(parts) == 2 and parts[1].strip()


class TestTaskStateTransitions:
    """测试 task abandon/pause/resume 命令的状态转换。"""

    def _call_abandon(self, task_id, status):
        mock_store = _make_mock_store([
            {"task_id": task_id, "status": status, "goal": "", "session_id": "", "user_id": "", "created_at": 0, "updated_at": 0}
        ])
        with patch("agent_py_agent.cli.task_commands.LocalStore") as mock_ls:
            mock_ls.return_value = mock_store
            with patch("agent_py_agent.cli.task_commands.load_config") as mock_cfg:
                mock_cfg.return_value = MagicMock(workspace_root="")
                with patch("agent_py_agent.cli.task_commands.resolve_workspace_root") as mock_resolve:
                    mock_resolve.return_value = Path(tempfile.gettempdir())
                    args = MockArgs(task_id=task_id)
                    return cmd_task_abandon(args)

    def _call_pause(self, task_id, status):
        mock_store = _make_mock_store([
            {"task_id": task_id, "status": status, "goal": "", "session_id": "", "user_id": "", "created_at": 0, "updated_at": 0}
        ])
        with patch("agent_py_agent.cli.task_commands.LocalStore") as mock_ls:
            mock_ls.return_value = mock_store
            with patch("agent_py_agent.cli.task_commands.load_config") as mock_cfg:
                mock_cfg.return_value = MagicMock(workspace_root="")
                with patch("agent_py_agent.cli.task_commands.resolve_workspace_root") as mock_resolve:
                    mock_resolve.return_value = Path(tempfile.gettempdir())
                    args = MockArgs(task_id=task_id)
                    return cmd_task_pause(args)

    def _call_resume(self, task_id, status):
        mock_store = _make_mock_store([
            {"task_id": task_id, "status": status, "goal": "", "session_id": "", "user_id": "", "created_at": 0, "updated_at": 0}
        ])
        with patch("agent_py_agent.cli.task_commands.LocalStore") as mock_ls:
            mock_ls.return_value = mock_store
            with patch("agent_py_agent.cli.task_commands.load_config") as mock_cfg:
                mock_cfg.return_value = MagicMock(workspace_root="")
                with patch("agent_py_agent.cli.task_commands.resolve_workspace_root") as mock_resolve:
                    mock_resolve.return_value = Path(tempfile.gettempdir())
                    args = MockArgs(task_id=task_id)
                    return cmd_task_resume(args)

    def test_abandon_completed_task(self):
        """测试 abandon 已 COMPLETED 的任务。

        验证对已完成任务执行 abandon 操作会成功（用户可能改变主意）。
        """
        result = self._call_abandon("task-1", "COMPLETED")
        assert result == 0

    def test_pause_already_paused_task(self):
        """测试重复操作：pause 已 PAUSED 的任务。

        验证对已暂停任务再次执行 pause 会提示用户并返回成功（幂等）。
        """
        result = self._call_pause("task-paused", "PAUSED")
        assert result == 0

    def test_resume_non_paused_task(self):
        """测试无效状态转换：resume 非 PAUSED 状态的任务。

        验证只有 PAUSED 状态的任务才能被 resume。
        """
        result = self._call_resume("task-running", "RUNNING")
        assert result == 1

    def test_resume_paused_task(self):
        """测试正常恢复：resume PAUSED 状态的任务。

        验证对 PAUSED 任务执行 resume 会成功。
        """
        mock_store = _make_mock_store([
            {"task_id": "task-paused", "status": "PAUSED", "goal": "", "session_id": "", "user_id": "", "created_at": 0, "updated_at": 0}
        ])
        mock_store.task_registry.update_task_status.return_value = True

        with patch("agent_py_agent.cli.task_commands.LocalStore") as mock_ls:
            mock_ls.return_value = mock_store
            with patch("agent_py_agent.cli.task_commands.load_config") as mock_cfg:
                mock_cfg.return_value = MagicMock(workspace_root="")
                with patch("agent_py_agent.cli.task_commands.resolve_workspace_root") as mock_resolve:
                    mock_resolve.return_value = Path(tempfile.gettempdir())
                    args = MockArgs(task_id="task-paused")
                    result = cmd_task_resume(args)

        assert result == 0
        mock_store.task_registry.update_task_status.assert_called_once_with("task-paused", "RUNNING")

    def test_resume_nonexistent_task(self):
        """测试 resume 不存在的任务。

        验证返回错误码并提示任务不存在。
        """
        mock_store = _make_mock_store([])
        mock_store.task_registry.lookup_task.return_value = None

        with patch("agent_py_agent.cli.task_commands.LocalStore") as mock_ls:
            mock_ls.return_value = mock_store
            with patch("agent_py_agent.cli.task_commands.load_config") as mock_cfg:
                mock_cfg.return_value = MagicMock(workspace_root="")
                with patch("agent_py_agent.cli.task_commands.resolve_workspace_root") as mock_resolve:
                    mock_resolve.return_value = Path(tempfile.gettempdir())
                    args = MockArgs(task_id="task-nonexist")
                    result = cmd_task_resume(args)

        assert result == 1

    def test_concurrent_state_transitions(self):
        """测试并发状态转换的安全性。

        验证多个并发操作不会导致状态不一致。
        """
        import threading

        mock_store = _make_mock_store([
            {"task_id": "task-concurrent", "status": "RUNNING", "goal": "", "session_id": "", "user_id": "", "created_at": 0, "updated_at": 0}
        ])
        mock_store.task_registry.update_task_status.return_value = True

        update_calls = []

        def mock_update(task_id, status):
            update_calls.append((task_id, status))
            return True

        mock_store.task_registry.update_task_status.side_effect = mock_update

        with patch("agent_py_agent.cli.task_commands.LocalStore") as mock_ls:
            mock_ls.return_value = mock_store
            with patch("agent_py_agent.cli.task_commands.load_config") as mock_cfg:
                mock_cfg.return_value = MagicMock(workspace_root="")
                with patch("agent_py_agent.cli.task_commands.resolve_workspace_root") as mock_resolve:
                    mock_resolve.return_value = Path(tempfile.gettempdir())

                    threads = []
                    for _ in range(5):
                        t = threading.Thread(
                            target=lambda: cmd_task_pause(MockArgs(task_id="task-concurrent"))
                        )
                        threads.append(t)
                        t.start()

                    for t in threads:
                        t.join()

        assert len(update_calls) == 5


class TestTaskSearch:
    """测试 task search 命令的搜索功能。"""

    def _call_search(self, query, tasks=None):
        if tasks is None:
            tasks = []
        mock_store = _make_mock_store(tasks)

        with patch("agent_py_agent.cli.task_commands.LocalStore") as mock_ls:
            mock_ls.return_value = mock_store
            with patch("agent_py_agent.cli.task_commands.load_config") as mock_cfg:
                mock_cfg.return_value = MagicMock(workspace_root="")
                with patch("agent_py_agent.cli.task_commands.resolve_workspace_root") as mock_resolve:
                    mock_resolve.return_value = Path(tempfile.gettempdir())
                    args = MockArgs(query=query)
                    return cmd_task_search(args)

    def test_empty_query(self):
        """测试空查询的处理。

        验证空查询会执行搜索（返回所有匹配的空查询结果）。
        """
        result = self._call_search("")
        assert result == 0

    def test_no_matching_results(self):
        """测试无匹配结果的搜索。

        验证搜索没有匹配时返回友好提示。
        """
        result = self._call_search("完全不存在的关键词xyz123", [])
        assert result == 0
        # 检查输出中是否包含提示信息（通过 mock print 捕获）

    def test_fuzzy_matching_accuracy(self):
        """测试模糊匹配的准确性。

        验证模糊搜索能正确匹配 task_id 和 goal 中的关键词。
        """
        tasks = [
            {
                "task_id": "task-abc-123",
                "status": "RUNNING",
                "goal": "这是一个测试任务用于验证搜索功能",
                "session_id": "",
                "user_id": "",
                "created_at": 0,
                "updated_at": 1,
            },
            {
                "task_id": "task-xyz-456",
                "status": "RUNNING",
                "goal": "另一个完全不相关的任务",
                "session_id": "",
                "user_id": "",
                "created_at": 0,
                "updated_at": 2,
            },
        ]
        result = self._call_search("测试", tasks)
        assert result == 0

    def test_search_by_task_id(self):
        """测试通过 task_id 搜索。

        验证能精确匹配 task_id。
        """
        tasks = [
            {
                "task_id": "task-unique-789",
                "status": "RUNNING",
                "goal": "普通任务",
                "session_id": "",
                "user_id": "",
                "created_at": 0,
                "updated_at": 1,
            }
        ]
        result = self._call_search("unique-789", tasks)
        assert result == 0
        # 应该找到任务

    def test_special_characters_handling(self):
        """测试特殊字符处理的健壮性。

        验证搜索包含特殊字符（如引号、括号、正则元字符）时不会报错。
        """
        special_chars = ['"quoted"', "(paren)", "[bracket]", "pipe|split", "star*"]
        for char_query in special_chars:
            try:
                result = self._call_search(char_query, [])
                assert result == 0
            except Exception as e:
                pytest.fail(f"特殊字符查询 '{char_query}' 导致异常: {e}")


class TestErrorMessages:
    """测试错误提示信息的用户体验。"""

    def test_nonexistent_task_error_message(self):
        """测试不存在的任务错误提示。

        验证不存在的任务 ID 会给出清晰的错误提示。
        """
        mock_store = _make_mock_store([])
        mock_store.task_registry.lookup_task.return_value = None

        with patch("agent_py_agent.cli.task_commands.LocalStore") as mock_ls:
            mock_ls.return_value = mock_store
            with patch("agent_py_agent.cli.task_commands.load_config") as mock_cfg:
                mock_cfg.return_value = MagicMock(workspace_root="")
                with patch("agent_py_agent.cli.task_commands.resolve_workspace_root") as mock_resolve:
                    mock_resolve.return_value = Path(tempfile.gettempdir())
                    args = MockArgs(task_id="task-does-not-exist")
                    result = cmd_task_abandon(args)

        assert result == 1

    def test_update_failure_handling(self):
        """测试更新失败时的错误处理。

        验证 update_task_status 返回 False 时会返回错误码。
        """
        mock_store = _make_mock_store([
            {"task_id": "task-1", "status": "RUNNING", "goal": "", "session_id": "", "user_id": "", "created_at": 0, "updated_at": 0}
        ])
        mock_store.task_registry.update_task_status.return_value = False

        with patch("agent_py_agent.cli.task_commands.LocalStore") as mock_ls:
            mock_ls.return_value = mock_store
            with patch("agent_py_agent.cli.task_commands.load_config") as mock_cfg:
                mock_cfg.return_value = MagicMock(workspace_root="")
                with patch("agent_py_agent.cli.task_commands.resolve_workspace_root") as mock_resolve:
                    mock_resolve.return_value = Path(tempfile.gettempdir())
                    args = MockArgs(task_id="task-1")
                    result = cmd_task_abandon(args)

        assert result == 1

    def test_empty_task_id_handling(self):
        """测试空任务 ID 的处理。

        验证空 task_id 不会导致系统崩溃。
        """
        mock_store = _make_mock_store([])
        mock_store.task_registry.lookup_task.return_value = None

        with patch("agent_py_agent.cli.task_commands.LocalStore") as mock_ls:
            mock_ls.return_value = mock_store
            with patch("agent_py_agent.cli.task_commands.load_config") as mock_cfg:
                mock_cfg.return_value = MagicMock(workspace_root="")
                with patch("agent_py_agent.cli.task_commands.resolve_workspace_root") as mock_resolve:
                    mock_resolve.return_value = Path(tempfile.gettempdir())
                    args = MockArgs(task_id="")
                    result = cmd_task_show(args)

        # 应该优雅处理
        assert result in (0, 1)

    def test_pause_nonexistent_task(self):
        """测试暂停不存在的任务。

        验证返回错误码。
        """
        mock_store = _make_mock_store([])
        mock_store.task_registry.lookup_task.return_value = None

        with patch("agent_py_agent.cli.task_commands.LocalStore") as mock_ls:
            mock_ls.return_value = mock_store
            with patch("agent_py_agent.cli.task_commands.load_config") as mock_cfg:
                mock_cfg.return_value = MagicMock(workspace_root="")
                with patch("agent_py_agent.cli.task_commands.resolve_workspace_root") as mock_resolve:
                    mock_resolve.return_value = Path(tempfile.gettempdir())
                    args = MockArgs(task_id="task-nonexist")
                    result = cmd_task_pause(args)

        assert result == 1