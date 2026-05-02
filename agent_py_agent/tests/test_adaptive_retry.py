from __future__ import annotations

"""LLM: tests for adaptive retry logic.

给人看的解释：
测试各种分析结果对应的重派策略。
"""

import pytest

from agent_py_agent.agent.agent_core.adaptive_retry import (
    adaptive_retry,
    estimate_split_count,
    should_auto_split,
    split_task,
)
from agent_py_agent.agent.agent_core.failure_analyzer import FailureAnalysis
from agent_py_agent.agent.subagents.models import SubAgentTask


class TestAdaptiveRetry:
    """测试自适应重派。"""

    @pytest.fixture
    def sample_task(self) -> SubAgentTask:
        """创建示例任务。"""
        return SubAgentTask(
            id="test-task-1",
            goal="测试任务",
            thought="测试思考",
            plan=["步骤1", "步骤2", "步骤3", "步骤4", "步骤5", "步骤6"],
            status="BLOCKED",
            failure_type="runner_timeout",
            runner_attempts=1,
            depth=0,
        )

    def test_adaptive_retry_no_action(self, sample_task: SubAgentTask) -> None:
        """测试不需要任何操作的情况。"""
        analysis = FailureAnalysis(
            failure_type="runner_timeout",
            root_cause="some_other_cause",
            suggested_action="manual_review",
            should_retry=False,
            should_split=False,
            should_adjust_timeout=False,
        )

        result = adaptive_retry(sample_task, analysis, max_split_depth=2)

        assert result == []

    def test_adaptive_retry_adjust_timeout(self, sample_task: SubAgentTask) -> None:
        """测试调整超时重试。"""
        analysis = FailureAnalysis(
            failure_type="runner_timeout",
            root_cause="timeout",
            suggested_action="increase_timeout_and_retry",
            should_retry=True,
            should_split=False,
            should_adjust_timeout=True,
            new_timeout_seconds=180.0,
        )

        result = adaptive_retry(sample_task, analysis, max_split_depth=2)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].attributes["dynamic_timeout_seconds"] == 180.0
        assert result[0].runner_attempts == 0
        assert result[0].status == "PLANNING"
        assert result[0].failure_type == ""

    def test_adaptive_retry_split_task(self, sample_task: SubAgentTask) -> None:
        """测试拆分任务。"""
        analysis = FailureAnalysis(
            failure_type="runner_timeout",
            root_cause="task_too_large",
            suggested_action="split_task",
            should_retry=False,
            should_split=True,
            should_adjust_timeout=False,
            split_suggestions=["第一部分", "第二部分"],
        )

        result = adaptive_retry(sample_task, analysis, max_split_depth=2)

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0].id == "test-task-1-part-01"
        assert result[1].id == "test-task-1-part-02"
        assert result[0].parent_id == sample_task.id
        assert result[1].parent_id == sample_task.id
        assert sample_task.status == "SPLIT"

    def test_adaptive_retry_split_depth_limit(self, sample_task: SubAgentTask) -> None:
        """测试拆分深度限制。"""
        sample_task.depth = 3

        analysis = FailureAnalysis(
            failure_type="runner_timeout",
            root_cause="task_too_large",
            suggested_action="split_task",
            should_retry=False,
            should_split=True,
            split_suggestions=["第一部分", "第二部分"],
        )

        result = adaptive_retry(sample_task, analysis, max_split_depth=2)

        # 超过深度限制，不拆分
        assert result == []

    def test_adaptive_retry_simple_retry(self, sample_task: SubAgentTask) -> None:
        """测试简单重试。"""
        analysis = FailureAnalysis(
            failure_type="model_error",
            root_cause="model_transient_error",
            suggested_action="retry_with_same_timeout",
            should_retry=True,
            should_split=False,
            should_adjust_timeout=False,
        )

        result = adaptive_retry(sample_task, analysis, max_split_depth=2)

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].runner_attempts == 0
        assert result[0].status == "PLANNING"
        assert result[0].failure_type == ""


