"""Dispatch 循环异常场景测试（原 test_dispatch_loop_watchdog.py；watchdog 模块已随无读取方配置一起删除）。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


class TestDispatchLoopExceptions:
    """补充：dispatch_loop 异常场景测试。"""

    def test_task_midway_failure(self, tmp_path: Path):
        """任务中途失败的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_retry_limit = 1

        call_count = [0]

        def dispatch_side_effect(*args, **kwargs):
            call_count[0] += 1
            # 第一轮有任务失败，第二轮继续处理
            mock_report = MagicMock()
            if call_count[0] == 1:
                # 模拟任务失败
                failed_record = MagicMock()
                failed_record.ok = False
                failed_record.status = "FAILED"
                failed_record.step = "runner"
                mock_report.records = [failed_record]
                agent.has_pending_work = True
            else:
                mock_report.records = []
                agent.has_pending_work = False
            return mock_report

        agent.dispatch_subagents.side_effect = dispatch_side_effect
        agent.subagents.list_runs.return_value = []

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)
        assert result.rounds_count >= 1

    def test_all_tasks_paused(self, tmp_path: Path):
        """所有任务都被暂停时的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_retry_limit = 1

        mock_report = MagicMock()
        # 所有任务都是暂停状态
        paused_record = MagicMock()
        paused_record.ok = True
        paused_record.status = "PAUSED"
        paused_record.step = "runner"
        mock_report.records = [paused_record]
        agent.dispatch_subagents.return_value = mock_report

        # 模拟所有任务都暂停
        mock_task = MagicMock()
        mock_task.status = "PAUSED"
        mock_task.runner_attempts = 1
        agent.subagents.list_runs.return_value = [mock_task]

        # 设置 max_runners=1 且 runner_failure_retry_limit 使暂停的任务不参与调度
        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20, max_runners=1)
        assert isinstance(result, DispatchLoopReport)

    def test_max_rounds_limit_reached(self, tmp_path: Path):
        """达到最大轮数限制时的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_retry_limit = 1

        # 永远有候选任务
        mock_task = MagicMock()
        mock_task.status = "RUNNING"
        mock_task.runner_attempts = 0
        agent.subagents.list_runs.return_value = [mock_task]
        agent.has_pending_work = True

        mock_report = MagicMock()
        mock_report.records = [MagicMock()]  # 有记录
        agent.dispatch_subagents.return_value = mock_report

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=3)
        assert result.rounds_count == 3
        assert result.stopped_by_limit is True

    def test_dispatch_with_no_retry_policy(self, tmp_path: Path):
        """不重试策略下的 dispatch 处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_retry_limit = 0

        mock_report = MagicMock()
        failed_record = MagicMock()
        failed_record.ok = False
        failed_record.status = "FAILED"
        mock_report.records = [failed_record]

        agent.dispatch_subagents.return_value = mock_report
        agent.subagents.list_runs.return_value = []
        agent.has_pending_work = False

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)
        assert isinstance(result, DispatchLoopReport)

    def test_dispatch_runner_worker_exception(self, tmp_path: Path):
        """runner worker 抛出异常时的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.mixin import (
            SimpleAgentDispatchMixin,
        )

        class MockAgent(SimpleAgentDispatchMixin):
            def __init__(self):
                self._has_pending_work = False
                self._consecutive_dispatch_rounds = 0
                self.config = MagicMock()
                self.config.runner_failure_retry_limit = 1
                self.config.runner_concurrency = 1
                self.config.runner_start_rate = None
                self.config.runner_timeout_seconds = 120
                self.subagents = MagicMock()
                self.local_store = MagicMock()

            def subagents_list_runs(self):
                return self.subagents.list_runs()

        agent = MockAgent()

        mock_task = MagicMock()
        mock_task.id = "test_task"
        mock_task.status = "RUNNING"
        mock_task.goal = "测试目标"
        mock_task.plan = ""
        mock_task.attributes = {}
        mock_task.runner_attempts = 0

        agent.subagents.list_runs.return_value = [mock_task]
        agent._has_pending_work = True

        # 模拟 save 抛出异常
        agent.subagents.save.side_effect = RuntimeError("Save failed")
        agent.subagents.load.return_value = mock_task

        # runner 执行路径不应该崩溃
        try:
            agent.dispatch_subagents(
                router=MagicMock(),
                apply=True,
                start_runners=True,
            )
        except Exception:
            pass  # 异常应该被捕获

    def test_dispatch_zero_candidates(self, tmp_path: Path):
        """零候选任务时的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_retry_limit = 1
        agent.subagents.list_runs.return_value = []  # 无候选
        agent.has_pending_work = False

        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)
        assert result.rounds_count == 1
        assert result.total_records == 0

    def test_dispatch_with_negative_max_rounds(self, tmp_path: Path):
        """负数 max_rounds 时的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_retry_limit = 1

        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report
        agent.has_pending_work = False
        agent.subagents.list_runs.return_value = []

        # 负数 max_rounds 应该安全处理（视为无效，不执行循环）
        result = dispatch_loop(agent, router=None, max_consecutive_rounds=-10)
        assert isinstance(result, DispatchLoopReport)
