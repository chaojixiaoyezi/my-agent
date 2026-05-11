"""LLM: Focused tests for schedule_child_subagents real-model batch limits.

函数/模块用途: 验证 runner 工具入口会拒绝过大的 child 批次，避免长 JSON 调用被模型截断后卡死。
"""

from __future__ import annotations

import json

from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


# LLM: _tool_agent builds a real SimpleAgent so the guard is tested at the callable tool boundary.
# 函数用途: 创建带当前 runner 上下文的测试 agent，并返回 schedule_child_subagents 工具和 root run。
def _tool_agent(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
    agent._current_subagent_run_id = root.id
    return agent, root, ScheduleChildSubagentsTool(agent)


# LLM: test_schedule_tool_rejects_three_child_batches covers the R21 truncation failure mode.
# 函数用途: 单次创建 3 个 child 时工具应明确失败，并要求模型拆成更小批次。
def test_schedule_tool_rejects_three_child_batches(tmp_path):
    agent, root, tool = _tool_agent(tmp_path)

    result = tool.execute(
        {
            "children": [
                {"goal": "写 index.html", "role": "leaf_worker", "agent_name": "index"},
                {"goal": "写 cart.html", "role": "leaf_worker", "agent_name": "cart"},
                {"goal": "写 checkout.html", "role": "leaf_worker", "agent_name": "checkout"},
            ]
        }
    )

    assert result.ok is False
    assert "单次最多创建 2 个 child" in result.output
    assert agent.subagents.load(root.id).child_ids == []


# LLM: test_schedule_tool_allows_two_child_batches keeps useful parallel fan-out available.
# 函数用途: 允许每次 1-2 个 child 的小批量调度，模型仍可通过多次调用并发推进。
def test_schedule_tool_allows_two_child_batches(tmp_path):
    agent, root, tool = _tool_agent(tmp_path)

    result = tool.execute(
        {
            "children": [
                {"goal": "写 index.html", "role": "leaf_worker", "agent_name": "index"},
                {"goal": "写 cart.html", "role": "leaf_worker", "agent_name": "cart"},
            ]
        }
    )
    payload = json.loads(result.output)

    assert result.ok is True
    assert len(payload["created_run_ids"]) == 2
    assert agent.subagents.load(root.id).child_ids == payload["created_run_ids"]
