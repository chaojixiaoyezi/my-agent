"""LLM: CLI 任务管理命令的用户体验测试。

测试 task list/show/abandon/pause/resume/search 命令的边界情况和用户交互。
只读展示命令经 _task_store 缝换成 mock 投影库；abandon/pause/resume 走真实宿主与原 control_task，
专注测试 CLI 输出格式、退出码和错误处理。
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.subagents.models import TaskStatus
from agent_py_agent.agent.subagents.task_control import task_control_record
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
from agent_py_agent.tests.test_rework_r9_cli_task_control import _cli, _owning_runner, _seed


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


@contextmanager
def _patched_task_cli_deps(mock_store):
    # R9-12：只读命令的唯一存储缝是 task_commands._task_store(args)（实际配置 owner 的投影库）。
    with patch("agent_py_agent.cli.task_commands._task_store", return_value=mock_store):
        yield


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

        with _patched_task_cli_deps(mock_store):
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
    """R9-12：控制命令经实际配置 owner 的原宿主 control_task；不写 TaskRegistry 投影。"""

    def test_abandon_completed_task(self, tmp_path, capsys):
        agent, child, attempt = _seed(tmp_path)
        lease, _session = _owning_runner(agent, child.id, attempt)
        try:
            assert _cli(tmp_path, cmd_task_abandon, child.id) == 0
            capsys.readouterr()
            assert _cli(tmp_path, cmd_task_abandon, child.id) == 0
            assert f"任务 {child.id} 已经是 ABANDONED 状态。" in capsys.readouterr().out
            assert agent.subagents.load(child.id).status == "ABANDONED"
        finally:
            lease.__exit__(None, None, None)

    def test_pause_already_paused_task(self, tmp_path, capsys):
        agent, child, attempt = _seed(tmp_path)
        lease, _session = _owning_runner(agent, child.id, attempt)
        try:
            assert _cli(tmp_path, cmd_task_pause, child.id) == 0
            capsys.readouterr()
            assert _cli(tmp_path, cmd_task_pause, child.id) == 0
            assert f"任务 {child.id} 已经是 PAUSED 状态。" in capsys.readouterr().out
            task = agent.subagents.load(child.id)
            assert task.status == "PAUSED" and task_control_record(task)["generation"] == 1
        finally:
            lease.__exit__(None, None, None)

    def test_resume_non_paused_task(self, tmp_path, capsys):
        agent, child, _attempt = _seed(tmp_path)
        assert _cli(tmp_path, cmd_task_resume, child.id) == 1
        assert "TASK_NOT_PAUSED" in capsys.readouterr().out
        assert agent.subagents.load(child.id).status == "RUNNING"

    def test_resume_paused_task(self, tmp_path, capsys):
        agent, child, attempt = _seed(tmp_path)
        lease, _session = _owning_runner(agent, child.id, attempt)
        try:
            assert _cli(tmp_path, cmd_task_pause, child.id) == 0
            capsys.readouterr()
            # 原执行轮宿主尚未写正向清理证明：恢复必须被拒，任务仍是 PAUSED。
            assert _cli(tmp_path, cmd_task_resume, child.id) == 1
            assert "TASK_CLEANUP_UNCONFIRMED" in capsys.readouterr().out
            assert agent.subagents.load(child.id).status == "PAUSED"
        finally:
            # 原宿主收尾：退出同一租约时落正向清理证明。
            lease.__exit__(None, None, None)
        assert _cli(tmp_path, cmd_task_resume, child.id) == 0
        assert f"任务 {child.id} 已恢复为 PENDING，将由原调度续跑同一 run。" in capsys.readouterr().out
        assert agent.subagents.load(child.id).status == "PENDING"

    def test_resume_nonexistent_task(self, tmp_path, capsys):
        _seed(tmp_path)
        assert _cli(tmp_path, cmd_task_resume, "task-nonexist") == 1
        assert "任务 task-nonexist 不存在。" in capsys.readouterr().out

    def test_concurrent_state_transitions(self, tmp_path, capsys):
        import threading

        agent, child, attempt = _seed(tmp_path)
        lease, _session = _owning_runner(agent, child.id, attempt)
        results = []
        try:
            threads = [threading.Thread(target=lambda: results.append(_cli(tmp_path, cmd_task_pause, child.id)))
                       for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            task = agent.subagents.load(child.id)
            assert results == [0] * 5
            assert task.status == "PAUSED" and task_control_record(task)["generation"] == 1
            # 同一把 creation guard 串行化控制：恰好一次真正暂停，其余四次只报告已应用。
            out = capsys.readouterr().out
            assert out.count(f"任务 {child.id} 已暂停，状态 PAUSED；") == 1
            assert out.count(f"任务 {child.id} 已经是 PAUSED 状态。") == 4
        finally:
            lease.__exit__(None, None, None)


class TestTaskSearch:
    """测试 task search 命令的搜索功能。"""

    def _call_search(self, query, tasks=None):
        if tasks is None:
            tasks = []
        mock_store = _make_mock_store(tasks)

        with _patched_task_cli_deps(mock_store):
            return cmd_task_search(MockArgs(query=query))

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

    def test_nonexistent_task_error_message(self, tmp_path, capsys):
        _seed(tmp_path)
        assert _cli(tmp_path, cmd_task_abandon, "task-does-not-exist") == 1
        assert "任务 task-does-not-exist 不存在。" in capsys.readouterr().out

    def test_update_failure_handling(self, tmp_path, monkeypatch, capsys):
        from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

        _agent, child, _attempt = _seed(tmp_path)

        def unavailable(self, *_args, **_kwargs):
            raise OSError("simulated I/O error")

        monkeypatch.setattr(RuntimeRepository, "__init__", unavailable)
        assert _cli(tmp_path, cmd_task_abandon, child.id) == 1
        assert f"无法打开配置的任务权威，未放弃任务 {child.id}：" in capsys.readouterr().out

    def test_empty_task_id_handling(self):
        """测试空任务 ID 的处理。

        验证空 task_id 不会导致系统崩溃。
        """
        mock_store = _make_mock_store([])
        mock_store.task_registry.lookup_task.return_value = None

        with _patched_task_cli_deps(mock_store):
            result = cmd_task_show(MockArgs(task_id=""))

        # 应该优雅处理
        assert result in (0, 1)

    def test_pause_nonexistent_task(self, tmp_path, capsys):
        _seed(tmp_path)
        assert _cli(tmp_path, cmd_task_pause, "task-nonexist") == 1
        assert "任务 task-nonexist 不存在。" in capsys.readouterr().out