class TestSplitTask:
    """测试任务拆分。"""

    @pytest.fixture
    def sample_task(self) -> SubAgentTask:
        """创建示例任务。"""
        return SubAgentTask(
            id="test-task-1",
            goal="测试任务",
            thought="测试思考",
            plan=["步骤1", "步骤2", "步骤3", "步骤4", "步骤5", "步骤6"],
            depth=0,
        )

    def test_split_task_two_parts(self, sample_task: SubAgentTask) -> None:
        """测试拆分成两部分。"""
        suggestions = ["第一部分", "第二部分"]

        result = split_task(sample_task, suggestions)

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0].id == "test-task-1-part-01"
        assert result[1].id == "test-task-1-part-02"
        assert "第一部分" in result[0].goal
        assert "第二部分" in result[1].goal
        assert result[0].parent_id == sample_task.id
        assert result[1].parent_id == sample_task.id
        assert result[0].depth == 1
        assert result[1].depth == 1
        assert sample_task.status == "SPLIT"
        assert "split_into" in sample_task.attributes

    def test_split_task_inherit_plan(self, sample_task: SubAgentTask) -> None:
        """测试子任务继承 plan。"""
        suggestions = ["第一部分", "第二部分"]

        result = split_task(sample_task, suggestions)

        # 第一个子任务应该有前半部分 plan
        assert len(result[0].plan) > 0
        # 第二个子任务应该有后半部分 plan
        assert len(result[1].plan) > 0
        # 原任务 plan 不应该被清空
        assert len(sample_task.plan) == 6

    def test_split_task_single_suggestion(self, sample_task: SubAgentTask) -> None:
        """测试单个拆分建议。"""
        sample_task.plan = ["步骤1"]
        suggestions = ["第一部分"]

        result = split_task(sample_task, suggestions)

        assert len(result) == 1
        assert result[0].id == "test-task-1-part-01"
        assert result[0].plan == ["步骤1"]

    def test_split_task_inherit_allowed_items(self, sample_task: SubAgentTask) -> None:
        """测试子任务继承允许项。"""
        sample_task.allowed_skills = ["skill1", "skill2"]
        sample_task.allowed_tools = ["tool1", "tool2"]

        suggestions = ["第一部分", "第二部分"]
        result = split_task(sample_task, suggestions)

        for subtask in result:
            assert "skill1" in subtask.allowed_skills
            assert "skill2" in subtask.allowed_skills
            assert "tool1" in subtask.allowed_tools
            assert "tool2" in subtask.allowed_tools

    def test_split_task_preserves_context_manifest(self, sample_task: SubAgentTask) -> None:
        """测试子任务保留 context_manifest。"""
        from agent_py_agent.agent.subagents.models import ContextManifest

        sample_task.context_manifest = ContextManifest(
            core_pack_version="v1",
            task_pack_refs=["ref1"],
        )

        suggestions = ["第一部分", "第二部分"]
        result = split_task(sample_task, suggestions)

        for subtask in result:
            assert subtask.context_manifest.core_pack_version == "v1"
            assert subtask.context_manifest.task_pack_refs == ["ref1"]


class TestShouldAutoSplit:
    """测试自动拆分判断。"""

    def test_should_auto_split_timeout_and_many_attempts(self) -> None:
        """测试超时且多次重试应该拆分。"""
        task = SubAgentTask(
            id="test-task",
            goal="测试",
            thought="测试",
            plan=["步骤1", "步骤2", "步骤3", "步骤4"],
            failure_type="runner_timeout",
            runner_attempts=2,
            depth=0,
        )

        assert should_auto_split(task, max_depth=2) is True

    def test_should_auto_split_depth_limit(self) -> None:
        """测试深度限制。"""
        task = SubAgentTask(
            id="test-task",
            goal="测试",
            thought="测试",
            plan=["步骤1", "步骤2", "步骤3", "步骤4"],
            failure_type="runner_timeout",
            runner_attempts=3,
            depth=3,  # 超过限制
        )

        assert should_auto_split(task, max_depth=2) is False

    def test_should_auto_split_small_plan(self) -> None:
        """测试小 plan 不拆分。"""
        task = SubAgentTask(
            id="test-task",
            goal="测试",
            thought="测试",
            plan=["步骤1", "步骤2"],  # 太短
            failure_type="runner_timeout",
            runner_attempts=3,
            depth=0,
        )

        assert should_auto_split(task, max_depth=2) is False

    def test_should_auto_split_not_timeout(self) -> None:
        """测试非超时不拆分。"""
        task = SubAgentTask(
            id="test-task",
            goal="测试",
            thought="测试",
            plan=["步骤1", "步骤2", "步骤3", "步骤4"],
            failure_type="capability_request",  # 不是超时
            runner_attempts=3,
            depth=0,
        )

        assert should_auto_split(task, max_depth=2) is False


class TestEstimateSplitCount:
    """测试拆分数量估算。"""

    def test_estimate_split_count_small(self) -> None:
        """测试小任务。"""
        task = SubAgentTask(
            id="test-task",
            goal="测试",
            thought="测试思考",
            plan=["步骤1", "步骤2", "步骤3"],
        )

        assert estimate_split_count(task) == 1

    def test_estimate_split_count_medium(self) -> None:
        """测试中等任务。"""
        task = SubAgentTask(
            id="test-task",
            goal="测试",
            thought="测试思考",
            plan=["步骤1", "步骤2", "步骤3", "步骤4", "步骤5", "步骤6"],
        )

        assert estimate_split_count(task) == 2

    def test_estimate_split_count_large(self) -> None:
        """测试大任务。"""
        task = SubAgentTask(
            id="test-task",
            goal="测试",
            thought="测试思考",
            plan=["步骤%s" % i for i in range(1, 13)],  # 12 步
        )

        assert estimate_split_count(task) == 3

    def test_estimate_split_count_very_large(self) -> None:
        """测试超大任务。"""
        task = SubAgentTask(
            id="test-task",
            goal="测试",
            thought="测试思考",
            plan=["步骤%s" % i for i in range(1, 21)],  # 20 步
        )

        # 20 步应该至少拆成 5 个
        assert estimate_split_count(task) >= 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
