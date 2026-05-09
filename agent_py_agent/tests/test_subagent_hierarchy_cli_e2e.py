"""LLM: CLI-level regression tests for subagent hierarchy scheduling and recovery.

函数/模块用途: 用真实 parser/config/SimpleAgent 跑通层级创建和恢复查询，避免能力只在 manager 单测里可用。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.config import load_config
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.cli.parser import build_parser


# LLM: _write_config creates a minimal isolated CLI config for hierarchy E2E tests.
# 函数用途: 写入临时配置，让 CLI 和测试创建的 SimpleAgent 使用同一个 workspace。
def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: "."\n'
        'model_backend: "echo"\n'
        'subagent_workspace: "subs"\n',
        encoding="utf-8",
    )
    return config_path


# LLM: test_subagent_hierarchy_cli_e2e_creates_and_queries_recovery covers user-visible commands.
# 函数用途: 通过 CLI 创建 child runs，再把一个 child 标记阻塞，并用恢复树命令查到它。
def test_subagent_hierarchy_cli_e2e_creates_and_queries_recovery(tmp_path, capsys):
    config_path = _write_config(tmp_path)
    agent = SimpleAgent(load_config(config_path), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="orchestrate", plan=["split"])
    parser = build_parser()

    create_args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "subagents-hierarchy",
            root.id,
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

    blocked = agent.subagents.load(created["created_run_ids"][1])
    blocked.status = "BLOCKED"
    blocked.blockers = ["checker waiting for reporter evidence"]
    agent.subagents.save(blocked)

    recovery_args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "subagents-recovery-tree",
            root.id,
            "--hide-healthy",
            "--json",
        ]
    )
    assert recovery_args.func(recovery_args) == 0
    recovery = json.loads(capsys.readouterr().out)

    assert recovery["root_run_id"] == root.id
    assert recovery["node_count"] == 3
    assert recovery["recovery_candidate_count"] == 1
    assert [item["run_id"] for item in recovery["nodes"]] == [root.id, blocked.id]
    assert recovery["recovery_candidates"][0]["takeover_readiness_ref"]
