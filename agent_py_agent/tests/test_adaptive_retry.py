from __future__ import annotations

"""LLM: tests for adaptive retry logic.

给人看的解释：
测试各种分析结果对应的重派策略。
"""

import pytest

from agent_py_agent.agent.agent_core.failure_analysis_service import split_task
from agent_py_agent.agent.subagents.models import SubAgentTask


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
        assert sample_task.status == "TAKEN_OVER"
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
