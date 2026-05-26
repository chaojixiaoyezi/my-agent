"""result_processors 边界场景测试。"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.subagents.models import SubAgentParsedOutput
from agent_py_agent.agent.subagents.parsing import parse_subagent_runner_output
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
    """LLM: evidence packets become task facts for closeout."""
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


# LLM: coverage records are the machine-readable replacement for parent prose like “sibling covered it”.
# 函数用途: 验证 runner 输出的 coverage_records 会被解析并持久到 task.attributes，供验收和收口链路读取。
def test_process_structured_output_records_coverage_records(mock_task):
    text = """
    [SUBAGENT_RESULT]
    {
      "status": "DONE",
      "coverage_records": [
        {
          "covered_run_id": "bad-leaf",
          "covered_by_run_id": "good-leaf",
          "reason": "good leaf produced the same market section",
          "artifact_refs": ["artifact://good-report"]
        }
      ]
    }
    [/SUBAGENT_RESULT]
    """
    parsed = parse_subagent_runner_output(text)
    mock_task.attributes = {}

    _process_structured_output(mock_task, parsed, 123456.0, None)

    assert parsed.coverage_records[0]["covered_run_id"] == "bad-leaf"
    assert mock_task.attributes["coverage_records"] == [{
        "covered_run_id": "bad-leaf",
        "covered_by_run_id": "good-leaf",
        "reason": "good leaf produced the same market section",
        "artifact_refs": ["artifact://good-report"],
        "evidence_refs": [],
    }]


# LLM: negative content-check evidence must use explicit schema rather than summary prose.
# 函数用途: 子代理用 match_mode=not_contains 表示坏模式不存在时，最终收口按负向检查通过。
def test_process_structured_output_normalizes_explicit_absent_pattern_evidence(mock_task):
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        evidence=[{
            "kind": "content_check",
            "summary": "页脚链接无空 href=\"#\"",
            "path": "/tmp/index.html",
            "content_pattern": "<a href=\"#\">",
            "match_mode": "not_contains",
            "ok": False,
        }],
    )

    result = _process_structured_output(mock_task, parsed, 123456.0, None)

    assert result["structured_evidence_count"] == 1
    assert mock_task.evidence[0].ok is True


# LLM: prose-only negative summaries are not enough to flip evidence state.
# 函数用途: 防止代码层把“没有/无/不存在”这类自然语言摘要误当成业务验收合同。
def test_process_structured_output_does_not_invert_natural_absent_summary(mock_task):
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        evidence=[{
            "kind": "content_check",
            "summary": "页脚链接无空 href=\"#\"",
            "path": "/tmp/index.html",
            "content_pattern": "<a href=\"#\">",
            "ok": False,
        }],
    )

    result = _process_structured_output(mock_task, parsed, 123456.0, None)

    assert result["structured_evidence_count"] == 1
    assert mock_task.evidence[0].ok is False


def test_process_structured_output_synthesizes_artifact_evidence_packet(mock_task):
    """LLM: artifact-only runner outputs still become traceable closeout evidence."""
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


# LLM: short artifact paths from real runners should become durable refs before parent closeout.
# 函数用途: 覆盖 coordinator 输出 market_synthesis_report.md 这类短路径时，任务状态保存真实文件路径。
def test_process_structured_output_normalizes_relative_artifact_refs(mock_task, tmp_path):
    task_dir = tmp_path / "subagent-run"
    task_dir.mkdir()
    artifact = task_dir / "market_synthesis_report.md"
    artifact.write_text("report", encoding="utf-8")
    mock_task.task_dir = str(task_dir)
    mock_task.output_dir = str(task_dir / "output")
    mock_task.reports_dir = str(task_dir / "reports")
    mock_task.task_workspace_artifacts_dir = ""
    mock_task.agent_run_artifacts_dir = ""
    mock_task.data_dir = ""
    mock_task.scratch_dir = ""
    mock_task.allowed_write_roots = []
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        artifacts=[{"path": "market_synthesis_report.md", "kind": "report", "summary": "market"}],
    )

    result = _process_structured_output(mock_task, parsed, 123456.0, None)

    assert result["artifacts"][0]["path"] == str(artifact)
    assert result["evidence_packets"][0]["artifact_refs"] == [str(artifact)]
    assert mock_task.artifact_refs == [str(artifact)]


# LLM: model-provided evidence packet refs need the same path normalization as artifacts.
# 函数用途: 覆盖 evidence_packets.artifact_refs 直接写短路径时，父级 closeout 不再出现相对/绝对混用。
def test_process_structured_output_normalizes_evidence_packet_artifact_refs(mock_task, tmp_path):
    task_dir = tmp_path / "subagent-run"
    output_dir = task_dir / "output"
    output_dir.mkdir(parents=True)
    artifact = output_dir / "entry_strategy.md"
    artifact.write_text("strategy", encoding="utf-8")
    mock_task.task_dir = str(task_dir)
    mock_task.output_dir = str(output_dir)
    mock_task.reports_dir = str(task_dir / "reports")
    mock_task.task_workspace_artifacts_dir = ""
    mock_task.agent_run_artifacts_dir = ""
    mock_task.data_dir = ""
    mock_task.scratch_dir = ""
    mock_task.allowed_write_roots = []
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        evidence_packets=[{
            "claim": "策略文件已完成",
            "checked_scope": "output",
            "artifact_refs": ["output/entry_strategy.md"],
            "confidence": 0.9,
        }],
    )

    result = _process_structured_output(mock_task, parsed, 123456.0, None)

    assert result["evidence_packets"][0]["artifact_refs"] == [str(artifact)]
    assert mock_task.artifact_refs == [str(artifact)]


# LLM: Real hierarchy runs may write deliverables beside run metadata, not only inside artifacts/.
# 函数用途: 覆盖子代理把报告写在 agent_run_workspace 根目录、evidence packet 只报短路径时，验收不再误判 missing_artifact_refs。
def test_process_structured_output_resolves_refs_from_agent_run_workspace(mock_task, tmp_path):
    task_dir = tmp_path / "legacy-task"
    run_workspace = tmp_path / "tasks" / "parent" / "agents" / "child"
    run_workspace.mkdir(parents=True)
    artifact = run_workspace / "thailand_analysis.md"
    artifact.write_text("analysis", encoding="utf-8")
    mock_task.task_dir = str(task_dir)
    mock_task.output_dir = str(task_dir / "output")
    mock_task.reports_dir = str(task_dir / "reports")
    mock_task.agent_run_workspace_dir = str(run_workspace)
    mock_task.task_workspace_artifacts_dir = str(tmp_path / "tasks" / "parent" / "artifacts")
    mock_task.agent_run_artifacts_dir = str(run_workspace / "artifacts")
    mock_task.task_workspace_shared_dir = str(tmp_path / "tasks" / "parent" / "shared")
    mock_task.task_workspace_dir = str(tmp_path / "tasks" / "parent")
    mock_task.data_dir = ""
    mock_task.scratch_dir = ""
    mock_task.allowed_write_roots = []
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        artifacts=[{"path": str(artifact), "kind": "report"}],
        evidence_packets=[{
            "claim": "泰国分析报告已完成",
            "checked_scope": "thailand_analysis.md",
            "artifact_refs": ["thailand_analysis.md"],
            "confidence": 0.95,
        }],
    )

    result = _process_structured_output(mock_task, parsed, 123456.0, None)

    assert parsed.status == "DONE"
    assert parsed.failure_type == ""
    assert result["evidence_packets"][0]["artifact_refs"] == [str(artifact)]
    assert mock_task.artifact_refs == [str(artifact)]


# LLM: coordinator parent refs should prefer child task artifact_refs over guessed child paths.
# 函数用途: 复现真实 E2E 中父级把 child 产物路径猜错，但 child task.json 里已有真实 artifact_refs 的场景。
def test_process_structured_output_resolves_guessed_child_artifact_refs(mock_task, tmp_path):
    subagents_root = tmp_path / "subagents"
    parent_dir = subagents_root / "parent-run"
    child_id = "subagent-child-1"
    child_dir = subagents_root / child_id
    actual = parent_dir / "grandchild_direct_competitors" / "reports" / "direct_competitors_research.md"
    actual.parent.mkdir(parents=True)
    actual.write_text("direct competitors", encoding="utf-8")
    child_dir.mkdir(parents=True)
    (child_dir / "task.json").write_text(
        json.dumps({"id": child_id, "artifact_refs": [str(actual)]}, ensure_ascii=False),
        encoding="utf-8",
    )
    mock_task.task_dir = str(parent_dir)
    mock_task.output_dir = str(parent_dir / "output")
    mock_task.reports_dir = str(parent_dir / "reports")
    mock_task.agent_run_workspace_dir = ""
    mock_task.task_workspace_artifacts_dir = ""
    mock_task.agent_run_artifacts_dir = ""
    mock_task.task_workspace_shared_dir = ""
    mock_task.task_workspace_dir = ""
    mock_task.data_dir = ""
    mock_task.scratch_dir = ""
    mock_task.allowed_write_roots = []
    mock_task.child_ids = [child_id]
    guessed = subagents_root / child_id / "reports" / "direct_competitors_research.md"
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        evidence_packets=[{
            "claim": "直接竞争对手研究已完成",
            "checked_scope": "child artifact refs",
            "artifact_refs": [str(guessed)],
            "confidence": 0.9,
        }],
    )

    result = _process_structured_output(mock_task, parsed, 123456.0, None)

    assert parsed.status == "DONE"
    assert parsed.failure_type == ""
    assert result["evidence_packets"][0]["artifact_refs"] == [str(actual)]
    assert mock_task.artifact_refs == [str(actual)]


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
