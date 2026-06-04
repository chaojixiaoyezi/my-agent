"""Focused tests for schedule_child_subagents idempotency contracts."""

from __future__ import annotations

import json

from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


def _schedule_one(manager: SubAgentManager, parent_id: str, goal: str, *, key: str = ""):
    return manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent_id,
            child_specs=[HierarchyChildSpec(goal=goal, role="worker", context_packs=_packs(key))],
            apply=True,
        )
    )


def test_schedule_child_reuses_explicit_idempotency_contract_without_growing_tree(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(goal="root", thought="root", plan=["root"])
    parent = manager.create_run(goal="parent", thought="parent", plan=["parent"], parent_id=root.id, root_id=root.id)

    first = _schedule_one(manager, parent.id, "写条目列表页面", key="product-list")
    second = _schedule_one(manager, parent.id, "继续条目列表页面", key="product-list")

    assert len(first.created_run_ids) == 1
    assert second.created_run_ids == []
    assert second.reused_run_ids == first.created_run_ids
    assert second.dispatch_run_ids == first.created_run_ids
    assert manager.load(parent.id).child_ids == first.created_run_ids
    assert second.items[0].reason == "reused"


def test_schedule_child_does_not_reuse_different_goal_with_same_default_name(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(goal="root", thought="root", plan=["root"])
    parent = manager.create_run(goal="parent", thought="parent", plan=["parent"], parent_id=root.id, root_id=root.id)

    first = _schedule_one(manager, parent.id, "写条目列表页面", key="product-list")
    second = _schedule_one(manager, parent.id, "写流程状态页面")

    assert len(first.created_run_ids) == 1
    assert len(second.created_run_ids) == 1
    assert second.reused_run_ids == []
    assert manager.load(parent.id).child_ids == [*first.created_run_ids, *second.created_run_ids]


def test_schedule_child_reuses_verified_child_without_dispatching(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(goal="root", thought="root", plan=["root"])
    parent = manager.create_run(goal="parent", thought="parent", plan=["parent"], parent_id=root.id, root_id=root.id)
    first = _schedule_one(manager, parent.id, "写条目列表页面", key="product-list")
    child = manager.load(first.created_run_ids[0])
    child.status = "DONE"
    child.verification_status = "VERIFIED"
    manager.save(child)

    second = _schedule_one(manager, parent.id, "继续条目列表页面", key="product-list")

    assert second.created_run_ids == []
    assert second.reused_run_ids == first.created_run_ids
    assert second.dispatch_run_ids == []
    assert second.items[0].reason == "reused"


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
    params = {
        "dry_run": False,
        "children": [{"goal": "写条目列表页面", "role": "worker", "context_packs": _packs("product-list")}],
    }

    first = json.loads(tool.execute(params).output)
    second = json.loads(tool.execute(params).output)

    assert first["created_run_ids"]
    assert second["created_run_ids"] == []
    assert second["reused_run_ids"] == first["created_run_ids"]
    assert second["dispatch_run_ids"] == first["created_run_ids"]


def _packs(key: str) -> list[dict[str, object]]:
    if not key:
        return []
    return [{
        "kind": "idempotency_contract",
        "contract": {
            "schema": "subagent_idempotency_contract.v1",
            "kind": "schedule-test",
            "idempotency_key": key,
            "scope_refs": [key],
        },
    }]
