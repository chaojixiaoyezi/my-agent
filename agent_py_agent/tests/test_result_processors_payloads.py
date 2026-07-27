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

def _parsed_output(**overrides):
    params = {
        "found": True,
        "ok": True,
        "parse_error": "",
        "status": "DONE",
        "summary": "瀹屾垚",
        "blocked_reason": "",
        "failure_type": "",
        "used_skills": [],
        "used_tools": [],
        "evidence": [],
        "capability_requests": [],
        "artifacts": [],
        "tests": [],
        "patches": [],
        "lessons": [],
        "next_actions": [],
    }
    params.update(overrides)
    return SubAgentParsedOutput(**params)


def _output_payload_context(mock_task, parsed, **overrides):
    params = {
        "task": mock_task,
        "dry_run": False,
        "ok": True,
        "message": "鎴愬姛",
        "backend": "test-backend",
        "tool_rounds": 0,
        "parsed": parsed,
        "actual_tools": None,
        "structured_evidence_count": 0,
        "structured_request_count": 0,
        "created_request_ids": [],
        "ignored_tools": [],
        "ignored_skills": [],
        "artifacts": [],
        "evidence_packets": [],
        "findings": [],
        "tests": [],
        "patches": [],
        "lessons": [],
        "blockers": [],
        "next_actions": [],
        "structured_repair_attempted": False,
        "structured_repair_ok": False,
        "structured_repair_error": "",
        "now": 123456.0,
    }
    params.update(overrides)
    return OutputPayloadContext(**params)


def _runner_result_context(mock_task, parsed, **overrides):
    params = {
        "task": mock_task,
        "dry_run": False,
        "ok": True,
        "message": "鎴愬姛",
        "backend": "test-backend",
        "tool_rounds": 0,
        "live_context_compaction": {},
        "prompt": "",
        "response": "",
        "parsed": parsed,
        "structured_repair_attempted": False,
        "structured_repair_ok": False,
        "structured_repair_error": "",
        "structured_evidence_count": 0,
        "structured_request_count": 0,
        "artifact_count": 0,
        "test_count": 0,
        "patch_count": 0,
        "lesson_count": 0,
        "now": 123456.0,
    }
    params.update(overrides)
    return RunnerResultContext(**params)


def test_build_output_payload_basic(mock_task):
    """测试基本 payload 构建。"""
    parsed = _parsed_output()
    payload = _build_output_payload(
        _output_payload_context(mock_task, parsed, tool_rounds=5, actual_tools=["tool_a"])
    )

    assert payload["run_id"] == "test-run-123"
    assert payload["ok"] is True
    assert payload["backend"] == "test-backend"
    assert payload["tool_rounds"] == 5
    assert "next_action" in payload

def test_build_output_payload_with_blockers(mock_task):
    """测试带阻断因素的结果。"""
    parsed = _parsed_output(
        ok=False,
        parse_error="解析失败",
        status="FAILED",
        blocked_reason="资源不足",
    )
    payload = _build_output_payload(
        _output_payload_context(
            mock_task,
            parsed,
            ok=False,
            message="失败",
            backend="test",
            blockers=["资源不足"],
        )
    )

    assert payload["blockers"] == ["资源不足"]

def test_build_runner_result_basic(mock_task):
    """测试基本结果构建。"""
    parsed = _parsed_output()
    result = _build_runner_result(
        _runner_result_context(
            mock_task,
            parsed,
            tool_rounds=3,
            prompt="test prompt",
            response="test response",
        )
    )

    assert isinstance(result, SubAgentRunnerResult)
    assert result.run_id == "test-run-123"
    assert result.ok is True
    assert result.backend == "test-backend"
    assert result.tool_rounds == 3

def test_build_runner_result_with_structured_output(mock_task):
    """测试带结构化输出的结果。"""
    parsed = _parsed_output()
    result = _build_runner_result(
        _runner_result_context(
            mock_task,
            parsed,
            backend="test",
            structured_repair_attempted=True,
            structured_repair_ok=True,
            structured_evidence_count=2,
            structured_request_count=1,
            artifact_count=3,
            test_count=1,
            lesson_count=2,
        )
    )

    assert result.structured_output_found is True
    assert result.structured_output_ok is True
    assert result.evidence_count == 2
    assert result.capability_request_count == 1
