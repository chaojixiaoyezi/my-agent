"""自适应重试测试 - failure_analysis_service.py 重试策略、退避算法。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


class TestSplitTask:
    """测试 split_task 函数。"""

    def test_split_into_multiple_subtasks(self, tmp_path: Path):
        """拆分为多个子任务。"""
        from agent_py_agent.agent.agent_core.failure_analysis_service import split_task

        task = MagicMock()
        task.id = "parent_task"
        task.goal = "父任务目标"
        task.thought = ""
        task.plan = ["步骤1", "步骤2", "步骤3", "步骤4"]
        task.agent_name = "general"
        task.role = "general"
        task.owner = ""
        task.supervisor = ""
        task.parent_id = ""
        task.root_id = ""
        task.depth = 0
        task.allowed_skills = []
        task.allowed_tools = ["tool1", "tool2"]
        task.context_manifest = None
        task.status = "RUNNING"
        task.child_ids = []

        suggestions = ["第一部分", "第二部分"]
        subtasks = split_task(task, suggestions)

        assert len(subtasks) == 2
        assert subtasks[0].id == "parent_task-part-01"
        assert subtasks[1].id == "parent_task-part-02"
        assert subtasks[0].depth == 1
        assert subtasks[1].depth == 1

    def test_split_updates_parent_status(self, tmp_path: Path):
        """拆分后更新父任务状态。"""
        from agent_py_agent.agent.agent_core.failure_analysis_service import split_task

        task = MagicMock()
        task.id = "parent_task"
        task.goal = "父任务目标"
        task.thought = ""
        task.plan = ["步骤1", "步骤2"]
        task.depth = 0
        task.allowed_skills = []
        task.allowed_tools = []
        task.context_manifest = None
        task.status = "RUNNING"
        task.child_ids = []
        task.attributes = {}

        split_task(task, ["部分1", "部分2", "部分3"])

        assert task.status == "TAKEN_OVER"
        assert "split_into" in task.attributes
