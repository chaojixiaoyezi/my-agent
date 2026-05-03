"""自动化防护测试 - automation_guard.py 自动化防护、风险控制。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestSubagentAutomationGuard:
    """测试 SubagentAutomationGuard 类。"""

    def test_guard_with_low_automation_level(self, tmp_path: Path):
        """低自动化级别时的阈值。"""
        from agent_py_agent.agent.agent_core.automation_guard import SubagentAutomationGuard
        from agent_py_agent.agent.agent_core.task_complexity import TaskComplexityEstimate

        class MockConfig:
            subagent_automation_level = 1

        config = MockConfig()
        guard = SubagentAutomationGuard(config)

        assert guard.level == 1
        assert guard.threshold == 2  # 级别1对应阈值2

    def test_guard_with_medium_automation_level(self, tmp_path: Path):
        """中等自动化级别时的阈值。"""
        from agent_py_agent.agent.agent_core.automation_guard import SubagentAutomationGuard

        class MockConfig:
            subagent_automation_level = 2

        config = MockConfig()
        guard = SubagentAutomationGuard(config)

        assert guard.threshold == 4

    def test_guard_with_high_automation_level(self, tmp_path: Path):
        """高自动化级别时的阈值。"""
        from agent_py_agent.agent.agent_core.automation_guard import SubagentAutomationGuard

        class MockConfig:
            subagent_automation_level = 3

        config = MockConfig()
        guard = SubagentAutomationGuard(config)

        assert guard.threshold == 8

    def test_guard_unknown_level_default(self, tmp_path: Path):
        """未知级别使用默认阈值。"""
        from agent_py_agent.agent.agent_core.automation_guard import SubagentAutomationGuard

        class MockConfig:
            subagent_automation_level = 99

        config = MockConfig()
        guard = SubagentAutomationGuard(config)

        assert guard.threshold == 8  # 默认值


class TestShouldDelegate:
    """测试 should_delegate 方法。"""

    def test_below_threshold_not_delegated(self, tmp_path: Path):
        """低于阈值时不委托。"""
        from agent_py_agent.agent.agent_core.automation_guard import SubagentAutomationGuard
        from agent_py_agent.agent.agent_core.task_complexity import TaskComplexityEstimate

        class MockConfig:
            subagent_automation_level = 2

        config = MockConfig()
        guard = SubagentAutomationGuard(config)

        complexity = TaskComplexityEstimate(
            estimated_rounds=2,  # 低于阈值4
            estimated_input_tokens=5000,
            estimated_output_tokens=1500,
            confidence=0.6,
            factors={},
        )

        result = guard.should_delegate(complexity)
        assert result is False

    def test_above_threshold_delegated(self, tmp_path: Path):
        """高于阈值时委托。"""
        from agent_py_agent.agent.agent_core.automation_guard import SubagentAutomationGuard
        from agent_py_agent.agent.agent_core.task_complexity import TaskComplexityEstimate

        class MockConfig:
            subagent_automation_level = 2

        config = MockConfig()
        guard = SubagentAutomationGuard(config)

        complexity = TaskComplexityEstimate(
            estimated_rounds=5,  # 高于阈值4
            estimated_input_tokens=5000,
            estimated_output_tokens=1500,
            confidence=0.6,
            factors={},
        )

        result = guard.should_delegate(complexity)
        assert result is True

    def test_equal_to_threshold_delegated(self, tmp_path: Path):
        """等于阈值时委托。"""
        from agent_py_agent.agent.agent_core.automation_guard import SubagentAutomationGuard
        from agent_py_agent.agent.agent_core.task_complexity import TaskComplexityEstimate

        class MockConfig:
            subagent_automation_level = 2

        config = MockConfig()
        guard = SubagentAutomationGuard(config)

        complexity = TaskComplexityEstimate(
            estimated_rounds=4,  # 等于阈值4
            estimated_input_tokens=5000,
            estimated_output_tokens=1500,
            confidence=0.6,
            factors={},
        )

        result = guard.should_delegate(complexity)
        assert result is True


class TestWarnIfNotDelegating:
    """测试 warn_if_not_delegating 方法。"""

    def test_warning_when_should_delegate_but_not(self, tmp_path: Path):
        """应该委托但没委托时发出警告。"""
        from agent_py_agent.agent.agent_core.automation_guard import SubagentAutomationGuard
        from agent_py_agent.agent.agent_core.task_complexity import TaskComplexityEstimate

        class MockConfig:
            subagent_automation_level = 2

        config = MockConfig()
        guard = SubagentAutomationGuard(config)

        complexity = TaskComplexityEstimate(
            estimated_rounds=5,  # 高于阈值
            estimated_input_tokens=5000,
            estimated_output_tokens=1500,
            confidence=0.6,
            factors={},
        )

        with patch("agent_py_agent.agent.agent_core.automation_guard.logger") as mock_logger:
            guard.warn_if_not_delegating(complexity, delegated=False)
            mock_logger.warning.assert_called()

    def test_no_warning_when_delegated(self, tmp_path: Path):
        """已委托时不发出警告。"""
        from agent_py_agent.agent.agent_core.automation_guard import SubagentAutomationGuard
        from agent_py_agent.agent.agent_core.task_complexity import TaskComplexityEstimate

        class MockConfig:
            subagent_automation_level = 2

        config = MockConfig()
        guard = SubagentAutomationGuard(config)

        complexity = TaskComplexityEstimate(
            estimated_rounds=5,
            estimated_input_tokens=5000,
            estimated_output_tokens=1500,
            confidence=0.6,
            factors={},
        )

        with patch("agent_py_agent.agent.agent_core.automation_guard.logger") as mock_logger:
            guard.warn_if_not_delegating(complexity, delegated=True)
            mock_logger.warning.assert_not_called()

    def test_no_warning_below_threshold(self, tmp_path: Path):
        """低于阈值时不发出警告。"""
        from agent_py_agent.agent.agent_core.automation_guard import SubagentAutomationGuard
        from agent_py_agent.agent.agent_core.task_complexity import TaskComplexityEstimate

        class MockConfig:
            subagent_automation_level = 2

        config = MockConfig()
        guard = SubagentAutomationGuard(config)

        complexity = TaskComplexityEstimate(
            estimated_rounds=2,  # 低于阈值
            estimated_input_tokens=5000,
            estimated_output_tokens=1500,
            confidence=0.6,
            factors={},
        )

        with patch("agent_py_agent.agent.agent_core.automation_guard.logger") as mock_logger:
            guard.warn_if_not_delegating(complexity, delegated=False)
            mock_logger.warning.assert_not_called()