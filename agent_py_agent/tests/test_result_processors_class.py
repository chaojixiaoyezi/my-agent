"""result_processors 模块测试。

测试 _process_structured_output、_build_output_payload、_build_runner_result、
_write_runner_result_files、_append_runner_debrief_content 等函数。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.subagents.models import (
    SubAgentParsedOutput,
    SubAgentRunnerResult,
    SubAgentTask,
)
from agent_py_agent.agent.subagents.result_processors import (
    OutputPayloadContext,
    RunnerResultContext,
    _append_runner_debrief_content,
    _build_output_payload,
    _build_runner_result,
    _process_structured_output,
    _write_runner_result_files,
)

# ── 测试夹具 ──────────────────────────────────────────────────────────────

# ── Shared fixture builders ──────────────────────────────────────────────────

def _mock_task_with_files(tmp_path: Path) -> MagicMock:
    """Create a mock SubAgentTask with file paths set under tmp_path."""
    task = MagicMock(spec=SubAgentTask)
    task.id = "test-run-123"
    task.status = "RUNNING"
    task.verification_status = "UNVERIFIED"
    task.failure_type = ""
    task.result = ""
    task.ended_at = 0.0
    task.updated_at = 0.0
    task.heartbeat_at = 0.0
    task.runner_attempts = 0
    task.runner_last_attempt_at = 0.0
    task.runner_last_error = ""
    task.runner_active_attempt_id = ""
    task.runner_abandoned_attempt_ids = []
    task.used_tools = []
    task.used_skills = []
    task.evidence = []
    task.capability_requests = []
    task.capability_grants = []
    task.allowed_tools = ["tool_a", "tool_b"]
    task.allowed_skills = ["skill_x"]
    task.runner_prompt_file = str(tmp_path / "prompt.txt")
    task.runner_response_file = str(tmp_path / "response.txt")
    task.runner_result_file = str(tmp_path / "result.md")
    task.runner_result_json = str(tmp_path / "result.json")
    task.output_json = str(tmp_path / "output.json")
    task.debrief_file = str(tmp_path / "debrief.md")
    task.execution_context_file = str(tmp_path / "context.md")
    task.execution_context_json = str(tmp_path / "context.json")
    return task


def _sample_parsed_output_ok() -> SubAgentParsedOutput:
    return SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        summary="完成",
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


def _sample_parsed_output_empty() -> SubAgentParsedOutput:
    return SubAgentParsedOutput(
        found=False,
        ok=False,
        parse_error="",
        status="",
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


def _sample_runner_result_ok() -> SubAgentRunnerResult:
    return SubAgentRunnerResult(
        run_id="test-run-123",
        dry_run=False,
        ok=True,
        status="DONE",
        verification_status="NEEDS_ACCEPTANCE",
        message="成功",
        backend="test",
        tool_rounds=1,
        runner_attempts=1,
        runner_last_error="",
        execution_context_json="",
        execution_context_file="",
        prompt_file="",
        response_file="",
        result_file="",
        result_json="",
        output_json="",
        structured_output_found=True,
        structured_output_ok=True,
        structured_parse_error="",
        structured_repair_attempted=False,
        structured_repair_ok=False,
        structured_repair_error="",
        structured_summary="完成",
        evidence_count=0,
        capability_request_count=0,
        artifact_count=0,
        test_count=0,
        patch_count=0,
        lesson_count=0,
        blocked_reason="",
        created_at=123456.0,
    )


def _sample_runner_result_empty() -> SubAgentRunnerResult:
    return SubAgentRunnerResult(
        run_id="test-run-123",
        dry_run=False,
        ok=False,
        status="FAILED",
        verification_status="UNVERIFIED",
        message="",
        backend="",
        tool_rounds=0,
        runner_attempts=1,
        runner_last_error="",
        execution_context_json="",
        execution_context_file="",
        prompt_file="",
        response_file="",
        result_file="",
        result_json="",
        output_json="",
        structured_output_found=False,
        structured_output_ok=False,
        structured_parse_error="",
        structured_repair_attempted=False,
        structured_repair_ok=False,
        structured_repair_error="",
        structured_summary="",
        evidence_count=0,
        capability_request_count=0,
        artifact_count=0,
        test_count=0,
        patch_count=0,
        lesson_count=0,
        blocked_reason="",
        created_at=123456.0,
    )


# ── _process_structured_output 测试 ────────────────────────────────────────

def test_process_structured_output_basic(mock_task):
    """测试基本结构化输出处理。"""
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        summary="任务完成",
        blocked_reason="",
        failure_type="",
        used_skills=[],
        used_tools=[],
        evidence=[{"summary": "写入文件成功", "kind": "file_write", "ok": True}],
        capability_requests=[],
        artifacts=[],
        tests=[],
        patches=[],
        lessons=["学到新技能"],
        next_actions=["继续优化"],
    )

    result = _process_structured_output(mock_task, parsed, 123456.0, None)

    assert result["structured_evidence_count"] == 1
    assert result["structured_request_count"] == 0
    assert len(result["lessons"]) == 1
    assert len(result["next_actions"]) == 1


def test_process_structured_output_ignores_unauthorized_tools(mock_task):
    """测试忽略未授权工具。"""
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="RUNNING",
        used_skills=[],
        used_tools=["tool_c", "tool_a"],  # tool_c 未授权
        evidence=[],
        capability_requests=[],
        artifacts=[],
        tests=[],
        patches=[],
        lessons=[],
        next_actions=[],
    )

    result = _process_structured_output(mock_task, parsed, 123456.0, None)

    assert "tool_c" in result["ignored_tools"]
    assert "tool_a" not in result["ignored_tools"]


def test_process_structured_output_creates_capability_requests(mock_task):
    """测试创建能力请求。"""
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="RUNNING",
        used_skills=[],
        used_tools=[],
        evidence=[],
        capability_requests=[{
            "problem": "缺少图像处理能力",
            "needed_capability": "图像处理",
            "expected_output": "图像处理结果",
            "capability_type": "shell",
            "requested_tools": ["shell_gateway"],
            "requested_commands": ["python -m pytest"],
            "path_scope": ["/workspace/project"],
            "output_budget": {"stdout_bytes": 2048},
            "risk_level": "low",
        }],
        artifacts=[],
        tests=[],
        patches=[],
        lessons=[],
        next_actions=[],
    )

    result = _process_structured_output(mock_task, parsed, 123456.0, None)

    assert result["structured_request_count"] == 1
    assert len(mock_task.capability_requests) == 1
    request = mock_task.capability_requests[0]
    assert request.capability_type == "shell"
    assert request.requested_tools == ["shell_gateway"]
    assert request.requested_commands == ["python -m pytest"]
    assert request.output_budget["stdout_bytes"] == 2048


def test_process_structured_output_skips_empty_requests(mock_task):
    """测试跳过空的能力请求。"""
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="RUNNING",
        used_skills=[],
        used_tools=[],
        evidence=[],
        capability_requests=[{"problem": "", "needed_capability": ""}],  # 空的
        artifacts=[],
        tests=[],
        patches=[],
        lessons=[],
        next_actions=[],
    )

    result = _process_structured_output(mock_task, parsed, 123456.0, None)

    assert result["structured_request_count"] == 0


def test_process_structured_output_with_actual_tools(mock_task):
    """测试实际工具记录。"""
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="RUNNING",
        used_skills=[],
        used_tools=["tool_a"],
        evidence=[],
        capability_requests=[],
        artifacts=[],
        tests=[],
        patches=[],
        lessons=[],
        next_actions=[],
    )

    result = _process_structured_output(
        mock_task, parsed, 123456.0, actual_tools=["tool_a", "tool_b"]
    )

    # 实际使用的工具被记录
    assert len(mock_task.used_tools) >= 1


# ── _build_output_payload 测试 ──────────────────────────────────────────────





# ── _build_runner_result 测试 ──────────────────────────────────────────────





# ── _write_runner_result_files 测试 ────────────────────────────────────────

def test_write_runner_result_files_basic(tmp_path):
    """测试写入结果文件。"""
    mock_task = _mock_task_with_files(tmp_path)

    _write_runner_result_files(
        mock_task, _sample_runner_result_ok(), {"run_id": "test-run-123", "ok": True},
        prompt="test prompt content",
        response="test response content",
    )

    # 验证文件被创建
    assert (tmp_path / "prompt.txt").exists()
    assert (tmp_path / "response.txt").exists()
    assert (tmp_path / "output.json").exists()
    assert (tmp_path / "result.json").exists()

    # 验证内容
    assert (tmp_path / "prompt.txt").read_text() == "test prompt content"
    assert (tmp_path / "response.txt").read_text() == "test response content"


def test_write_runner_result_files_empty_prompt_response(tmp_path):
    """测试空 prompt/response 不写入文件。"""
    mock_task = _mock_task_with_files(tmp_path)

    _write_runner_result_files(
        mock_task, _sample_runner_result_empty(), {"run_id": "test-run-123"},
        prompt="",
        response="",
    )

    # 文件不应该被创建（因为内容为空）
    assert not (tmp_path / "prompt.txt").exists()
    assert not (tmp_path / "response.txt").exists()
    # 但 output 和 result json 应该存在
    assert (tmp_path / "output.json").exists()


# ── _append_runner_debrief_content 测试 ─────────────────────────────────────

def test_append_runner_debrief_content_basic(tmp_path):
    """测试追加 debrief 内容。"""
    task = MagicMock(spec=SubAgentTask)
    task.id = "test-run"
    task.debrief_file = str(tmp_path / "debrief.md")

    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        summary="完成",
        blocked_reason="",
        failure_type="",
        used_skills=[],
        used_tools=[],
        evidence=[],
        capability_requests=[],
        artifacts=[{"path": "output.txt", "summary": "生成文件"}],
        tests=[],
        patches=[],
        lessons=["学到经验"],
        next_actions=["下一步"],
    )

    # 确保父目录存在
    Path(tmp_path / "debrief.md").parent.mkdir(parents=True, exist_ok=True)

    _append_runner_debrief_content(task, parsed)

    content = Path(task.debrief_file).read_text(encoding="utf-8")
    assert "Runner Structured Output" in content
    assert "Runner Artifacts" in content
    assert "Runner Lessons" in content
    assert "Runner Next Actions" in content


def test_append_runner_debrief_content_empty_sections(tmp_path):
    """测试无内容时不追加。"""
    task = MagicMock(spec=SubAgentTask)
    task.id = "test-run"
    task.debrief_file = str(tmp_path / "debrief.md")

    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
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

    # 确保文件存在
    Path(tmp_path / "debrief.md").parent.mkdir(parents=True, exist_ok=True)
    Path(task.debrief_file).write_text("# DEBRIEF\n\n", encoding="utf-8")

    _append_runner_debrief_content(task, parsed)

    # 无内容时不追加，只保留原有内容
    content = Path(task.debrief_file).read_text()
    assert content == "# DEBRIEF\n\n"


def test_append_runner_debrief_content_creates_debrief_if_missing(tmp_path):
    """测试 debrief 文件不存在时创建。"""
    task = MagicMock(spec=SubAgentTask)
    task.id = "test-run"
    task.debrief_file = str(tmp_path / "new" / "debrief.md")

    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        summary="",
        blocked_reason="",
        failure_type="",
        used_skills=[],
        used_tools=[],
        evidence=[],
        capability_requests=[],
        artifacts=[{"path": "f.txt", "summary": "f"}],
        tests=[],
        patches=[],
        lessons=[],
        next_actions=[],
    )

    _append_runner_debrief_content(task, parsed)

    assert Path(task.debrief_file).exists()
    content = Path(task.debrief_file).read_text()
    assert "# DEBRIEF" in content
    assert "Runner Artifacts" in content
