from __future__ import annotations

"""LLM: tests for failure analyzer.

给人看的解释：
测试各种失败类型的分析结果。
"""

import pytest

from agent_py_agent.agent.agent_core.failure_analyzer import FailureAnalysis, SubAgentFailureAnalyzer
from agent_py_agent.agent.subagents.models import SubAgentTask, SubAgentRunnerResult


class TestSubAgentFailureAnalyzer:
    """测试失败分析器。"""

    @pytest.fixture
    def analyzer(self) -> SubAgentFailureAnalyzer:
        """创建分析器实例。"""
        return SubAgentFailureAnalyzer(max_timeout=600.0, max_retry_attempts=3)

    @pytest.fixture
    def sample_task(self) -> SubAgentTask:
        """创建示例任务。"""
        return SubAgentTask(
            id="test-task-1",
            goal="测试任务",
            thought="测试思考",
            plan=["步骤1", "步骤2", "步骤3"],
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
            message="测试失败",
        )

    def test_analyze_timeout_first_attempt(self, analyzer: SubAgentFailureAnalyzer, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult) -> None:
        """测试首次超时。"""
        sample_task.runner_attempts = 1
        sample_task.failure_type = "runner_timeout"

        analysis = analyzer.analyze(sample_task, sample_result)

        assert analysis.root_cause == "timeout"
        assert analysis.suggested_action == "increase_timeout_and_retry"
        assert analysis.should_retry is True
        assert analysis.should_split is False
        assert analysis.should_adjust_timeout is True
        assert analysis.new_timeout_seconds is not None
        assert analysis.new_timeout_seconds > 120  # 默认超时 120 秒

    def test_analyze_timeout_multiple_attempts(self, analyzer: SubAgentFailureAnalyzer, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult) -> None:
        """测试多次超时。"""
        sample_task.runner_attempts = 3
        sample_task.failure_type = "runner_timeout"

        analysis = analyzer.analyze(sample_task, sample_result)

        assert analysis.root_cause == "task_too_large"
        assert analysis.suggested_action == "split_task"
        assert analysis.should_retry is False
        assert analysis.should_split is True
        assert analysis.should_adjust_timeout is False
        assert analysis.new_timeout_seconds is None
        assert len(analysis.split_suggestions) > 0

    def test_analyze_capability_missing(self, analyzer: SubAgentFailureAnalyzer, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult) -> None:
        """测试能力缺失。"""
        sample_task.failure_type = "capability_request"

        analysis = analyzer.analyze(sample_task, sample_result)

        assert analysis.root_cause == "capability_missing"
        assert analysis.suggested_action == "manual_capability_grant"
        assert analysis.should_retry is False
        assert analysis.should_split is False

    def test_analyze_capability_insufficient_grant(self, analyzer: SubAgentFailureAnalyzer, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult) -> None:
        """测试授权不足。"""
        sample_task.failure_type = "capability_request"
        # 模拟已有授权
        from agent_py_agent.agent.subagents.models import CapabilityGrant

        sample_task.capability_grants = [
            CapabilityGrant(
                id="grant-1",
                request_id="req-1",
                grant_to_run_id=sample_task.id,
            )
        ]

        analysis = analyzer.analyze(sample_task, sample_result)

        assert analysis.root_cause == "insufficient_grant"
        assert analysis.suggested_action == "manual_review"
        assert analysis.should_retry is False

    def test_analyze_parse_error_retry(self, analyzer: SubAgentFailureAnalyzer, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult) -> None:
        """测试解析错误重试。"""
        sample_task.failure_type = "structured_output_parse_error"
        sample_task.runner_attempts = 1
        sample_result.structured_parse_error = "Missing required field"

        analysis = analyzer.analyze(sample_task, sample_result)

        assert analysis.root_cause == "parse_error"
        assert analysis.suggested_action == "retry_with_same_timeout"
        assert analysis.should_retry is True
        assert analysis.should_split is False

    def test_analyze_parse_error_persistent(self, analyzer: SubAgentFailureAnalyzer, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult) -> None:
        """测试持续解析错误。"""
        sample_task.failure_type = "structured_output_parse_error"
        sample_task.runner_attempts = 3
        sample_result.structured_parse_error = "Missing required field"

        analysis = analyzer.analyze(sample_task, sample_result)

        assert analysis.root_cause == "persistent_parse_error"
        assert analysis.suggested_action == "manual_review"
        assert analysis.should_retry is False

    def test_analyze_tool_failure(self, analyzer: SubAgentFailureAnalyzer, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult) -> None:
        """测试工具失败。"""
        sample_task.failure_type = "tool_result_missing"
        sample_task.runner_last_error = "Tool returned no output"

        analysis = analyzer.analyze(sample_task, sample_result)

        assert analysis.root_cause == "tool_transient_error"
        assert analysis.suggested_action == "retry_with_same_timeout"
        assert analysis.should_retry is True

    def test_analyze_model_error(self, analyzer: SubAgentFailureAnalyzer, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult) -> None:
        """测试模型错误。"""
        sample_task.failure_type = "model_error"
        sample_task.runner_last_error = "API rate limit exceeded"

        analysis = analyzer.analyze(sample_task, sample_result)

        assert analysis.root_cause == "model_transient_error"
        assert analysis.suggested_action == "retry_with_same_timeout"
        assert analysis.should_retry is True

    def test_analyze_channel_broken(self, analyzer: SubAgentFailureAnalyzer, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult) -> None:
        """测试通道损坏。"""
        sample_task.channel_status = "BROKEN"

        analysis = analyzer.analyze(sample_task, sample_result)

        assert analysis.root_cause == "channel_broken"
        assert analysis.suggested_action == "manual_channel_repair"
        assert analysis.should_retry is False
        assert analysis.should_split is False

    def test_analyze_verification_failed(self, analyzer: SubAgentFailureAnalyzer, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult) -> None:
        """测试验收失败。"""
        sample_task.status = "BLOCKED"
        sample_task.verification_status = "FAILED"

        analysis = analyzer.analyze(sample_task, sample_result)

        assert analysis.root_cause == "quality_issue"
        assert analysis.suggested_action == "manual_review"
        assert analysis.should_retry is False

    def test_analyze_generic_failure_retry(self, analyzer: SubAgentFailureAnalyzer, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult) -> None:
        """测试通用失败（可重试）。"""
        sample_task.failure_type = "unknown"
        sample_task.runner_attempts = 1
        sample_task.runner_last_error = "Unknown error"

        analysis = analyzer.analyze(sample_task, sample_result)

        assert analysis.root_cause == "generic_failure"
        assert analysis.suggested_action == "retry_with_same_timeout"
        assert analysis.should_retry is True

    def test_analyze_generic_failure_persistent(self, analyzer: SubAgentFailureAnalyzer, sample_task: SubAgentTask, sample_result: SubAgentRunnerResult) -> None:
        """测试通用失败（持久）。"""
        sample_task.failure_type = "unknown"
        sample_task.runner_attempts = 3
        sample_task.runner_last_error = "Unknown error"

        analysis = analyzer.analyze(sample_task, sample_result)

        assert analysis.root_cause == "persistent_failure"
        assert analysis.suggested_action == "manual_review"
        assert analysis.should_retry is False

    def test_suggest_splits_with_plan(self, analyzer: SubAgentFailureAnalyzer, sample_task: SubAgentTask) -> None:
        """测试根据 plan 生成拆分建议。"""
        sample_task.plan = ["步骤1", "步骤2", "步骤3", "步骤4", "步骤5", "步骤6"]

        suggestions = analyzer._suggest_splits(sample_task)

        assert len(suggestions) == 2
        assert "步骤1" in suggestions[0]
        assert "步骤4" in suggestions[1]

    def test_suggest_splits_without_plan(self, analyzer: SubAgentFailureAnalyzer, sample_task: SubAgentTask) -> None:
        """测试没有 plan 时生成拆分建议。"""
        sample_task.plan = ["步骤1", "步骤2"]
        sample_task.goal = "翻译这个文档"

        suggestions = analyzer._suggest_splits(sample_task)

        assert len(suggestions) >= 1
        assert "翻译" in suggestions[0]

    def test_split_suggestions_refactor(self, analyzer: SubAgentFailureAnalyzer, sample_task: SubAgentTask) -> None:
        """测试重构任务的拆分建议。"""
        sample_task.plan = ["步骤1"]
        sample_task.goal = "重构这个模块"

        suggestions = analyzer._suggest_splits(sample_task)

        assert len(suggestions) >= 2
        assert any("重构" in s for s in suggestions)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
