"""schedule_child_subagents runner-context regression tests."""
from __future__ import annotations

import json

from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


# LLM: test_runner_context_schedule_bare_lineage_name_returns_payload_not_index_error guards R32 prefix-only names.
# 函数用途: 验证真实模型只写“小小傻妞”这类层级前缀时，runner 工具会补稳定后缀并返回 JSON，而不是抛裸 IndexError。
def test_runner_context_schedule_bare_lineage_name_returns_payload_not_index_error(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
    child = agent.subagents.create_run(
        goal="child",
        thought="child",
        plan=["child"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
    )
    agent._current_subagent_run_id = child.id
    tool = ScheduleChildSubagentsTool(agent)

    result = tool.execute(
        {
            "orchestration": {
                "apply": True,
                "max_depth": 2,
                "children": [{"goal": "继续协调页面任务", "role": "coordinator", "agent_name": "小小傻妞"}],
            },
        }
    )
    payload = json.loads(result.output)

    assert result.ok is True
    assert payload["created_run_ids"]
    assert agent.subagents.load(payload["created_run_ids"][0]).agent_name == "小小傻妞-coordinator"
