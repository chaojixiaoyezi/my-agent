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
    ContextManifest,
    QualityContract,
    SubAgentExecutionContext,
    SubAgentTask,
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

        def list_runs(self):
            return list(self._tasks.values())

        def _build_work_order_paths(self, run_id: str, task_dir=None):
            return {}

        def _append_task_work_log(self, task: SubAgentTask, message: str):
            pass

        def _index_execution_context(self, context):
            pass

    return TestManager(tmp_path)


def make_task(tmp_path, task_id="run-456"):
    task = MagicMock(spec=SubAgentTask)
    for field_name, value in _task_defaults(tmp_path, task_id).items():
        setattr(task, field_name, value)
    return task


def _task_defaults(tmp_path, task_id: str) -> dict:
    return {
        "id": task_id,
        "goal": "执行数据分析任务",
        "thought": "需要分析数据",
        "plan": ["步骤1", "步骤2", "步骤3"],
        "agent_name": "data-agent",
        "role": "analyst",
        "status": "RUNNING",
        "verification_status": "UNVERIFIED",
        "channel_status": "OK",
        "runner_attempts": 2,
        "runner_last_error": "",
        "owner": "owner-user",
        "supervisor": "supervisor-user",
        "final_owner": "final-owner",
        "parent_id": "parent-123",
        "root_id": "root-456",
        "depth": 1,
        "task_dir": str(tmp_path / "task_dir"),
        "execution_context_file": str(tmp_path / "context.md"),
        "execution_context_json": str(tmp_path / "context.json"),
        **_task_list_defaults(tmp_path),
    }


def _task_list_defaults(tmp_path) -> dict:
    return {
        "allowed_skills": ["skill_a"],
        "allowed_tools": ["tool_x", "tool_y"],
        "used_skills": [],
        "used_tools": [],
        "capability_requests": [],
        "capability_grants": [],
        "capability_gaps": [],
        "acceptance_checks": ["check1", "check2"],
        "evidence": [],
        "quality_contract": QualityContract(),
        "context_manifest": ContextManifest(),
        "context_packs": [],
        "allowed_write_roots": ["/tmp"],
        "forbidden_write_roots": ["/home"],
        "locked_files": [],
        "status_file": str(tmp_path / "status.json"),
        "work_log_file": str(tmp_path / "work_log.txt"),
        "action_receipts_file": str(tmp_path / "receipts.json"),
        "acceptance_file": str(tmp_path / "acceptance.json"),
        "test_checklist_file": str(tmp_path / "tests.json"),
        "bugs_file": str(tmp_path / "bugs.json"),
        "skill_usage_file": str(tmp_path / "skill_usage.json"),
        "skill_sparks_file": str(tmp_path / "skill_sparks.md"),
        "handoff_file": str(tmp_path / "handoff.json"),
        "debrief_file": str(tmp_path / "debrief.md"),
        "output_json": str(tmp_path / "output.json"),
        "dependencies_json": str(tmp_path / "deps.json"),
    }


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


def test_build_execution_context_reports_collaboration_context_load_error(mock_manager, tmp_path):
    class BrokenCollaborationStore:
        def pending_requests_for_agent(self, **_kwargs):
            raise OSError("collaboration ledger unavailable")

    sample_task = make_task(tmp_path)
    mock_manager._tasks[sample_task.id] = sample_task
    mock_manager.collaboration_store = BrokenCollaborationStore()

    context = mock_manager.build_execution_context(sample_task.id)
    collaboration = context.context_bundle["collaboration"]

    assert collaboration["targeted_request_count"] == 0
    assert collaboration["collaboration_load_error"]["context"] == "subagent_context.collaboration.pending_requests"
    assert collaboration["collaboration_load_error"]["category"] == "io"


