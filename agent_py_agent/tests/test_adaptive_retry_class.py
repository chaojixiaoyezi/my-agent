"""自适应重试测试 - failure_analysis_service.py 重试策略、退避算法。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestAdaptiveRetry:
    """测试 adaptive_retry 函数。"""

    def test_should_not_retry_when_disabled(self, tmp_path: Path):
        """不应该重试时返回空列表。"""
        from agent_py_agent.agent.agent_core.failure_analysis_service import (
            FailureAnalysis,
            adaptive_retry,
        )

        task = MagicMock()
        task.id = "test_task"
        task.runner_attempts = 1
        task.status = "FAILED"
        task.attributes = {}

        analysis = MagicMock()
        analysis.should_retry = False
        analysis.should_split = False

        result = adaptive_retry(task, analysis)

        assert result == []

    def test_should_split_task(self, tmp_path: Path):
        """需要拆分时返回子任务列表。"""
        from agent_py_agent.agent.agent_core.failure_analysis_service import (
            FailureAnalysis,
            adaptive_retry,
        )

        task = MagicMock()
        task.id = "test_task"
        task.goal = "原始目标"
        task.thought = ""
        task.plan = ["步骤1", "步骤2", "步骤3", "步骤4", "步骤5"]
        task.depth = 0
        task.runner_attempts = 1
        task.status = "FAILED"
        task.attributes = {}
        task.context_manifest = None

        analysis = MagicMock()
        analysis.should_retry = True
        analysis.should_split = True
        analysis.split_suggestions = ["第一部分", "第二部分"]

        result = adaptive_retry(task, analysis)

        assert len(result) == 2

    def test_should_retry_with_timeout_adjustment(self, tmp_path: Path):
        """需要调整超时并重试。"""
        from agent_py_agent.agent.agent_core.failure_analysis_service import (
            FailureAnalysis,
            adaptive_retry,
        )

        task = MagicMock()
        task.id = "test_task"
        task.runner_attempts = 1
        task.status = "FAILED"
        task.attributes = {}

        analysis = MagicMock()
        analysis.should_retry = True
        analysis.should_split = False
        analysis.should_adjust_timeout = True
        analysis.new_timeout_seconds = 300

        result = adaptive_retry(task, analysis)

        assert len(result) == 1
        assert task.attributes["dynamic_timeout_seconds"] == 300
        assert task.status == "PLANNING"

    def test_simple_retry(self, tmp_path: Path):
        """普通重试。"""
        from agent_py_agent.agent.agent_core.failure_analysis_service import (
            FailureAnalysis,
            adaptive_retry,
        )

        task = MagicMock()
        task.id = "test_task"
        task.runner_attempts = 1
        task.status = "FAILED"
        task.attributes = {}
        task.failure_type = "timeout"

        analysis = MagicMock()
        analysis.should_retry = True
        analysis.should_split = False
        analysis.should_adjust_timeout = False

        result = adaptive_retry(task, analysis)

        assert len(result) == 1
        assert task.runner_attempts == 0
        assert task.status == "PLANNING"

    def test_max_split_depth_exceeded(self, tmp_path: Path):
        """超过最大拆分深度时停止。"""
        from agent_py_agent.agent.agent_core.failure_analysis_service import (
            FailureAnalysis,
            adaptive_retry,
        )

        task = MagicMock()
        task.id = "test_task"
        task.depth = 3  # 超过 max_split_depth=2
        task.runner_attempts = 1
        task.status = "FAILED"
        task.attributes = {}

        analysis = MagicMock()
        analysis.should_retry = True
        analysis.should_split = True
        analysis.split_suggestions = ["a", "b", "c"]

        result = adaptive_retry(task, analysis, max_split_depth=2)

        assert result == []


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
        task.workflow_mode = None
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
        task.workflow_mode = None
        task.context_manifest = None
        task.status = "RUNNING"
        task.child_ids = []
        task.attributes = {}

        split_task(task, ["部分1", "部分2", "部分3"])

        assert task.status == "TAKEN_OVER"
        assert "split_into" in task.attributes


class TestShouldAutoSplit:
    """测试 should_auto_split 函数。"""

    def test_depth_limit_prevents_split(self, tmp_path: Path):
        """深度限制防止自动拆分。"""
        from agent_py_agent.agent.agent_core.failure_analysis_service import should_auto_split

        task = MagicMock()
        task.depth = 3
        task.plan = ["步骤1", "步骤2", "步骤3", "步骤4"]
        task.failure_type = "runner_timeout"
        task.runner_attempts = 2

        result = should_auto_split(task, max_depth=2)

        assert result is False

    def test_simple_task_not_split(self, tmp_path: Path):
        """简单任务不自动拆分。"""
        from agent_py_agent.agent.agent_core.failure_analysis_service import should_auto_split

        task = MagicMock()
        task.depth = 0
        task.plan = ["步骤1", "步骤2"]  # <= 3
        task.failure_type = "runner_timeout"
        task.runner_attempts = 2

        result = should_auto_split(task, max_depth=2)

        assert result is False

    def test_complex_timeout_task_should_split(self, tmp_path: Path):
        """复杂超时任务应该拆分。"""
        from agent_py_agent.agent.agent_core.failure_analysis_service import should_auto_split

        task = MagicMock()
        task.depth = 0
        task.plan = ["步骤1", "步骤2", "步骤3", "步骤4", "步骤5"]  # > 3
        task.failure_type = "runner_timeout"
        task.runner_attempts = 2

        result = should_auto_split(task, max_depth=2)

        assert result is True


class TestEstimateSplitCount:
    """测试 estimate_split_count 函数。"""

    def test_small_plan_single(self, tmp_path: Path):
        """小计划返回单个任务。"""
        from agent_py_agent.agent.agent_core.failure_analysis_service import estimate_split_count

        task = MagicMock()
        task.plan = ["步骤1", "步骤2"]

        result = estimate_split_count(task)

        assert result == 1

    def test_medium_plan_two(self, tmp_path: Path):
        """中等计划返回两个。"""
        from agent_py_agent.agent.agent_core.failure_analysis_service import estimate_split_count

        task = MagicMock()
        task.plan = ["步骤1", "步骤2", "步骤3", "步骤4", "步骤5"]

        result = estimate_split_count(task)

        assert result == 2

    def test_large_plan_three_or_more(self, tmp_path: Path):
        """大计划返回三个或更多。"""
        from agent_py_agent.agent.agent_core.failure_analysis_service import estimate_split_count

        task = MagicMock()
        task.plan = [f"步骤{i}" for i in range(20)]

        result = estimate_split_count(task)

        assert result >= 3
