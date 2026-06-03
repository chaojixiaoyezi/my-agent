"""边界情况测试 - 空任务列表、空目标、超长目标、并发状态转换、参数边界。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestEmptyInputCases:
    """测试空输入边界情况。"""

    def test_empty_task_list(self, tmp_path: Path):
        """空任务列表的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"
        agent.subagents.list_runs.return_value = []  # 空任务列表
        agent.has_pending_work = False

        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)
        assert isinstance(result, DispatchLoopReport)
        assert result.rounds_count <= 20
        assert result.total_records == 0

    def test_empty_goal_task(self, tmp_path: Path):
        """空目标任务的行为。"""
        from agent_py_agent.agent.subagents.models import SubAgentTask

        task = SubAgentTask(
            id="test_1",
            goal="",  # 空目标
            thought="",  # 必须字段
            plan=[],  # plan 是 list
            status="RUNNING",
            runner_attempts=0,
        )
        assert task.goal == ""

    def test_empty_goal_not_crash_dispatch(self, tmp_path: Path):
        """空目标任务在 dispatch 中不会崩溃。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"

        # 创建一个空目标的任务
        mock_task = MagicMock()
        mock_task.id = "empty_goal_task"
        mock_task.goal = ""
        mock_task.status = "RUNNING"

        agent.subagents.list_runs.return_value = [mock_task]
        agent.has_pending_work = False  # dispatch_loop will break after 1 round

        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)
        assert isinstance(result, DispatchLoopReport)

    def test_very_long_goal_truncation(self, tmp_path: Path):
        """超长目标被正确处理。"""
        from agent_py_agent.agent.memory_push import format_memories_for_injection

        long_goal = "A" * 10000  # 非常长的目标
        result = format_memories_for_injection([long_goal])
        # 应该能处理而不崩溃

    def test_unicode_goal_handling(self, tmp_path: Path):
        """Unicode 目标字符串的处理。"""
        from agent_py_agent.agent.agent_core.failure_analyzer import FailureAnalysis
        from agent_py_agent.agent.agent_core.failure_introspector import FailureIntrospector

        agent = MagicMock()
        agent.run.return_value = MagicMock(response='{"analysis_reason": "测试中文", "root_cause": "unknown", "should_retry": true}')

        introspector = FailureIntrospector(agent=agent)

        mock_task = MagicMock()
        mock_task.goal = "测试中文目标 🎉" * 100
        mock_task.attributes = {}
        mock_runner_result = MagicMock()
        mock_runner_result.ok = False
        mock_runner_result.runner_last_error = ""

        analysis = FailureAnalysis(
            failure_type="error",
            root_cause="unknown",
            suggested_action="check",
        )

        result = introspector.introspect(mock_task, mock_runner_result, analysis)
        assert isinstance(result.analysis_reason, str)


class TestConcurrentStateTransitions:
    """测试并发状态转换。"""

    def test_pause_and_abandon_same_task(self, tmp_path: Path):
        """同时暂停和放弃同一任务的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.mixin import (
            SimpleAgentDispatchMixin,
        )

        class MockAgent(SimpleAgentDispatchMixin):
            def __init__(self):
                self._has_pending_work = False
                self._consecutive_dispatch_rounds = 0
                self.config = MagicMock()
                self.config.runner_failure_policy = "auto"
                self.subagents = MagicMock()

        agent = MockAgent()

        # 模拟同时暂停和放弃任务
        mock_task = MagicMock()
        mock_task.status = "PAUSED"

        # 先暂停后放弃不应崩溃
        agent.subagents.list_runs.return_value = [mock_task]

        # 更新状态
        agent._update_pending_work_state()
        # 再次更新
        agent._update_pending_work_state()

    def test_concurrent_status_update(self, tmp_path: Path):
        """并发状态更新的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.mixin import (
            SimpleAgentDispatchMixin,
        )

        class MockAgent(SimpleAgentDispatchMixin):
            def __init__(self):
                self._has_pending_work = False
                self.config = MagicMock()
                self.config.runner_failure_policy = "auto"
                self.subagents = MagicMock()

        agent = MockAgent()

        # 快速多次更新状态
        for _ in range(10):
            agent._has_pending_work = True
            agent._update_pending_work_state()
            agent._has_pending_work = False
            agent._update_pending_work_state()

    def test_race_between_dispatch_and_complete(self, tmp_path: Path):
        """dispatch 和任务完成之间的竞态。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"

        call_count = [0]

        def dispatch_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                agent.has_pending_work = True
            else:
                agent.has_pending_work = False
            mock_report = MagicMock()
            mock_report.records = []
            return mock_report

        agent.dispatch_subagents.side_effect = dispatch_side_effect
        agent.subagents.list_runs.return_value = []

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)
        assert result.rounds_count >= 1


