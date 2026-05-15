"""LLM: Focused tests for schedule_child_subagents permissive batch behavior.

函数/模块用途: 验证 runner 工具入口默认不再用固定 child 数限制阻断正常派工。
"""

from __future__ import annotations

import json

from agent_py_agent.agent.action_protocol import SubagentScheduleEnvelope, decode_action_envelope
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


# LLM: test_schedule_tool_allows_three_child_batches_by_default covers unrestricted default fan-out.
# 函数用途: 默认不再因为单次 3 个 child 阻断；真实长 JSON 风险交给结构化协议和调试测试处理。
def test_schedule_tool_allows_three_child_batches_by_default(tmp_path):
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

    payload = json.loads(result.output)

    assert result.ok is True
    assert len(payload["created_run_ids"]) == 3
    assert agent.subagents.load(root.id).child_ids == payload["created_run_ids"]


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
    envelope = decode_action_envelope(payload["typed_envelope"])
    assert isinstance(envelope, SubagentScheduleEnvelope)
    assert envelope.parent_run_id == root.id
    assert envelope.created_run_ids == payload["created_run_ids"]
