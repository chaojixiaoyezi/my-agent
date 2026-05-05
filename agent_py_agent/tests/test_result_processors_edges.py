"""result_processors 边界场景测试。"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.subagents.models import SubAgentParsedOutput
from agent_py_agent.agent.subagents.result_processors import (
    OutputPayloadContext,
    _build_output_payload,
    _process_structured_output,
)


def test_process_structured_output_handles_raw_json(mock_task):
    """测试处理原始 JSON 数据。"""
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        used_skills=[],
        used_tools=[],
        evidence=[{
            "summary": "证据1",
            "kind": "test",
            "command": "run test",
            "path": "/test/path",
            "url": "",
            "ok": True,
        }],
        capability_requests=[],
        artifacts=[],
        tests=[],
        patches=[],
        lessons=[],
        next_actions=[],
    )

    result = _process_structured_output(mock_task, parsed, 123456.0, None)

    assert result["structured_evidence_count"] == 1
    assert len(mock_task.evidence) == 1


def test_build_output_payload_with_lessons_and_next_actions(mock_task):
    """测试 lessons 和 next_actions 被正确传递。"""
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
        lessons=["经验1", "经验2"],
        next_actions=["行动1", "行动2", "行动3"],
    )

    payload = _build_output_payload(
        OutputPayloadContext(
            task=mock_task,
            dry_run=False,
            ok=True,
            message="",
            backend="",
            tool_rounds=0,
            parsed=parsed,
            actual_tools=None,
            structured_evidence_count=0,
            structured_request_count=0,
            created_request_ids=[],
            ignored_tools=[],
            ignored_skills=[],
            artifacts=[],
            tests=[],
            patches=[],
            lessons=["经验1", "经验2"],
            blockers=[],
            next_actions=["行动1", "行动2", "行动3"],
            structured_repair_attempted=False,
            structured_repair_ok=False,
            structured_repair_error="",
            now=123456.0,
        )
    )

    assert payload["lessons"] == ["经验1", "经验2"]
    assert payload["next_actions"] == ["行动1", "行动2", "行动3"]
