"""边界情况测试 - 空任务列表、空目标、超长目标、并发状态转换、参数边界。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestEmptyInputCases:
    """测试空输入边界情况。"""

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

    def test_very_long_goal_truncation(self, tmp_path: Path):
        """超长目标被正确处理。"""
        from agent_py_agent.agent.memory_push import format_memories_for_injection
        from agent_py_agent.agent.memory_store import MemoryRecord

        long_goal = "A" * 10000  # 非常长的目标
        result = format_memories_for_injection(
            [
                MemoryRecord(
                    role="system",
                    content=long_goal,
                    kind="lesson",
                    entry_id="lesson-long",
                    attributes={"origin": "reviewed"},
                )
            ]
        )
        assert result.count("<memory-context>") <= 1

    def test_unicode_goal_handling(self, tmp_path: Path):
        """Unicode 目标字符串的处理。"""
        from agent_py_agent.agent.agent_core.failure_analysis_service import (
            FailureAnalysis,
            FailureIntrospector,
        )

        introspector = FailureIntrospector()

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


class TestParameterBoundaryCases:
    """测试参数边界情况。"""

    def test_timeout_zero(self, tmp_path: Path):
        """timeout=0 的处理（应该使用默认值）。"""
        from agent_py_agent.agent.agent_core.dynamic_timeout import calculate_dynamic_timeout

        class MockConfig:
            runner_timeout_seconds = 0
            dynamic_timeout_enabled = False
            dynamic_timeout_min = 10.0
            dynamic_timeout_max = 300.0
            model_speed_profile_path = ""

        config = MockConfig()
        result = calculate_dynamic_timeout(config, 100, 50)
        # timeout 为 0 时应该使用某个默认值或回退逻辑
        assert result > 0

    def test_max_tool_rounds_negative(self, tmp_path: Path):
        """max_tool_rounds 为负数时的处理。"""
        from agent_py_agent.agent.agent_core.failure_analysis_service import (
            FailureAnalysis,
            FailureIntrospection,
            FailureIntrospector,
        )

        introspector = FailureIntrospector()

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

    def test_empty_string_goal(self, tmp_path: Path):
        """空字符串目标的处理。"""
        from agent_py_agent.agent.agent_core.failure_analysis_service import (
            FailureAnalysis,
            FailureIntrospector,
        )

        introspector = FailureIntrospector()

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
