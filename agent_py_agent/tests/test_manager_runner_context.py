"""manager_runner_context 模块测试。

测试 SubAgentRunnerContextMixin.build_execution_context 和 write_execution_context 方法。
"""
from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.subagents.models import (
    CapabilityGrant,
    SubAgentTask,
    SubAgentExecutionContext,
    QualityContract,
    ContextManifest,
)


# ── 测试夹具 ──────────────────────────────────────────────────────────────

@pytest.fixture
def mock_manager(tmp_path):
    """创建模拟的 SubAgentRunnerContextMixin 管理器。"""
    from agent_py_agent.agent.subagents.manager_runner_context import SubAgentRunnerContextMixin

    class TestManager(SubAgentRunnerContextMixin):
        def __init__(self, tmp_dir):
            self._tmp_dir = tmp_dir
            self._tasks = {}

        def load(self, run_id: str) -> SubAgentTask:
            return self._tasks.get(run_id)

        def save(self, task: SubAgentTask) -> None:
            self._tasks[task.id] = task

        def _build_work_order_paths(self, run_id: str, task_dir=None):
            return {}

        def _append_task_work_log(self, task: SubAgentTask, message: str):
            pass

        def _index_execution_context(self, context):
            pass

    return TestManager(tmp_path)


def make_task(tmp_path, task_id="run-456"):
    """创建示例 SubAgentTask。"""
    task = MagicMock(spec=SubAgentTask)
    task.id = task_id
    task.goal = "执行数据分析任务"
    task.thought = "需要分析数据"
    task.plan = ["步骤1", "步骤2", "步骤3"]
    task.agent_name = "data-agent"
    task.role = "analyst"
    task.status = "RUNNING"
    task.verification_status = "UNVERIFIED"
    task.channel_status = "OK"
    task.runner_attempts = 2
    task.runner_last_error = ""
    task.owner = "owner-user"
    task.supervisor = "supervisor-user"
    task.final_owner = "final-owner"
    task.parent_id = "parent-123"
    task.root_id = "root-456"
    task.depth = 1
    task.task_dir = str(tmp_path / "task_dir")
    task.execution_context_file = str(tmp_path / "context.md")
    task.execution_context_json = str(tmp_path / "context.json")
    task.allowed_skills = ["skill_a"]
    task.allowed_tools = ["tool_x", "tool_y"]
    task.used_skills = []
    task.used_tools = []
    task.capability_requests = []
    task.capability_grants = []
    task.capability_gaps = []
    task.acceptance_checks = ["check1", "check2"]
    task.evidence = []
    task.quality_contract = QualityContract()
    task.context_manifest = ContextManifest()
    task.context_packs = []
    task.allowed_write_roots = ["/tmp"]
    task.forbidden_write_roots = ["/home"]
    task.locked_files = []
    task.status_file = str(tmp_path / "status.json")
    task.work_log_file = str(tmp_path / "work_log.txt")
    task.action_receipts_file = str(tmp_path / "receipts.json")
    task.acceptance_file = str(tmp_path / "acceptance.json")
    task.test_checklist_file = str(tmp_path / "tests.json")
    task.bugs_file = str(tmp_path / "bugs.json")
    task.skill_usage_file = str(tmp_path / "skill_usage.json")
    task.handoff_file = str(tmp_path / "handoff.json")
    task.debrief_file = str(tmp_path / "debrief.md")
    task.output_json = str(tmp_path / "output.json")
    task.dependencies_json = str(tmp_path / "deps.json")
    return task


# ── build_execution_context 基本测试 ─────────────────────────────────────────

def test_build_execution_context_basic(mock_manager, tmp_path):
    """测试基本执行上下文构建。"""
    sample_task = make_task(tmp_path)
    mock_manager._tasks[sample_task.id] = sample_task

    context = mock_manager.build_execution_context(sample_task.id)

    assert context.run_id == "run-456"
    assert context.goal == "执行数据分析任务"
    assert context.thought == "需要分析数据"
    assert context.plan == ["步骤1", "步骤2", "步骤3"]
    assert context.agent_name == "data-agent"
    assert context.role == "analyst"
    assert context.generated_at > 0


