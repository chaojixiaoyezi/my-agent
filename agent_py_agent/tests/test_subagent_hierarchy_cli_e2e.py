"""LLM: CLI-level regression tests for subagent hierarchy scheduling and recovery.

函数/模块用途: 用真实 parser/config/SimpleAgent 跑通层级创建和恢复查询，避免能力只在 manager 单测里可用。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import load_config
from agent_py_agent.cli.parser import build_parser


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: ""\n'
        f'my_agent_home: "{tmp_path / "home"}"\n'
        'model_backend: "echo"\n'
        'subagent_workspace: "subs"\n',
        encoding="utf-8",
    )
    return config_path


def test_subagent_hierarchy_cli_e2e_creates_and_queries_recovery(tmp_path, capsys):
    config_path = _write_config(tmp_path)
    agent = SimpleAgent(load_config(config_path), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="orchestrate", plan=["split"])
    parser = build_parser()
    created = _create_child_runs(parser, config_path, root.id, capsys)
    _write_hierarchy_contexts(agent, root.id, created["created_run_ids"])
    blocked = _mark_blocked_child(agent, created["created_run_ids"][1])
    recovery = _query_recovery_tree(parser, config_path, root.id, capsys)

    assert recovery["root_run_id"] == root.id
    assert recovery["node_count"] == 3
    assert recovery["recovery_candidate_count"] == 1
    assert [item["run_id"] for item in recovery["nodes"]] == [root.id, blocked.id]
    assert recovery["recovery_candidates"][0]["takeover_readiness_ref"]
    assert recovery["recovery_candidates"][0]["context_bundle_ref"].endswith("context_bundle.json")
    assert recovery["recovery_candidates"][0]["parent_context_bundle_ref"].endswith("context_bundle.json")


def _create_child_runs(parser, config_path: Path, root_id: str, capsys) -> dict[str, object]:
    create_args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "subagents-hierarchy",
            root_id,
            "--child",
            "analyst:reporter-a:write report",
            "--child",
            "reviewer:checker-a:check report",
            "--apply",
            "--json",
        ]
    )
    assert create_args.func(create_args) == 0
    created = json.loads(capsys.readouterr().out)
    assert len(created["created_run_ids"]) == 2
    return created


def _write_hierarchy_contexts(agent: SimpleAgent, root_id: str, run_ids: list[str]) -> None:
    agent.subagents.runner_context.write_execution_context(root_id)
    for run_id in run_ids:
        agent.subagents.runner_context.write_execution_context(run_id)


def _mark_blocked_child(agent: SimpleAgent, run_id: str):
    blocked = agent.subagents.load(run_id)
    blocked.status = "BLOCKED"
    blocked.blockers = ["checker waiting for reporter evidence"]
    agent.subagents.save(blocked)
    return blocked


def _query_recovery_tree(parser, config_path: Path, root_id: str, capsys) -> dict[str, object]:
    recovery_args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "subagents-recovery-tree",
            root_id,
            "--hide-healthy",
            "--json",
        ]
    )
    assert recovery_args.func(recovery_args) == 0
    return json.loads(capsys.readouterr().out)
