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


def test_process_structured_output_records_evidence_packets_and_findings(mock_task):
    """LLM: evidence packets become task facts for parent acceptance."""
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        evidence_packets=[{
            "claim": "关键结论",
            "checked_scope": "scope-a",
            "evidence_refs": ["artifact://evidence-1"],
            "artifact_refs": ["artifact://raw-1"],
            "confidence": 0.8,
        }],
        findings=[{
            "claim": "父级可读结论",
            "evidence_refs": ["artifact://evidence-1"],
            "severity": "P1",
        }],
    )

    result = _process_structured_output(mock_task, parsed, 123456.0, None)

    assert len(result["evidence_packets"]) == 1
    assert len(result["findings"]) == 1
    assert mock_task.evidence_refs == ["artifact://evidence-1"]
    assert mock_task.artifact_refs == ["artifact://raw-1"]


def test_process_structured_output_synthesizes_artifact_evidence_packet(mock_task):
    """LLM: artifact-only runner outputs still become traceable parent-acceptance evidence."""
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        summary="写入 proof.txt",
        artifacts=[{
            "path": "/tmp/proof.txt",
            "kind": "file",
            "summary": "包含精确内容 coordinator-seed-ok",
        }],
        evidence_packets=[],
    )

    result = _process_structured_output(mock_task, parsed, 123456.0, None)

    assert len(result["evidence_packets"]) == 1
    assert result["evidence_packets"][0]["artifact_refs"] == ["/tmp/proof.txt"]
    assert mock_task.artifact_refs == ["/tmp/proof.txt"]
    assert mock_task.evidence_packets[0].claim == "artifact produced: 包含精确内容 coordinator-seed-ok"


def _lessons_payload_context(mock_task, parsed: SubAgentParsedOutput) -> OutputPayloadContext:
    lessons = ["经验1", "经验2"]
    next_actions = ["行动1", "行动2", "行动3"]
    return OutputPayloadContext(
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
        evidence_packets=[],
        findings=[],
        tests=[],
        patches=[],
        lessons=lessons,
        blockers=[],
        next_actions=next_actions,
        structured_repair_attempted=False,
        structured_repair_ok=False,
        structured_repair_error="",
        now=123456.0,
    )


def test_build_output_payload_with_lessons_and_next_actions(mock_task):
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

    payload = _build_output_payload(_lessons_payload_context(mock_task, parsed))

    assert payload["lessons"] == ["经验1", "经验2"]
    assert payload["next_actions"] == ["行动1", "行动2", "行动3"]