def test_build_execution_context_includes_granted_skills_and_tools(mock_manager, tmp_path):
    """测试包含授权的 skills 和 tools。"""
    sample_task = make_task(tmp_path)
    grant = CapabilityGrant(
        id="grant-1",
        request_id="req-1",
        grant_to_run_id="run-456",
        skills=["skill_b", "skill_c"],
        tools=["tool_z"],
        capability_cards=[],
        reason="needed for task",
        constraints={},
        expires_after_task=True,
        created_at=123456.0,
    )
    sample_task.capability_grants = [grant]
    mock_manager._tasks[sample_task.id] = sample_task

    context = mock_manager.build_execution_context(sample_task.id)

    # granted_skills 和 granted_tools 应该被合并
    assert "skill_a" in context.allowed_skills
    assert "skill_b" in context.allowed_skills


def test_build_execution_context_granted_cards(mock_manager, tmp_path):
    """测试授权卡片去重。"""
    sample_task = make_task(tmp_path)
    grant = CapabilityGrant(
        id="grant-1",
        request_id="req-1",
        grant_to_run_id="run-456",
        skills=["skill_a"],
        tools=["tool_x"],
        capability_cards=[
            {"id": "card1", "kind": "skill", "name": "技能1"},
            {"id": "card1", "kind": "skill", "name": "技能1"},  # 重复
        ],
        reason="test",
        constraints={},
        expires_after_task=True,
        created_at=123456.0,
    )
    sample_task.capability_grants = [grant]
    mock_manager._tasks[sample_task.id] = sample_task

    context = mock_manager.build_execution_context(sample_task.id, max_cards=10)

    # 卡片应该去重
    assert len(context.granted_cards) == 1


def test_build_execution_context_with_granted_cards_limit(mock_manager, tmp_path):
    """测试授权卡片数量限制。"""
    sample_task = make_task(tmp_path)
    grant = CapabilityGrant(
        id="grant-1",
        request_id="req-1",
        grant_to_run_id="run-456",
        skills=[],
        tools=[],
        capability_cards=[
            {"id": f"card{i}", "kind": "skill", "name": f"技能{i}"}
            for i in range(10)
        ],
        reason="test",
        constraints={},
        expires_after_task=True,
        created_at=123456.0,
    )
    sample_task.capability_grants = [grant]
    mock_manager._tasks[sample_task.id] = sample_task

    context = mock_manager.build_execution_context(sample_task.id, max_cards=5)

    assert len(context.granted_cards) == 5


def test_build_execution_context_write_boundary(mock_manager, tmp_path):
    """测试写入边界设置。"""
    sample_task = make_task(tmp_path)
    mock_manager._tasks[sample_task.id] = sample_task

    context = mock_manager.build_execution_context(sample_task.id)

    assert "task_dir" in context.write_boundary
    assert "allowed_write_roots" in context.write_boundary
    assert "forbidden_write_roots" in context.write_boundary
    assert "locked_files" in context.write_boundary


def test_build_execution_context_instructions(mock_manager, tmp_path):
    """测试执行指令。"""
    sample_task = make_task(tmp_path)
    mock_manager._tasks[sample_task.id] = sample_task

    context = mock_manager.build_execution_context(sample_task.id)

    assert len(context.instructions) > 0
    assert any("allowed_skills" in i for i in context.instructions)


# ── build_execution_context 边界场景测试 ─────────────────────────────────

def test_build_execution_context_with_no_grants(mock_manager, tmp_path):
    """测试无授权时的上下文。"""
    sample_task = make_task(tmp_path)
    sample_task.capability_grants = []
    mock_manager._tasks[sample_task.id] = sample_task

    context = mock_manager.build_execution_context(sample_task.id)

    assert context.allowed_skills == ["skill_a"]
    assert context.granted_cards == []


def test_build_execution_context_preserves_task_fields(mock_manager, tmp_path):
    """测试保留任务字段。"""
    sample_task = make_task(tmp_path)
    sample_task.runner_attempts = 5
    sample_task.runner_last_error = "previous error"
    mock_manager._tasks[sample_task.id] = sample_task

    context = mock_manager.build_execution_context(sample_task.id)

    assert context.runner_attempts == 5
    assert context.runner_last_error == "previous error"


