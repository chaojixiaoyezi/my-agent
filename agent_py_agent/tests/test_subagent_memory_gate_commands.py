from __future__ import annotations

"""LLM: focused tests for subagent memory-gate CLI modes."""

from pathlib import Path
from unittest.mock import MagicMock, patch


def _base_args(tmp_path: Path) -> MagicMock:
    args = MagicMock()
    args.config = str(tmp_path / "config.yaml")
    args.run_id = "run_001"
    args.candidate_id = ""
    args.decision = "needs_evidence"
    args.reviewer = "parent"
    args.note = ""
    args.memory_path = ""
    args.skill_output_dir = None
    args.limit = 10
    args.retention_dry_run = False
    args.retention_apply = False
    args.export_memory = False
    args.export_skill = False
    args.verify = False
    return args


def test_cmd_subagents_memory_gate_lists_candidates(tmp_path: Path, capsys):
    """不传 candidate_id 时只列出 gate 候选。"""
    from agent_py_agent.cli.subagents import cmd_subagents_memory_gate

    args = _base_args(tmp_path)
    mock_agent = MagicMock()
    mock_agent.subagents.list_memory_gate_candidates.return_value = [
        {
            "candidate_id": "memgate-run_001-abc",
            "candidate_type": "skill_spark",
            "review_status": "pending",
            "promotion_status": "not_promoted",
            "content": "先核验证据链",
        }
    ]

    with patch("agent_py_agent.cli._memory_gate.make_agent", return_value=mock_agent):
        result = cmd_subagents_memory_gate(args)

    output = capsys.readouterr().out
    assert result == 0
    assert "SUBAGENT MEMORY GATE" in output
    assert "memgate-run_001-abc" in output


def test_cmd_subagents_memory_gate_records_review(tmp_path: Path, capsys):
    """传 candidate_id 时写回 review decision，但不执行提升。"""
    from agent_py_agent.cli.subagents import cmd_subagents_memory_gate

    args = _base_args(tmp_path)
    args.candidate_id = "memgate-run_001-abc"
    args.decision = "approve_memory"
    args.note = "证据已核验"
    mock_result = MagicMock()
    mock_result.candidate = {
        "candidate_id": "memgate-run_001-abc",
        "review_decision": "approve_memory",
        "promotion_status": "approved_for_memory_export",
    }
    mock_result.decisions_jsonl = tmp_path / "decisions.jsonl"
    mock_result.skill_spark_gate_json = tmp_path / "skill_spark_gate.json"
    mock_agent = MagicMock()
    mock_agent.subagents.review_memory_gate_candidate.return_value = mock_result

    with patch("agent_py_agent.cli._memory_gate.make_agent", return_value=mock_agent):
        result = cmd_subagents_memory_gate(args)

    output = capsys.readouterr().out
    assert result == 0
    assert "SUBAGENT MEMORY GATE REVIEW" in output
    assert "approved_for_memory_export" in output


def test_cmd_subagents_memory_gate_exports_memory(tmp_path: Path, capsys):
    """export-memory 只调用显式导出路径。"""
    from agent_py_agent.cli.subagents import cmd_subagents_memory_gate

    args = _base_args(tmp_path)
    args.candidate_id = "memgate-run_001-abc"
    args.memory_path = str(tmp_path / "memory.jsonl")
    args.export_memory = True
    mock_result = MagicMock()
    mock_result.exported_count = 1
    mock_result.skipped_count = 0
    mock_result.exports_jsonl = tmp_path / "exports.jsonl"
    mock_agent = MagicMock()
    mock_agent.subagents.export_memory_gate_candidates_to_memory.return_value = mock_result

    with patch("agent_py_agent.cli._memory_gate.make_agent", return_value=mock_agent):
        result = cmd_subagents_memory_gate(args)

    output = capsys.readouterr().out
    assert result == 0
    assert "SUBAGENT MEMORY GATE EXPORT MEMORY" in output
    assert "exported=1" in output


def test_cmd_subagents_memory_gate_retention_and_verify(tmp_path: Path, capsys):
    """retention 和 verify 都是显式 mode。"""
    from agent_py_agent.cli.subagents import cmd_subagents_memory_gate

    args = _base_args(tmp_path)
    args.retention_dry_run = True
    mock_retention = MagicMock()
    mock_retention.report = {"mode": "dry_run", "planned_action_count": 1, "active_after_count": 0}
    mock_retention.report_json = tmp_path / "retention_report.json"
    mock_retention.actions_jsonl = tmp_path / "retention_actions.jsonl"
    mock_agent = MagicMock()
    mock_agent.subagents.run_memory_gate_retention.return_value = mock_retention

    with patch("agent_py_agent.cli._memory_gate.make_agent", return_value=mock_agent):
        assert cmd_subagents_memory_gate(args) == 0

    args.retention_dry_run = False
    args.verify = True
    mock_verify = MagicMock()
    mock_verify.ok = True
    mock_verify.report = {"problem_count": 0}
    mock_verify.report_json = tmp_path / "verifier_report.json"
    mock_agent.subagents.verify_memory_gate_boundary.return_value = mock_verify

    with patch("agent_py_agent.cli._memory_gate.make_agent", return_value=mock_agent):
        assert cmd_subagents_memory_gate(args) == 0

    output = capsys.readouterr().out
    assert "SUBAGENT MEMORY GATE RETENTION" in output
    assert "SUBAGENT MEMORY GATE VERIFY" in output
