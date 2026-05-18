from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.__main__ import build_parser
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagent import parse_subagent_runner_output
from agent_py_agent.agent.subagents.manager_runner_results import RecordRunnerResultParams


def _rrr(run_id: str, **kwargs) -> RecordRunnerResultParams:
    """Helper to create RecordRunnerResultParams with run_id as positional arg."""
    return RecordRunnerResultParams(run_id=run_id, **kwargs)


def _write_config(root: Path, *, enable_self_learning: bool) -> Path:
    config_path = root / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: "."\n'
        'model_backend: "echo"\n'
        'subagent_workspace: "subs"\n'
        f'enable_self_learning: {"true" if enable_self_learning else "false"}\n',
        encoding="utf-8",
    )
    return config_path


# ── Shared fixture builders ──────────────────────────────────────────────────

def _parsed_output_with_lessons(lesson_text: str) -> SubAgentParsedOutput:
    return parse_subagent_runner_output(
        "[SUBAGENT_RESULT]\n"
        "{\n"
        f'  "status": "AWAITING_ACCEPTANCE",\n'
        f'  "summary": "整理出 lesson",\n'
        f'  "lessons": [{json.dumps(lesson_text)}],\n'
        f'  "evidence": [{{"kind": "note", "summary": "有 lesson", "ok": true}}],\n'
        '  "artifacts": [],\n'
        '  "tests": [],\n'
        '  "patches": []\n'
        "}\n"
        "[/SUBAGENT_RESULT]"
    )


def _sample_parsed_output_with_lesson() -> SubAgentParsedOutput:
    return parse_subagent_runner_output(
        "[SUBAGENT_RESULT]\n"
        "{\n"
        '  "status": "AWAITING_ACCEPTANCE",\n'
        '  "summary": "生成 lesson",\n'
        '  "lessons": ["先读现有测试，再补最小回归，再改实现"],\n'
        '  "evidence": [{"kind": "note", "summary": "有 lesson", "ok": true}],\n'
        '  "artifacts": [],\n'
        '  "tests": [],\n'
        '  "patches": []\n'
        "}\n"
        "[/SUBAGENT_RESULT]"
    )


def _create_agent_with_learning(tmp_path: Path) -> SimpleAgent:
    return SimpleAgent(
        AgentConfig(
            model_backend="echo",
            subagent_workspace="subs",
            enable_self_learning=True,
        ),
        tmp_path,
    )


def _run_and_record_lesson(agent: SimpleAgent, goal: str, thought: str, lesson_text: str):
    task = agent.subagents.create_run(goal=goal, thought=thought, plan=["执行", "沉淀 lesson"])
    parsed = _parsed_output_with_lessons(lesson_text)
    agent.subagents.record_runner_result(_rrr(task.id, dry_run=False, ok=True, message="done", structured_output=parsed))
    return task


def _setup_learning_candidate(agent: SimpleAgent, config_path: Path) -> tuple:
    task = _run_and_record_lesson(agent, "产出 learning draft", "记录 lesson。", "先读现有测试，再补最小回归，再改实现")
    return agent.subagents.list_learning_candidates()[0]


def test_record_runner_result_generates_and_dedupes_learning_candidates(tmp_path):
    agent = _create_agent_with_learning(tmp_path)
    first = _run_and_record_lesson(agent, "总结 lessons", "记录经验。", "先写失败复现，再改代码，最后补最小回归测试")
    second = _run_and_record_lesson(agent, "再次总结 lessons", "复用同一条经验。", "先复现失败，再改代码，最后补一个最小回归测试")

    candidates = agent.subagents.list_learning_candidates()
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.status == "draft"
    assert candidate.occurrence_count == 2
    assert candidate.confidence > 0.45
    assert candidate.source_runs == [first.id, second.id]
    assert candidate.evidence_count == 2
    assert candidate.evidence[0]["run_id"] == first.id
    assert candidate.evidence[0]["output_json"].endswith("output.json")
    assert (tmp_path / "data" / "learning_drafts" / f"{candidate.id}.json").exists()


def test_learn_cli_lists_accepts_rejects_and_reports_stats(tmp_path, capsys):
    config_path = _write_config(tmp_path, enable_self_learning=True)
    agent = _create_agent_with_learning(tmp_path)
    candidate = _setup_learning_candidate(agent, config_path)
    parser = build_parser()

    args = parser.parse_args(["--config", str(config_path), "learn", "list"])
    assert args.func(args) == 0
    output = capsys.readouterr().out
    assert "LEARNING DRAFTS" in output
    assert candidate.id in output

    args = parser.parse_args(["--config", str(config_path), "learn", "accept", candidate.id])
    assert args.func(args) == 0
    output = capsys.readouterr().out
    assert f"accepted {candidate.id}" in output
    assert "未生成 SKILL.md" in output
    assert agent.subagents.load_learning_candidate(candidate.id).status == "accepted"
    assert not list((tmp_path / "data" / "learning_drafts").glob("**/SKILL.md"))

    args = parser.parse_args(["--config", str(config_path), "learn", "stats", "--json"])
    assert args.func(args) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["total"] == 1
    assert stats["accepted"] == 1
    assert stats["draft"] == 0

    args = parser.parse_args(["--config", str(config_path), "learn", "reject", candidate.id])
    assert args.func(args) == 0
    output = capsys.readouterr().out
    assert f"rejected {candidate.id}" in output
    assert agent.subagents.load_learning_candidate(candidate.id).status == "rejected"


def test_learn_cli_requires_enable_self_learning(tmp_path, capsys):
    config_path = _write_config(tmp_path, enable_self_learning=False)
    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), "learn", "list"])

    assert args.func(args) == 2
    assert "enable_self_learning=false" in capsys.readouterr().err
