from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.subagents.manager import SubAgentManager


def test_create_subagents_rejects_old_replacement_aliases(tmp_path):
    """接管字段只保留 replacement_for_run_ids，避免同一动作多套名字。"""
    agent = SimpleNamespace(
        config=SimpleNamespace(
            enable_subagents=True,
            max_subagents=10,
            subagent_workflow_mode="off",
            access_mode="workspace-write",
        ),
        subagents=SubAgentManager(tmp_path, workspace_root=tmp_path),
        tools=SimpleNamespace(specs=lambda: []),
    )

    result = CreateSubagentsTool(agent).execute({
        "goal": "接管旧任务",
        "replaces_run_ids": ["old-run"],
    })

    assert result.ok is False
    assert "replacement_for_run_ids" in result.output
