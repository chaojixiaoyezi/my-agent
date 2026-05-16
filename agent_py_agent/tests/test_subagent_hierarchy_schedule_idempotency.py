"""Focused tests for schedule_child_subagents idempotency contracts."""

from __future__ import annotations

import json

from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


# LLM: _schedule_one keeps idempotency tests focused on contract fields instead of setup.
# 函数用途: 对同一 parent 调用 schedule_child_runs 创建一个 worker child，用于检查重复调度是否复用。
def _schedule_one(manager: SubAgentManager, parent_id: str, goal: str, *, role: str = "worker"):
    return manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent_id,
            child_specs=[HierarchyChildSpec(goal=goal, role=role)],
            apply=True,
        )
    )


# LLM: repeated same-contract schedule calls should reuse the direct child run.
# 函数用途: 同一父级、同一目标、同一角色、同一写入根重复调度时，不能继续扩容小小傻妞。
def test_schedule_child_reuses_same_contract_without_growing_tree(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(goal="root", thought="root", plan=["root"])
    parent = manager.create_run(goal="parent", thought="parent", plan=["parent"], parent_id=root.id, root_id=root.id)

    first = _schedule_one(manager, parent.id, "写商品列表页面")
    second = _schedule_one(manager, parent.id, "写商品列表页面")

    assert len(first.created_run_ids) == 1
    assert second.created_run_ids == []
    assert second.reused_run_ids == first.created_run_ids
    assert second.dispatch_run_ids == first.created_run_ids
    assert manager.load(parent.id).child_ids == first.created_run_ids
    assert second.items[0].reason == "reused"


# LLM: default lineage display names are not enough to merge different child jobs.
# 函数用途: 两个默认 worker 都可能叫小小傻妞-worker-1；目标不同必须创建新 run，不能误复用。
def test_schedule_child_does_not_reuse_different_goal_with_same_default_name(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(goal="root", thought="root", plan=["root"])
    parent = manager.create_run(goal="parent", thought="parent", plan=["parent"], parent_id=root.id, root_id=root.id)

    first = _schedule_one(manager, parent.id, "写商品列表页面")
    second = _schedule_one(manager, parent.id, "写购物车页面")

    assert len(first.created_run_ids) == 1
    assert len(second.created_run_ids) == 1
    assert second.reused_run_ids == []
    assert manager.load(parent.id).child_ids == [*first.created_run_ids, *second.created_run_ids]


# LLM: reused completed children should be visible but not suggested for dispatch.
# 函数用途: 已完成的小小傻妞重复调度时返回 reused_run_ids，但 dispatch_run_ids 为空，避免重复跑。
def test_schedule_child_reuses_verified_child_without_dispatching(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(goal="root", thought="root", plan=["root"])
    parent = manager.create_run(goal="parent", thought="parent", plan=["parent"], parent_id=root.id, root_id=root.id)
    first = _schedule_one(manager, parent.id, "写商品列表页面")
    child = manager.load(first.created_run_ids[0])
    child.status = "DONE"
    child.verification_status = "VERIFIED"
    manager.save(child)

    second = _schedule_one(manager, parent.id, "写商品列表页面")

    assert second.created_run_ids == []
    assert second.reused_run_ids == first.created_run_ids
    assert second.dispatch_run_ids == []
    assert second.items[0].reason == "reused"


# LLM: tool payload must expose reused/dispatch ids so parent runners do not guess from prose.
# 函数用途: runner 工具重复调度时，JSON 输出应包含 reused_run_ids 和 dispatch_run_ids，供下一步调度直接使用。
def test_schedule_child_tool_payload_exposes_reused_and_dispatch_ids(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
    parent = agent.subagents.create_run(
        goal="parent",
        thought="parent",
        plan=["parent"],
        parent_id=root.id,
        root_id=root.id,
        role="coordinator",
    )
    agent._current_subagent_run_id = parent.id
    tool = ScheduleChildSubagentsTool(agent)
    params = {"apply": True, "children": [{"goal": "写商品列表页面", "role": "worker"}]}

    first = json.loads(tool.execute(params).output)
    second = json.loads(tool.execute(params).output)

    assert first["created_run_ids"]
    assert second["created_run_ids"] == []
    assert second["reused_run_ids"] == first["created_run_ids"]
    assert second["dispatch_run_ids"] == first["created_run_ids"]