def test_build_execution_context_keeps_read_refs_without_hidden_dependency_rebinding(mock_manager, tmp_path):
    """执行上下文不再根据 sibling workflow 自动补写上游 artifact ref。"""
    upstream = make_task(tmp_path, "collect")
    upstream.status = "DONE"
    upstream.verification_status = "VERIFIED"
    upstream.workflow_parent_run_id = "batch-1"
    upstream.workflow_phase_id = "collect"
    artifact = tmp_path / "collect" / "data_collection.md"
    artifact.parent.mkdir()
    artifact.write_text("data", encoding="utf-8")
    upstream.artifact_refs = [str(artifact)]

    downstream = make_task(tmp_path, "content")
    downstream.workflow_parent_run_id = "batch-1"
    downstream.workflow_phase_id = "content"
    downstream.context_manifest = ContextManifest(required_read_paths=["data_collection.md"])
    mock_manager._tasks[upstream.id] = upstream
    mock_manager._tasks[downstream.id] = downstream

    context = mock_manager.build_execution_context(downstream.id)

    assert context.context_manifest.required_read_paths == ["data_collection.md"]


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
    product_root = tmp_path / "deliverables"
    run_workspace = tmp_path / "tasks" / "root-456" / "agents" / sample_task.id
    sample_task.role = "coordinator"
    sample_task.allowed_write_roots = [sample_task.task_dir, str(product_root)]
    sample_task.agent_run_workspace_dir = str(run_workspace)
    sample_task.agent_run_final_report_md = str(run_workspace / "final_report.md")
    mock_manager._tasks[sample_task.id] = sample_task

    context = mock_manager.build_execution_context(sample_task.id)

    assert "task_dir" in context.write_boundary
    assert "allowed_write_roots" in context.write_boundary
    assert "forbidden_write_roots" in context.write_boundary
    assert "locked_files" in context.write_boundary
    assert context.write_boundary["role"] == "coordinator"
    assert context.write_boundary["product_write_roots"] == [str(product_root)]
    assert context.write_boundary["product_write_policy"] == "direct"
    assert context.write_boundary["skill_sparks_file"].endswith("skill_sparks.md")
    assert str(run_workspace) in context.write_boundary["allowed_write_roots"]


def test_build_execution_context_worker_product_policy_direct(mock_manager, tmp_path):
    """worker/leaf worker 可以直接写自己的产物根。"""
    sample_task = make_task(tmp_path)
    product_root = tmp_path / "deliverables"
    sample_task.role = "leaf_worker"
    sample_task.allowed_write_roots = [sample_task.task_dir, str(product_root)]
    mock_manager._tasks[sample_task.id] = sample_task

    context = mock_manager.build_execution_context(sample_task.id)

    assert context.write_boundary["product_write_roots"] == [str(product_root)]
    assert context.write_boundary["product_write_policy"] == "direct"


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


def test_build_execution_context_exposes_controlled_exec_grant_refs(mock_manager, tmp_path):
    """受控 exec 授权必须把父级边界带进 runner context，不能让子代理自己补授权。"""
    sample_task = make_task(tmp_path)
    workspace = tmp_path / "workspace"
    grant = CapabilityGrant(
        id="grant-shell-1",
        request_id="req-shell-1",
        grant_to_run_id="run-456",
        grant_type="shell",
        tools=["controlled_exec"],
        command_allowlist=["python3", "pwd"],
        path_scope=[str(workspace)],
        network_scope=["api.example.com"],
        output_budget={"max_stdout_bytes": 1024, "max_stderr_bytes": 256},
        reason="需要读取任务目录内命令输出",
        constraints={"delete_policy": "trash_only"},
        expires_after_task=True,
        created_at=123456.0,
    )
    sample_task.capability_grants = [grant]
    mock_manager._tasks[sample_task.id] = sample_task

    context = mock_manager.build_execution_context(sample_task.id)

    assert "controlled_exec" in context.allowed_tools
    assert context.grants[0]["grant_type"] == "shell"
    assert context.grants[0]["command_allowlist"] == ["python3", "pwd"]
    assert context.controlled_exec_grants == [
        {
            "grant_id": "grant-shell-1",
            "request_id": "req-shell-1",
            "run_id": "run-456",
            "command_allowlist": ["python3", "pwd"],
            "path_scope": [str(workspace)],
            "network_scope": ["api.example.com"],
            "output_budget": {"max_stdout_bytes": 1024, "max_stderr_bytes": 256},
            "risk_level": "",
            "constraints": {"delete_policy": "trash_only"},
            "delete_policy": {
                "mode": "task_trash",
                "commands": ["rm", "rmdir", "unlink"],
                "requires_apply": True,
                "command_allowlist_required": False,
                "completion_requires": ["moved=true", "trash_manifest_ref"],
            },
        }
    ]
    assert context.write_boundary["controlled_exec_grants"] == context.controlled_exec_grants


def test_build_execution_context_adds_filesystem_grant_path_scope_to_write_roots(mock_manager, tmp_path):
    sample_task = make_task(tmp_path)
    build_dir = tmp_path / "deliverables" / "build"
    grant = CapabilityGrant(
        id="grant-write-1",
        request_id="req-write-1",
        grant_to_run_id="run-456",
        grant_type="tool",
        tools=["write_file", "apply_patch"],
        path_scope=[str(build_dir)],
        reason="允许接管 run 继续写产物目录",
        constraints={},
        expires_after_task=True,
        created_at=123456.0,
    )
    sample_task.capability_grants = [grant]
    mock_manager._tasks[sample_task.id] = sample_task

    context = mock_manager.build_execution_context(sample_task.id)

    assert str(build_dir) in context.write_boundary["allowed_write_roots"]


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

    data = json.loads((tmp_path / "context.json").read_text(encoding="utf-8"))
    assert data["run_id"] == "run-456"
    assert data["goal"] == "执行数据分析任务"