def test_build_execution_context_empty_plan(mock_manager, tmp_path):
    """测试空计划。"""
    sample_task = make_task(tmp_path)
    sample_task.plan = []
    mock_manager._tasks[sample_task.id] = sample_task

    context = mock_manager.build_execution_context(sample_task.id)

    assert context.plan == []


def test_build_execution_context_contains_required_fields(mock_manager, tmp_path):
    """测试执行上下文包含所有必需字段。"""
    sample_task = make_task(tmp_path)
    mock_manager._tasks[sample_task.id] = sample_task

    context = mock_manager.build_execution_context(sample_task.id)

    # 检查关键字段存在
    assert hasattr(context, "run_id")
    assert hasattr(context, "generated_at")
    assert hasattr(context, "goal")
    assert hasattr(context, "plan")
    assert hasattr(context, "agent_name")
    assert hasattr(context, "allowed_skills")
    assert hasattr(context, "allowed_tools")
    assert hasattr(context, "granted_cards")
    assert hasattr(context, "write_boundary")
    assert hasattr(context, "instructions")


def test_build_execution_context_grants_list(mock_manager, tmp_path):
    """测试授权记录列表。"""
    sample_task = make_task(tmp_path)
    grant = CapabilityGrant(
        id="grant-1",
        request_id="req-1",
        grant_to_run_id="run-456",
        skills=["skill_new"],
        tools=[],
        capability_cards=[],
        reason="needed",
        constraints={},
        expires_after_task=True,
        created_at=123456.0,
    )
    sample_task.capability_grants = [grant]
    mock_manager._tasks[sample_task.id] = sample_task

    context = mock_manager.build_execution_context(sample_task.id)

    assert len(context.grants) == 1
    assert context.grants[0]["id"] == "grant-1"
    assert context.grants[0]["skills"] == ["skill_new"]


def test_build_execution_context_multiple_grants(mock_manager, tmp_path):
    """测试多条授权记录。"""
    sample_task = make_task(tmp_path)
    grant1 = CapabilityGrant(
        id="grant-1",
        request_id="req-1",
        grant_to_run_id="run-456",
        skills=["skill_1"],
        tools=[],
        capability_cards=[],
        reason="reason1",
        constraints={},
        expires_after_task=True,
        created_at=123456.0,
    )
    grant2 = CapabilityGrant(
        id="grant-2",
        request_id="req-2",
        grant_to_run_id="run-456",
        skills=[],
        tools=["tool_new"],
        capability_cards=[],
        reason="reason2",
        constraints={},
        expires_after_task=True,
        created_at=123456.1,
    )
    sample_task.capability_grants = [grant1, grant2]
    mock_manager._tasks[sample_task.id] = sample_task

    context = mock_manager.build_execution_context(sample_task.id)

    assert len(context.grants) == 2
    # 授权的 skills 和 tools 被合并
    assert "skill_1" in context.allowed_skills
    assert "tool_new" in context.allowed_tools


# ── write_execution_context 测试 ──────────────────────────────────────────

def test_write_execution_context_creates_files(mock_manager, tmp_path):
    """测试写入执行上下文文件。"""
    sample_task = make_task(tmp_path)
    sample_task.execution_context_file = str(tmp_path / "context.md")
    sample_task.execution_context_json = str(tmp_path / "context.json")
    mock_manager._tasks[sample_task.id] = sample_task

    context = mock_manager.write_execution_context(sample_task.id)

    assert (tmp_path / "context.json").exists()
    assert (tmp_path / "context.md").exists()


def test_write_execution_context_json_content(mock_manager, tmp_path):
    """测试执行上下文 JSON 内容。"""
    sample_task = make_task(tmp_path)
    sample_task.execution_context_file = str(tmp_path / "context.md")
    sample_task.execution_context_json = str(tmp_path / "context.json")
    mock_manager._tasks[sample_task.id] = sample_task

    mock_manager.write_execution_context(sample_task.id)

    data = json.loads((tmp_path / "context.json").read_text())
    assert data["run_id"] == "run-456"
    assert data["goal"] == "执行数据分析任务"