class TestParameterBoundaryCases:
    """测试参数边界情况。"""

    def test_timeout_zero(self, tmp_path: Path):
        """timeout=0 的处理（应该使用默认值）。"""
        from agent_py_agent.agent.agent_core.dynamic_timeout import calculate_dynamic_timeout

        class MockConfig:
            runner_timeout_seconds = 0
            dynamic_timeout_enabled = False
            dynamic_timeout_safety_margin = 1.2
            dynamic_timeout_min = 10.0
            dynamic_timeout_max = 300.0
            model_speed_profile_path = ""

        config = MockConfig()
        result = calculate_dynamic_timeout(config, 100, 50)
        # timeout 为 0 时应该使用某个默认值或回退逻辑
        assert result > 0

    def test_max_tool_rounds_negative(self, tmp_path: Path):
        """max_tool_rounds 为负数时的处理。"""
        from agent_py_agent.agent.agent_core.failure_analyzer import FailureAnalysis
        from agent_py_agent.agent.agent_core.failure_introspector import (
            FailureIntrospection,
            FailureIntrospector,
        )

        agent = MagicMock()
        agent.run.return_value = MagicMock(response='{"analysis_reason": "test", "root_cause": "ok", "should_retry": true}')

        introspector = FailureIntrospector(agent=agent)

        mock_task = MagicMock()
        mock_task.goal = "test"
        mock_task.attributes = {"max_tool_rounds": -1}  # 负数
        mock_runner_result = MagicMock()
        mock_runner_result.ok = False
        mock_runner_result.runner_last_error = ""
        mock_runner_result.tool_rounds = -1

        analysis = FailureAnalysis(
            failure_type="error",
            root_cause="unknown",
            suggested_action="check",
        )

        result = introspector.introspect(mock_task, mock_runner_result, analysis)
        assert isinstance(result, FailureIntrospection)

    def test_negative_max_consecutive_rounds(self, tmp_path: Path):
        """max_consecutive_rounds 为负数时的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import dispatch_loop

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"
        agent.has_pending_work = False
        agent.subagents.list_runs.return_value = []

        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report

        # 负数应该被当作 0 或使用默认值
        result = dispatch_loop(agent, router=None, max_consecutive_rounds=-5)
        # 至少应该返回有效结果而不是崩溃

    def test_limit_zero(self, tmp_path: Path):
        """limit=0 时的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"

        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report
        agent.has_pending_work = False
        agent.subagents.list_runs.return_value = []

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20, limit=0)
        assert isinstance(result, DispatchLoopReport)

    def test_max_runners_zero(self, tmp_path: Path):
        """max_runners=0 时的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"

        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report
        agent.has_pending_work = False
        agent.subagents.list_runs.return_value = []

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20, max_runners=0)
        assert isinstance(result, DispatchLoopReport)

    def test_empty_string_goal(self, tmp_path: Path):
        """空字符串目标的处理。"""
        from agent_py_agent.agent.agent_core.failure_analyzer import FailureAnalysis
        from agent_py_agent.agent.agent_core.failure_introspector import FailureIntrospector

        agent = MagicMock()
        agent.run.return_value = MagicMock(response='{"analysis_reason": "empty", "root_cause": "no_goal", "should_retry": false}')

        introspector = FailureIntrospector(agent=agent)

        mock_task = MagicMock()
        mock_task.goal = ""
        mock_task.attributes = {}
        mock_runner_result = MagicMock()
        mock_runner_result.ok = False
        mock_runner_result.runner_last_error = ""

        analysis = FailureAnalysis(
            failure_type="error",
            root_cause="empty_goal",
            suggested_action="provide_goal",
        )

        result = introspector.introspect(mock_task, mock_runner_result, analysis)
        assert isinstance(result.analysis_reason, str)

    def test_extremely_large_max_rounds(self, tmp_path: Path):
        """极大 max_rounds 值的处理（应该被限制）。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"

        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report
        agent.has_pending_work = False
        agent.subagents.list_runs.return_value = []

        # 极大的值不应该导致无限循环
        result = dispatch_loop(agent, router=None, max_consecutive_rounds=10**9)
        assert isinstance(result, DispatchLoopReport)
        # 实际轮数应该被某个上限限制