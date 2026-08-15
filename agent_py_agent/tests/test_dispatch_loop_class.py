"""Dispatch循环测试 - dispatch_loop 循环控制、退出条件。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestDispatchLoopClass:
    """测试 dispatch_loop 函数（独立函数，非类方法）。"""

    def test_dispatch_loop_single_round(self, tmp_path: Path):
        """无待处理任务时只执行一轮。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"
        agent.has_pending_work = False
        agent.subagents.list_runs.return_value = []

        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)

        assert isinstance(result, DispatchLoopReport)
        assert result.rounds_count == 1
        assert result.total_records == 0
        assert result.stopped_by_limit is False

    def test_dispatch_loop_multiple_rounds(self, tmp_path: Path):
        """多轮调度直到任务完成。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"

        call_count = [0]

        def dispatch_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 3:
                agent.has_pending_work = False
            else:
                agent.has_pending_work = True

            mock_report = MagicMock()
            mock_report.records = [MagicMock()]
            return mock_report

        agent.dispatch_subagents.side_effect = dispatch_side_effect
        agent.subagents.list_runs.return_value = []

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)

        assert result.rounds_count == 3

    def test_dispatch_loop_stops_at_limit(self, tmp_path: Path):
        """达到最大轮数时停止。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"

        mock_report = MagicMock()
        mock_report.records = [MagicMock()]
        agent.dispatch_subagents.return_value = mock_report
        agent.has_pending_work = True
        agent.subagents.list_runs.return_value = [MagicMock()]

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=5)

        assert result.rounds_count == 5
        assert result.stopped_by_limit is True
        assert agent.dispatch_subagents.call_count == 5

    def test_dispatch_loop_empty_candidates(self, tmp_path: Path):
        """无候选任务时结束。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"
        agent.subagents.list_runs.return_value = []
        agent.has_pending_work = False

        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)

        assert result.rounds_count == 1
        assert result.final_pending_count == 0

    def test_final_pending_count_reports_load_error(self, tmp_path: Path):
        """最终 pending 统计不能把账本读取失败伪装成没有待跑任务。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import dispatch_loop

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"
        agent.config.same_run_redispatch_limit = 0
        agent.has_pending_work = False
        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report
        agent.subagents.list_runs.side_effect = ValueError("bad subagent state")

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)

        assert result.final_pending_count == 0
        assert result.final_pending_load_error["context"] == "dispatch_loop.final_pending_runner_count"
        assert result.final_pending_load_error["error"]["message"]


class TestDispatchLoopClassRecords:
    """测试 dispatch_loop 的记录和报告结构。"""

    def test_dispatch_loop_records_count(self, tmp_path: Path):
        """正确统计 records 总数。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"

        # 第一轮有2条记录，然后 _has_pending_work 变为 False，停止
        mock_report1 = MagicMock()
        mock_report1.records = [MagicMock(), MagicMock()]

        agent.dispatch_subagents.return_value = mock_report1
        agent.subagents.list_runs.return_value = []

        # 手动控制 has_pending_work 的变化
        call_count = [0]
        original_dispatch = agent.dispatch_subagents
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 2:
                agent.has_pending_work = False
            return mock_report1
        agent.dispatch_subagents.side_effect = side_effect

        # 设置初始状态
        agent.has_pending_work = True

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)

        # 总记录数应该 >= 2（至少一轮）
        assert result.total_records >= 2

    def test_dispatch_loop_report_structure(self, tmp_path: Path):
        """验证报告结构。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"
        agent.has_pending_work = False

        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report
        agent.subagents.list_runs.return_value = []

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)

        assert hasattr(result, "rounds_count")
        assert hasattr(result, "total_records")
        assert hasattr(result, "final_pending_count")
        assert hasattr(result, "stopped_by_limit")
        assert hasattr(result, "stopped_by_no_progress")
        assert hasattr(result, "rounds")

    def test_dispatch_loop_rounds_list(self, tmp_path: Path):
        """每轮记录被正确保存。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"

        mock_report = MagicMock()
        mock_report.records = [MagicMock()]

        agent.dispatch_subagents.return_value = mock_report
        agent.subagents.list_runs.return_value = []

        # 初始 has_pending_work = True，但 dispatch 后变为 False
        agent.has_pending_work = True

        def side_effect(*args, **kwargs):
            agent.has_pending_work = False
            return mock_report
        agent.dispatch_subagents.side_effect = side_effect

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=20)

        # 应该至少执行了一轮
        assert result.rounds_count >= 1
        assert len(result.rounds) >= 1

    def test_dispatch_loop_zero_max_rounds(self, tmp_path: Path):
        """max_rounds=0 时的处理。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import (
            DispatchLoopReport,
            dispatch_loop,
        )

        agent = MagicMock()
        agent.config.runner_failure_policy = "auto"
        agent.has_pending_work = False

        mock_report = MagicMock()
        mock_report.records = []
        agent.dispatch_subagents.return_value = mock_report
        agent.subagents.list_runs.return_value = []

        result = dispatch_loop(agent, router=None, max_consecutive_rounds=0)

        assert isinstance(result, DispatchLoopReport)


class TestDispatchLoopReport:
    """测试 DispatchLoopReport 数据类。"""

    def test_default_values(self, tmp_path: Path):
        """默认值的测试。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import DispatchLoopReport

        report = DispatchLoopReport()
        assert report.rounds_count == 0
        assert report.total_records == 0
        assert report.final_pending_count == 0
        assert report.stopped_by_limit is False
        assert report.stopped_by_no_progress is False
        assert report.rounds == []

    def test_round_info_structure(self, tmp_path: Path):
        """每轮信息结构。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.loop import DispatchLoopReport

        report = DispatchLoopReport()
        report.rounds.append({"round": 1, "record_count": 5, "ok": True})

        assert report.rounds[0]["round"] == 1
        assert report.rounds[0]["record_count"] == 5
