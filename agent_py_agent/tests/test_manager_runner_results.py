"""manager_runner_results 模块测试。

测试 SubAgentRunnerResultMixin.record_runner_result 方法。
"""
from __future__ import annotations

import json
import time

from agent_py_agent.agent.subagents.models import CapabilityRequest, SubAgentParsedOutput
from agent_py_agent.tests.support.manager_runner_results import _rrr, mock_manager, sample_task

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


# LLM: closeout_for_all_task_nodes writes feedback only; it must not revive parent acceptance states.
# 函数用途: 验证子代理完成后只在任务属性里留下同一套 closeout 提示事实，不改变成功状态。
def test_record_runner_result_writes_task_node_closeout_feedback_when_enabled(mock_manager, sample_task):
    mock_manager.closeout_for_all_task_nodes = True
    mock_manager._tasks[sample_task.id] = sample_task

    result = mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="完成",
        status="DONE",
        verification_status="VERIFIED",
    ))

    feedback = sample_task.attributes["task_node_closeout"]
    assert result.ok is True
    assert feedback["mode"] == "feedback_only"
    assert feedback["enabled_by"] == "closeout_for_all_task_nodes"
    assert feedback["ok"] is True
    assert feedback["status"] == "DONE"
    assert "acceptance" not in feedback


# LLM: Missing artifact refs are not a runner-level hard stop; closeout owns delivery validation.
# 函数用途: 验证子代理声明了不存在的产物路径时，不再由 runner 直接改成 BLOCKED。
def test_record_runner_result_does_not_block_missing_local_artifact_ref(mock_manager, sample_task, tmp_path):
    mock_manager._tasks[sample_task.id] = sample_task
    sample_task.task_dir = str(tmp_path)
    sample_task.output_dir = str(tmp_path / "output")
    sample_task.reports_dir = str(tmp_path / "reports")
    sample_task.task_workspace_artifacts_dir = ""
    sample_task.agent_run_artifacts_dir = ""
    sample_task.data_dir = ""
    sample_task.scratch_dir = ""
    sample_task.allowed_write_roots = []

    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        summary="报告已完成",
        artifacts=[{"path": "missing_report.md", "kind": "report"}],
        evidence_packets=[{
            "claim": "报告已完成",
            "checked_scope": "missing_report.md",
            "artifact_refs": ["missing_report.md"],
            "confidence": 0.9,
        }],
    )

    result = mock_manager.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="完成",
        structured_output=parsed,
    ))

    assert result.ok is True
    assert sample_task.status == "DONE"
    assert sample_task.verification_status == "VERIFIED"
    assert sample_task.failure_type == ""
    assert sample_task.runner_last_error == ""


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
        status="DONE",
        summary="文件已经写出，等待收口。",
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

    assert sample_task.status == "DONE"
    assert sample_task.failure_type == ""
    assert sample_task.blockers == []
    assert sample_task.capability_requests[0].status == "RESOLVED"
