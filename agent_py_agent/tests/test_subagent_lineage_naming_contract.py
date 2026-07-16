"""Focused tests for default subagent display-name contracts."""

from __future__ import annotations

import json


def _create_agent(tmp_path):
    from unittest.mock import MagicMock

    from agent_py_agent.agent.subagents.manager import SubAgentManager

    agent = MagicMock()
    agent.config.enable_subagents = True
    agent.config.max_subagents = 10
    agent.config.subagent_workflow_mode = "off"
    agent.subagents = SubAgentManager(tmp_path / ".my-agent" / "subagents", workspace_root=tmp_path)
    return agent


def test_top_level_items_get_fixed_lineage_names(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _create_agent(tmp_path)
    payload = json.loads(CreateSubagentsTool(agent).execute({
        "goal": "并行编写并测试首页",
        "items": [
            {"goal": "写首页", "role": "worker"},
            {"goal": "测试首页", "role": "tester"},
        ]
    }).output)

    names = [agent.subagents.load(run_id).agent_name for run_id in payload["created_run_ids"]]
    assert names == ["agent-d1-worker-1", "agent-d1-tester-2"]


def test_count_fanout_gets_fixed_lineage_names(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent = _create_agent(tmp_path)
    payload = json.loads(CreateSubagentsTool(agent).execute({
        "goal": "分别做两个页面",
        "count": 2,
        "role": "worker",
    }).output)

    names = [agent.subagents.load(run_id).agent_name for run_id in payload["created_run_ids"]]
    assert names == ["agent-d1-worker-1", "agent-d1-worker-2"]


def test_scheduled_children_get_structured_depth_name_and_index(tmp_path):
    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
        HierarchyChildSpec,
        HierarchyScheduleRequest,
    )

    manager = SubAgentManager(tmp_path / "subs")
    parent = manager.create_run(
        goal="父级任务",
        thought="root",
        plan=["plan"],
        agent_name="小傻妞-coordinator-1",
        role="coordinator",
        depth=1,
    )
    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(goal="写页面", role="worker"),
                HierarchyChildSpec(goal="测页面", role="tester"),
            ],
            apply=True,
        )
    )

    names = [manager.load(run_id).agent_name for run_id in result.created_run_ids]
    assert names == ["agent-d2-worker-1", "agent-d2-tester-2"]
