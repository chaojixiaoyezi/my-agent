"""manager_runner_results 模块测试。

测试 SubAgentRunnerResultMixin.record_runner_result 方法。
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.subagents.manager_runner_results import (
    RecordRunnerResultParams,
    SubAgentRunnerResultMixin,
)
from agent_py_agent.agent.subagents.models import (
    CapabilityRequest,
    SubAgentParsedOutput,
    SubAgentRunnerResult,
    SubAgentTask,
)

# ── 测试夹具 ──────────────────────────────────────────────────────────────

def _rrr(run_id: str, **kwargs) -> RecordRunnerResultParams:
    """Helper to create RecordRunnerResultParams with run_id as positional arg."""
    return RecordRunnerResultParams(run_id=run_id, **kwargs)

@pytest.fixture
def mock_manager(tmp_path):
    """创建模拟的 SubAgentRunnerResultMixin 管理器。"""
    from agent_py_agent.agent.subagents.manager_runner_results import SubAgentRunnerResultMixin

    # 创建一个使用 mixin 的类
    class TestManager(SubAgentRunnerResultMixin):
        def __init__(self, tmp_dir):
            self._tmp_dir = tmp_dir
            self._tasks = {}
            self._work_logs = []

        def load(self, run_id: str) -> SubAgentTask:
            return self._tasks.get(run_id)

        def save(self, task: SubAgentTask) -> None:
            self._tasks[task.id] = task

        def _build_work_order_paths(self, run_id: str, task_dir=None):
            return {}

        def _append_task_work_log(self, task: SubAgentTask, message: str):
            self._work_logs.append(message)

        def _index_runner_result(self, result, payload):
            pass

        def record_learning_candidates(self, task, lessons):
            return []

    return TestManager(tmp_path)


@pytest.fixture
def sample_task(tmp_path):
    task = MagicMock(spec=SubAgentTask)
    for field_name, value in _sample_task_fields(tmp_path).items():
        setattr(task, field_name, value)
    return task


def _sample_task_fields(tmp_path) -> dict:
    return {
        "id": "run-123",
        "status": "RUNNING",
        "verification_status": "UNVERIFIED",
        "failure_type": "",
        "result": "",
        "ended_at": 0.0,
        "updated_at": 0.0,
        "heartbeat_at": 0.0,
        "runner_attempts": 0,
        "runner_last_attempt_at": 0.0,
        "runner_last_error": "",
        "runner_active_attempt_id": "",
        "runner_abandoned_attempt_ids": [],
        "used_tools": [],
        "used_skills": [],
        "evidence": [],
        "evidence_packets": [],
        "findings": [],
        "evidence_refs": [],
        "artifact_refs": [],
        "blockers": [],
        "progress": 0.0,
        "current_step": "",
        "latest_summary": "",
        "budget_used": {},
        "checkpoint_ref": "",
        "capability_requests": [],
        "capability_grants": [],
        "allowed_tools": ["tool_a", "tool_b"],
        "allowed_skills": ["skill_x"],
        **_sample_task_path_fields(tmp_path),
    }


def _sample_task_path_fields(tmp_path) -> dict:
    return {
        "runner_prompt_file": str(tmp_path / "prompt.txt"),
        "runner_response_file": str(tmp_path / "response.txt"),
        "runner_result_file": str(tmp_path / "result.md"),
        "runner_result_json": str(tmp_path / "result.json"),
        "output_json": str(tmp_path / "output.json"),
        "status_report_json": str(tmp_path / "status_report.json"),
        "debrief_file": str(tmp_path / "debrief.md"),
        "execution_context_file": str(tmp_path / "context.md"),
        "execution_context_json": str(tmp_path / "context.json"),
        "task_dir": str(tmp_path),
        "goal": "测试任务",
        "thought": "",
        "plan": [],
        "agent_name": "test-agent",
        "role": "general",
        "owner": "tester",
        "supervisor": "",
        "final_owner": "",
        "parent_id": "",
        "root_id": "",
        "depth": 0,
    }


# ── record_runner_result 基本测试 ──────────────────────────────────────────

def test_record_runner_result_success(mock_manager, sample_task):
    """测试成功记录 runner 结果。"""
    mock_manager._tasks[sample_task.id] = sample_task

    result = mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="任务完成",
        backend="test-backend",
        tool_rounds=5,
        status="DONE",
    ))

    assert result.ok is True
    assert result.status == "DONE"
    assert result.message == "任务完成"
    assert result.backend == "test-backend"
    assert result.tool_rounds == 5


def test_record_runner_result_with_parsed_output(mock_manager, sample_task):
    """测试带结构化输出的结果记录。"""
    mock_manager._tasks[sample_task.id] = sample_task

    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        summary="解析完成",
        blocked_reason="",
        failure_type="",
        used_skills=[],
        used_tools=[],
        evidence=[],
        capability_requests=[],
        artifacts=[],
        tests=[],
        patches=[],
        lessons=["学到经验"],
        next_actions=[],
    )

    result = mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="完成",
        structured_output=parsed,
    ))

    assert result.ok is True
    assert result.structured_output_found is True


def test_record_runner_result_dry_run(mock_manager, sample_task):
    """测试 dry_run 不增加 runner_attempts。"""
    sample_task.runner_attempts = 0
    mock_manager._tasks[sample_task.id] = sample_task

    result = mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=True,
        ok=True,
        message="dry run",
    ))

    assert result.dry_run is True
    assert result.runner_attempts == 0  # dry_run 不增加


def test_record_runner_result_failure(mock_manager, sample_task):
    """测试失败结果记录。"""
    mock_manager._tasks[sample_task.id] = sample_task

    result = mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=False,
        message="执行失败",
        status="FAILED",
    ))

    assert result.ok is False
    assert result.status == "FAILED"


# ── 过期结果处理测试 ──────────────────────────────────────────────────────

def test_record_runner_result_ignores_abandoned_attempt(mock_manager, sample_task):
    """测试忽略已放弃的尝试结果。"""
    sample_task.runner_abandoned_attempt_ids = ["attempt-old"]
    sample_task.runner_active_attempt_id = ""
    mock_manager._tasks[sample_task.id] = sample_task

    result = mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        attempt_id="attempt-old",
        dry_run=False,
        ok=True,
        message="stale result",
    ))

    assert result.ok is False
    assert "abandoned" in result.message


def test_record_runner_result_ignores_non_active_attempt(mock_manager, sample_task):
    """测试忽略非活跃尝试结果。"""
    sample_task.runner_active_attempt_id = "active-123"
    mock_manager._tasks[sample_task.id] = sample_task

    result = mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        attempt_id="different-attempt",
        dry_run=False,
        ok=True,
        message="stale result",
    ))

    assert result.ok is False
    assert "non-active" in result.message


# ── 状态更新测试 ──────────────────────────────────────────────────────────

def test_record_runner_result_updates_task_status(mock_manager, sample_task):
    """测试更新任务状态。"""
    mock_manager._tasks[sample_task.id] = sample_task

    mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="done",
        status="DONE",
    ))

    assert sample_task.status == "DONE"
    assert sample_task.updated_at > 0


def test_record_runner_result_sets_ended_at_for_terminal_statuses(mock_manager, sample_task):
    """测试终态设置 ended_at。"""
    mock_manager._tasks[sample_task.id] = sample_task

    before = time.time()
    mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=False,
        message="failed",
        status="FAILED",
    ))

    assert sample_task.ended_at >= before


def test_record_runner_result_increments_runner_attempts(mock_manager, sample_task):
    """测试增加 runner_attempts。"""
    sample_task.runner_attempts = 2
    mock_manager._tasks[sample_task.id] = sample_task

    mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="ok",
    ))

    assert sample_task.runner_attempts == 3


def test_record_runner_result_records_last_error(mock_manager, sample_task):
    """测试记录最后错误。"""
    mock_manager._tasks[sample_task.id] = sample_task

    mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=False,
        message="Error: connection failed",
        status="FAILED",
    ))

    assert "connection failed" in sample_task.runner_last_error


# ── 结构化输出解析测试 ────────────────────────────────────────────────────

def test_record_runner_result_parses_structured_output(mock_manager, sample_task):
    """测试解析结构化输出。"""
    mock_manager._tasks[sample_task.id] = sample_task

    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        summary="summary from model",
        blocked_reason="",
        failure_type="",
        used_skills=["skill_x"],
        used_tools=["tool_a"],
        evidence=[{"summary": "evidence 1", "kind": "file", "ok": True}],
        capability_requests=[],
        artifacts=[],
        tests=[],
        patches=[],
        lessons=[],
        next_actions=["next"],
    )

    result = mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="original message",
        structured_output=parsed,
    ))

    assert result.structured_summary == "summary from model"


def test_record_runner_result_handles_parse_failure(mock_manager, sample_task):
    """测试解析失败处理。"""
    mock_manager._tasks[sample_task.id] = sample_task

    parsed = SubAgentParsedOutput(
        found=True,
        ok=False,
        parse_error="JSON decode error",
        status="",
        summary="",
        blocked_reason="",
        failure_type="",
        used_skills=[],
        used_tools=[],
        evidence=[],
        capability_requests=[],
        artifacts=[],
        tests=[],
        patches=[],
        lessons=[],
        next_actions=[],
    )

    result = mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="original",
        structured_output=parsed,
    ))

    assert result.ok is False
    assert "parse failed" in result.message


# ── 文件写入测试 ──────────────────────────────────────────────────────────

def test_record_runner_result_writes_output_json(mock_manager, sample_task, tmp_path):
    """测试写入 output.json。"""
    sample_task.output_json = str(tmp_path / "output.json")
    mock_manager._tasks[sample_task.id] = sample_task

    mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="ok",
    ))

    assert (tmp_path / "output.json").exists()
    data = json.loads((tmp_path / "output.json").read_text())
    assert data["ok"] is True


def test_record_runner_result_writes_result_json(mock_manager, sample_task, tmp_path):
    """测试写入 runner_result.json。"""
    sample_task.runner_result_json = str(tmp_path / "result.json")
    sample_task.runner_result_file = str(tmp_path / "result.md")
    mock_manager._tasks[sample_task.id] = sample_task

    mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="ok",
        prompt="the prompt",
        response="the response",
    ))

    assert (tmp_path / "result.json").exists()


# ── 边界场景测试 ──────────────────────────────────────────────────────────

def test_record_runner_result_without_structured_output(mock_manager, sample_task):
    """测试无结构化输出时的处理。"""
    mock_manager._tasks[sample_task.id] = sample_task

    result = mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="simple success",
    ))

    assert result.structured_output_found is False


def test_record_runner_result_clears_active_attempt_on_success(mock_manager, sample_task):
    """测试成功后清除活跃尝试ID。"""
    sample_task.runner_active_attempt_id = "attempt-123"
    mock_manager._tasks[sample_task.id] = sample_task

    mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        attempt_id="attempt-123",
        dry_run=False,
        ok=True,
        message="ok",
    ))

    assert sample_task.runner_active_attempt_id == ""


def test_record_runner_result_with_blocked_reason(mock_manager, sample_task):
    """测试带 blocked_reason 的结果。"""
    mock_manager._tasks[sample_task.id] = sample_task

    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="BLOCKED",
        summary="blocked",
        blocked_reason="waiting for resource",
        failure_type="",
        used_skills=[],
        used_tools=[],
        evidence=[],
        capability_requests=[],
        artifacts=[],
        tests=[],
        patches=[],
        lessons=[],
        next_actions=[],
    )

    result = mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="",
        structured_output=parsed,
    ))

    assert "waiting for resource" in result.blocked_reason


def test_record_runner_result_success_clears_stale_failure_state(mock_manager, sample_task):
    """成功重跑后清理旧 failure_type、blockers 和 OPEN capability request。"""
    sample_task.failure_type = "capability_request"
    sample_task.blockers = ["旧的 write_file 能力缺口"]
    sample_task.capability_requests = [
        CapabilityRequest(
            id="capreq-old",
            from_run_id=sample_task.id,
            problem="缺少写入能力",
            needed_capability="write_file",
        )
    ]
    mock_manager._tasks[sample_task.id] = sample_task

    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="AWAITING_ACCEPTANCE",
        summary="文件已经写出，等待验收。",
        blocked_reason="",
        failure_type="",
        used_skills=[],
        used_tools=[],
        evidence=[],
        capability_requests=[],
        artifacts=[],
        tests=[],
        patches=[],
        lessons=[],
        next_actions=[],
    )

    mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="ok",
        structured_output=parsed,
    ))

    assert sample_task.status == "AWAITING_ACCEPTANCE"
    assert sample_task.failure_type == ""
    assert sample_task.blockers == []
    assert sample_task.capability_requests[0].status == "RESOLVED"
