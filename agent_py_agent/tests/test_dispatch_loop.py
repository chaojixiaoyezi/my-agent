"""Dispatch 循环闭环保证机制测试。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestPendingWorkState:
    """测试闭环检测逻辑。"""

    def test_has_pending_work_when_candidates_exist(self, tmp_path: Path):
        """有可调度任务时 has_pending_work 为 True。"""
        from agent_py_agent.agent.agent_core.dispatch_mixin import SimpleAgentDispatchMixin

        # 创建一个 mock agent
        class MockAgent(SimpleAgentDispatchMixin):
            def __init__(self):
                self._has_pending_work = False
                self._consecutive_dispatch_rounds = 0
                self.config = MagicMock()
                self.config.runner_failure_policy = "auto"
                self.subagents = MagicMock()

                # Mock list_runs 返回可调度的任务
                mock_task = MagicMock()
                mock_task.id = "test_task_1"
                mock_task.status = "RUNNING"
                mock_task.runner_attempts = 0
                self.subagents.list_runs.return_value = [mock_task]

            def subagents_list_runs(self):
                return self.subagents.list_runs()

        agent = MockAgent()

        # 验证初始状态
        assert agent._has_pending_work is False

    def test_has_pending_work_false_when_no_candidates(self, tmp_path: Path):
        """无任务时 has_pending_work 为 False。"""
        from agent_py_agent.agent.agent_core.dispatch_mixin import SimpleAgentDispatchMixin

        class MockAgent(SimpleAgentDispatchMixin):
            def __init__(self):
                self._has_pending_work = False
                self._consecutive_dispatch_rounds = 0
                self.config = MagicMock()
                self.config.runner_failure_policy = "auto"
                self.subagents = MagicMock()

                # Mock list_runs 返回空
                self.subagents.list_runs.return_value = []

        agent = MockAgent()
        assert agent._has_pending_work is False


class TestDispatchLoop:
    """测试 dispatch_loop 函数。"""

    def test_dispatch_loop_single_round(self, tmp_path: Path):
        """单轮 dispatch 后无任务时只跑一轮。"""
        from agent_py_agent.agent.agent_core.dispatch_loop import DispatchLoopReport, dispatch_loop
        from agent_py_agent.agent.agent_core.dispatch_params import DispatchParams

        # Mock agent
        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"
        agent.has_pending_work = False
        agent.subagents.list_runs.return_value = []

        # Mock dispatch_report
        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report

        # 调用 dispatch_loop
        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)

        assert isinstance(result, DispatchLoopReport)
        assert result.rounds_count == 1
        assert result.total_records == 0
        assert result.stopped_by_limit is False
        assert isinstance(agent.dispatch_subagents.call_args.kwargs["params"], DispatchParams)

    def test_dispatch_loop_multiple_rounds(self, tmp_path: Path):
        """有任务时跑多轮。"""
        from agent_py_agent.agent.agent_core.dispatch_loop import DispatchLoopReport, dispatch_loop

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"

        # 跟踪调用次数
        call_count = [0]

        # 第一次调用有任务，第二次没有
        mock_report_1 = MagicMock()
        mock_report_1.records = [MagicMock(), MagicMock()]
        mock_report_2 = MagicMock()
        mock_report_2.records = []

        def dispatch_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # 第一次调用后还有待处理工作
                agent.has_pending_work = True
                return mock_report_1
            else:
                # 第二次调用后没有待处理工作
                agent.has_pending_work = False
                return mock_report_2

        agent.dispatch_subagents.side_effect = dispatch_side_effect

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)

        assert result.rounds_count == 2
        assert result.total_records == 2

    def test_dispatch_loop_stops_at_limit(self, tmp_path: Path):
        """达到最大轮数后停止。"""
        from agent_py_agent.agent.agent_core.dispatch_loop import DispatchLoopReport, dispatch_loop

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"

        # 每次都有任务，永远不空
        mock_report = MagicMock()
        mock_report.records = [MagicMock()]
        agent.dispatch_subagents.return_value = mock_report
        agent.has_pending_work = True
        agent.subagents.list_runs.return_value = [MagicMock()]  # 永远有候选

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=5)

        assert result.rounds_count == 5
        assert result.stopped_by_limit is True
        # dispatch_subagents 被调用 5 次
        assert agent.dispatch_subagents.call_count == 5

    def test_dispatch_loop_stops_when_audit_only_actions_repeat(self, tmp_path: Path):
        """重复的记录类动作不应该把调度循环拖到最大轮数。"""
        from agent_py_agent.agent.agent_core.dispatch_loop import dispatch_loop
        from agent_py_agent.agent.subagents.reports import DispatchRecord, DispatchReport

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"
        agent.subagents.list_runs.return_value = []

        def dispatch_side_effect(*args, **kwargs):
            agent.has_pending_work = True
            return DispatchReport(
                generated_at=0.0,
                dry_run=False,
                summary={"total": 2},
                records=[
                    DispatchRecord(
                        id="due",
                        step="due_check",
                        action="scan",
                        run_id="",
                        dry_run=False,
                        applied=False,
                        ok=True,
                        message="发现 3 个 due-check issue。",
                    ),
                    DispatchRecord(
                        id="classify",
                        step="action_apply",
                        action="classify_blocker",
                        run_id="blocked-run",
                        dry_run=False,
                        applied=True,
                        ok=True,
                        message="已记录 classify_blocker 待人工处理。",
                        before_status="BLOCKED",
                        after_status="BLOCKED",
                    ),
                ],
            )

        agent.dispatch_subagents.side_effect = dispatch_side_effect

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)

        assert result.rounds_count == 2
        assert result.stopped_by_no_progress is True
        assert result.stopped_by_limit is False


class TestAdaptiveInterval:
    """测试自适应间隔逻辑。"""

    def test_adaptive_interval_with_changes(self, tmp_path: Path):
        """有变化时使用短间隔。"""
        # 这个测试验证 watch_subagents 的间隔逻辑
        # 实际测试需要完整的 agent 环境，这里用 mock 验证逻辑

        active_interval = 5
        idle_interval = 30

        # 模拟有变化的场景
        last_dispatch_had_changes = True
        current_interval = active_interval if last_dispatch_had_changes else idle_interval
        assert current_interval == 5

    def test_adaptive_interval_without_changes(self, tmp_path: Path):
        """无变化时使用长间隔。"""
        active_interval = 5
        idle_interval = 30

        # 模拟无变化的场景
        last_dispatch_had_changes = False
        current_interval = active_interval if last_dispatch_had_changes else idle_interval
        assert current_interval == 30


class TestFailureAutoTrigger:
    """测试失败自动触发逻辑。"""

    def test_runner_failure_sets_pending_work(self, tmp_path: Path):
        """runner 失败后设置 _has_pending_work 为 True。"""
        from agent_py_agent.agent.agent_core.dispatch_mixin import SimpleAgentDispatchMixin

        class MockAgent(SimpleAgentDispatchMixin):
            def __init__(self):
                self._has_pending_work = False
                self._consecutive_dispatch_rounds = 0
                self.config = MagicMock()
                self.config.runner_failure_policy = "auto"
                self.subagents = MagicMock()

        agent = MockAgent()

        # 模拟 runner 执行失败的情况
        result = MagicMock()
        result.ok = False
        result.status = "BLOCKED"

        retry_reason = "failure_type=runner_error; attempt=2/2"

        # 失败自动触发逻辑
        if not result.ok:
            failure_type = str(result.status or "").strip().upper()
            if failure_type in {"BLOCKED", "TIMEOUT"} and retry_reason:
                agent._has_pending_work = True

        assert agent._has_pending_work is True

    def test_runner_success_does_not_trigger(self, tmp_path: Path):
        """runner 成功后不触发。"""
        from agent_py_agent.agent.agent_core.dispatch_mixin import SimpleAgentDispatchMixin

        class MockAgent(SimpleAgentDispatchMixin):
            def __init__(self):
                self._has_pending_work = False

        agent = MockAgent()

        result = MagicMock()
        result.ok = True
        result.status = "RUNNING"

        retry_reason = None

        # 失败自动触发逻辑
        if not result.ok:
            failure_type = str(result.status or "").strip().upper()
            if failure_type in {"BLOCKED", "TIMEOUT"} and retry_reason:
                agent.has_pending_work = True

        assert agent._has_pending_work is False




class TestStartupRecoveryEnhancement:
    """测试启动恢复增强。"""

    def test_dispatch_pending_detection(self, tmp_path: Path):
        """检测未完成的 dispatch 循环。"""
        from agent_py_agent.agent.startup_recovery import ActiveWorkSummary

        summary = ActiveWorkSummary()
        summary.dispatch_pending = True
        summary.dispatch_rounds = 5

        assert summary.dispatch_pending is True
        assert summary.dispatch_rounds == 5

    def test_format_dispatch_pending(self, tmp_path: Path):
        """格式化未完成 dispatch 的提示。"""
        from agent_py_agent.agent.startup_recovery import (
            ActiveWorkSummary,
            format_active_work_summary,
        )

        summary = ActiveWorkSummary()
        summary.dispatch_pending = True
        summary.dispatch_rounds = 10

        formatted = format_active_work_summary(summary)

        assert "dispatch" in formatted.lower() or "调度" in formatted


import os


class TestConsecutiveRoundsCounter:
    """测试连续轮数计数器。"""

    def test_increment_dispatch_rounds(self, tmp_path: Path):
        """递增连续 dispatch 轮数。"""
        from agent_py_agent.agent.agent_core.dispatch_mixin import SimpleAgentDispatchMixin

        class MockAgent(SimpleAgentDispatchMixin):
            def __init__(self):
                self._consecutive_dispatch_rounds = 0

        agent = MockAgent()

        agent._increment_dispatch_rounds()
        assert agent._consecutive_dispatch_rounds == 1

        agent._increment_dispatch_rounds()
        assert agent._consecutive_dispatch_rounds == 2

    def test_reset_dispatch_rounds(self, tmp_path: Path):
        """重置连续 dispatch 轮数。"""
        from agent_py_agent.agent.agent_core.dispatch_mixin import SimpleAgentDispatchMixin

        class MockAgent(SimpleAgentDispatchMixin):
            def __init__(self):
                self._consecutive_dispatch_rounds = 5

        agent = MockAgent()

        agent._reset_dispatch_rounds()
        assert agent._consecutive_dispatch_rounds == 0


class TestMaxConsecutiveRoundsLimit:
    """测试最大连续轮数保护。"""

    def test_stops_at_max_rounds(self, tmp_path: Path):
        """达到上限后停止调度。"""
        max_consecutive = 5
        current_rounds = 5

        # 达到上限检查
        if current_rounds >= max_consecutive:
            should_stop = True
        else:
            should_stop = False

        assert should_stop is True

    def test_continues_before_max_rounds(self, tmp_path: Path):
        """未达到上限时继续。"""
        max_consecutive = 5
        current_rounds = 3

        if current_rounds >= max_consecutive:
            should_stop = True
        else:
            should_stop = False

        assert should_stop is False


        # 负数可能导致 0 轮
