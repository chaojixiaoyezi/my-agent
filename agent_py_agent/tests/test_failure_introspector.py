from __future__ import annotations

"""LLM: tests for failure introspector.

给人看的解释：
测试 LLM 失败自省器的各种场景：调用成功、JSON解析、参数提取、降级逻辑。
"""

from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.agent_core.failure_analyzer import FailureAnalysis
from agent_py_agent.agent.agent_core.failure_introspector import (
    FailureIntrospection,
    FailureIntrospector,
)
from agent_py_agent.agent.subagents.models import SubAgentRunnerResult, SubAgentTask


class TestFailureIntrospection:
    """测试 FailureIntrospection 数据类。"""

    def test_default_values(self) -> None:
        """测试默认值。"""
        introspection = FailureIntrospection()
        assert introspection.analysis_reason == ""
        assert introspection.root_cause == ""
        assert introspection.suggested_params == {}
        assert introspection.should_retry is True
        assert introspection.should_split is False
        assert introspection.confidence == 0.5

    def test_full_init(self) -> None:
        """测试完整初始化。"""
        introspection = FailureIntrospection(
            analysis_reason="任务太大",
            root_cause="task_too_large",
            suggested_params={"new_timeout_seconds": 300},
            should_retry=False,
            should_split=True,
            confidence=0.85,
        )
        assert introspection.analysis_reason == "任务太大"
        assert introspection.root_cause == "task_too_large"
        assert introspection.suggested_params == {"new_timeout_seconds": 300}
        assert introspection.should_retry is False
        assert introspection.should_split is True
        assert introspection.confidence == 0.85


class TestFailureIntrospector:
    """测试 FailureIntrospector 类。"""

    @pytest.fixture
    def mock_agent(self) -> MagicMock:
        """创建模拟 agent。"""
        agent = MagicMock()
        agent.run.return_value = MagicMock(response='{"analysis_reason": "超时", "root_cause": "timeout", "suggested_params": {"new_timeout_seconds": 200}, "should_retry": true, "should_split": false, "confidence": 0.8}')
        return agent

    @pytest.fixture
    def sample_task(self) -> SubAgentTask:
        """创建示例任务。"""
        return SubAgentTask(
            id="test-task-1",
            goal="测试任务",
            thought="测试思考",
            plan=["步骤1", "步骤2"],
        )

    @pytest.fixture
    def sample_result(self) -> SubAgentRunnerResult:
        """创建示例 runner 结果。"""
        return SubAgentRunnerResult(
            run_id="test-task-1",
            dry_run=False,
            ok=False,
            status="BLOCKED",
            verification_status="UNVERIFIED",
            message="超时",
        )

    @pytest.fixture
    def sample_analysis(self) -> FailureAnalysis:
        """创建示例失败分析。"""
        return FailureAnalysis(
            failure_type="runner_timeout",
            root_cause="timeout",
            suggested_action="increase_timeout_and_retry",
            should_retry=True,
            should_split=False,
        )

    def test_introspect_with_llm_success(self, mock_agent: MagicMock, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult, sample_analysis: FailureAnalysis) -> None:
        """测试 LLM 调用成功。"""
        introspector = FailureIntrospector(agent=mock_agent)
        result = introspector.introspect(sample_task, sample_result, sample_analysis)

        assert result.analysis_reason == "超时"
        assert result.root_cause == "timeout"
        assert result.suggested_params == {"new_timeout_seconds": 200}
        assert result.should_retry is True
        assert result.confidence == 0.8
        mock_agent.run.assert_called_once()

    def test_introspect_with_llm_json_parse_error(self, mock_agent: MagicMock, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult, sample_analysis: FailureAnalysis) -> None:
        """测试 LLM 返回非 JSON 格式时降级。"""
        mock_agent.run.return_value = MagicMock(response="这不是 JSON")
        introspector = FailureIntrospector(agent=mock_agent)
        result = introspector.introspect(sample_task, sample_result, sample_analysis)

        # 降级到规则分类
        assert result.confidence == 0.3
        assert result.should_retry == sample_analysis.should_retry

    def test_introspect_with_llm_missing_fields(self, mock_agent: MagicMock, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult, sample_analysis: FailureAnalysis) -> None:
        """测试 LLM 返回缺少字段的 JSON。"""
        mock_agent.run.return_value = MagicMock(response='{"analysis_reason": "失败"}')
        introspector = FailureIntrospector(agent=mock_agent)
        result = introspector.introspect(sample_task, sample_result, sample_analysis)

        # 缺少字段时使用规则分类的值
        assert result.root_cause == sample_analysis.root_cause
        assert result.should_retry == sample_analysis.should_retry

    def test_introspect_without_agent_fallback(self, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult, sample_analysis: FailureAnalysis) -> None:
        """测试未设置 agent 时降级。"""
        introspector = FailureIntrospector(agent=None)
        result = introspector.introspect(sample_task, sample_result, sample_analysis)

        assert result.confidence == 0.3
        assert result.analysis_reason.startswith("规则分类")

    def test_introspect_with_exception(self, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult, sample_analysis: FailureAnalysis) -> None:
        """测试 LLM 调用抛出异常时降级。"""
        mock_agent = MagicMock()
        mock_agent.run.side_effect = RuntimeError("LLM 调用失败")
        introspector = FailureIntrospector(agent=mock_agent)
        result = introspector.introspect(sample_task, sample_result, sample_analysis)

        assert result.confidence == 0.3
        assert result.analysis_reason.startswith("规则分类")

    def test_suggest_params_from_analysis(self, sample_analysis: FailureAnalysis) -> None:
        """测试从规则分析生成建议参数。"""
        sample_analysis.should_adjust_timeout = True
        sample_analysis.new_timeout_seconds = 300.0
        sample_analysis.should_split = True
        sample_analysis.split_suggestions = ["步骤1", "步骤2"]

        introspector = FailureIntrospector()
        params = introspector._suggest_params_from_analysis(sample_analysis)

        assert "new_timeout_seconds" in params
        assert "split_suggestions" in params

    def test_get_current_timeout_with_attribute(self, sample_task: SubAgentTask) -> None:
        """测试从任务属性获取超时。"""
        sample_task.attributes["dynamic_timeout_seconds"] = 200.0
        introspector = FailureIntrospector()
        timeout = introspector._get_current_timeout(sample_task)
        assert timeout == 200.0

    def test_get_current_timeout_default(self, sample_task: SubAgentTask) -> None:
        """测试默认超时。"""
        introspector = FailureIntrospector()
        timeout = introspector._get_current_timeout(sample_task)
        assert timeout == 120.0

    def test_fallback_to_rules_complete(self, sample_analysis: FailureAnalysis) -> None:
        """测试完整规则分类降级。"""
        sample_analysis.should_retry = False
        sample_analysis.should_split = True
        sample_analysis.root_cause = "task_too_large"

        introspector = FailureIntrospector()
        result = introspector._fallback_to_rules(sample_analysis)

        assert result.should_retry is False
        assert result.should_split is True
        assert result.root_cause == "task_too_large"
        assert result.confidence == 0.3

    def test_set_agent(self) -> None:
        """测试设置 agent。"""
        introspector = FailureIntrospector()
        assert introspector._agent is None

        mock_agent = MagicMock()
        introspector.set_agent(mock_agent)
        assert introspector._agent is mock_agent