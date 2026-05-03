from __future__ import annotations

import logging

import pytest

from agent_py_agent.agent.agent_core.automation_guard import SubagentAutomationGuard
from agent_py_agent.agent.agent_core.task_complexity import TaskComplexityEstimate


class MockConfig:
    def __init__(self, automation_level: int = 2):
        self.subagent_automation_level = automation_level


class TestSubagentAutomationGuard:
    def test_level_1_threshold(self):
        guard = SubagentAutomationGuard(MockConfig(1))
        assert guard.level == 1
        assert guard.threshold == 2

    def test_level_2_threshold(self):
        guard = SubagentAutomationGuard(MockConfig(2))
        assert guard.level == 2
        assert guard.threshold == 4

    def test_level_3_threshold(self):
        guard = SubagentAutomationGuard(MockConfig(3))
        assert guard.level == 3
        assert guard.threshold == 8

    def test_unknown_level_defaults_to_8(self):
        guard = SubagentAutomationGuard(MockConfig(99))
        assert guard.threshold == 8


class TestShouldDelegate:
    def test_below_threshold_not_delegated(self):
        guard = SubagentAutomationGuard(MockConfig(2))
        complexity = TaskComplexityEstimate(
            estimated_rounds=3,
            estimated_input_tokens=6000,
            estimated_output_tokens=1500,
            confidence=0.6,
            factors={},
        )
        assert not guard.should_delegate(complexity)

    def test_at_threshold_delegated(self):
        guard = SubagentAutomationGuard(MockConfig(2))
        complexity = TaskComplexityEstimate(
            estimated_rounds=4,
            estimated_input_tokens=8000,
            estimated_output_tokens=2000,
            confidence=0.6,
            factors={},
        )
        assert guard.should_delegate(complexity)

    def test_above_threshold_delegated(self):
        guard = SubagentAutomationGuard(MockConfig(2))
        complexity = TaskComplexityEstimate(
            estimated_rounds=10,
            estimated_input_tokens=20000,
            estimated_output_tokens=5000,
            confidence=0.6,
            factors={},
        )
        assert guard.should_delegate(complexity)


class TestWarnIfNotDelegating:
    def test_no_warning_when_not_should_delegate(self, caplog):
        guard = SubagentAutomationGuard(MockConfig(2))
        complexity = TaskComplexityEstimate(
            estimated_rounds=2,
            estimated_input_tokens=4000,
            estimated_output_tokens=1000,
            confidence=0.6,
            factors={},
        )
        with caplog.at_level(logging.WARNING):
            guard.warn_if_not_delegating(complexity, delegated=False)
        assert len(caplog.records) == 0

    def test_no_warning_when_should_delegate_and_delegated(self, caplog):
        guard = SubagentAutomationGuard(MockConfig(2))
        complexity = TaskComplexityEstimate(
            estimated_rounds=10,
            estimated_input_tokens=20000,
            estimated_output_tokens=5000,
            confidence=0.6,
            factors={},
        )
        with caplog.at_level(logging.WARNING):
            guard.warn_if_not_delegating(complexity, delegated=True)
        assert len(caplog.records) == 0

    def test_warning_when_should_delegate_but_not_delegated(self, caplog):
        guard = SubagentAutomationGuard(MockConfig(2))
        complexity = TaskComplexityEstimate(
            estimated_rounds=10,
            estimated_input_tokens=20000,
            estimated_output_tokens=5000,
            confidence=0.6,
            factors={},
        )
        with caplog.at_level(logging.WARNING):
            guard.warn_if_not_delegating(complexity, delegated=False)
        assert len(caplog.records) == 1
        assert "自动化级别 2" in caplog.text
        assert "10" in caplog.text
        assert "4" in caplog.text


class TestIntegration:
    def test_level_1_delegates_low_complexity(self):
        guard = SubagentAutomationGuard(MockConfig(1))
        complexity = TaskComplexityEstimate(
            estimated_rounds=2,
            estimated_input_tokens=4000,
            estimated_output_tokens=1000,
            confidence=0.6,
            factors={},
        )
        assert guard.should_delegate(complexity)

    def test_level_3_requires_high_complexity(self):
        guard = SubagentAutomationGuard(MockConfig(3))
        complexity = TaskComplexityEstimate(
            estimated_rounds=5,
            estimated_input_tokens=10000,
            estimated_output_tokens=2500,
            confidence=0.6,
            factors={},
        )
        assert not guard.should_delegate(complexity)
