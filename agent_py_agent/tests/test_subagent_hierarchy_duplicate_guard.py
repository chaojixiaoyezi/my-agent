"""LLM: regression tests for same-parent subagent domain dedupe.

函数/模块用途: 覆盖真实 E2E 中 root 重复创建 checkout/quality coordinator 的问题。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


# LLM: test_hierarchy_schedule_blocks_duplicate_coordinator_domains covers real shopping E2E double-dispatch.
# 函数用途: 同一个父节点已有 checkout/quality coordinator 后，再创建同域 coordinator 必须被阻断。
def test_hierarchy_schedule_blocks_duplicate_coordinator_domains(tmp_path):
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="shopping root", thought="orchestrate", plan=["plan"])

    first = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="build cart and checkout pages",
                    role="coordinator",
                    agent_name="cart-checkout-coordinator",
                ),
                HierarchyChildSpec(
                    goal="verify shopping pages",
                    role="coordinator",
                    agent_name="quality-coordinator",
                ),
            ],
            apply=True,
        )
    )
    duplicate = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="build checkout page",
                    role="coordinator",
                    agent_name="checkout-coordinator",
                ),
                HierarchyChildSpec(
                    goal="verify all links",
                    role="checker",
                    agent_name="quality-checker",
                ),
            ],
            apply=True,
        )
    )

    assert len(first.created_run_ids) == 2
    assert duplicate.blocked is True
    assert duplicate.reason == "duplicate_child_domain:checkout"
    assert manager.load(root.id).child_ids == first.created_run_ids


# LLM: test_hierarchy_schedule_allows_generic_numbered_checker_siblings protects recovery trees.
# 函数用途: `grand-1/grand-2` 这类泛化编号 checker 不是同业务域重复，不能被同域去重误挡。
def test_hierarchy_schedule_allows_generic_numbered_checker_siblings(tmp_path):
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="orchestrate", plan=["split"])
    child = manager.create_run(
        goal="child one", thought="work", plan=["work"],
        parent_id=root.id, root_id=root.id, depth=1,
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[
                HierarchyChildSpec(goal="grand one", role="checker", agent_name="grand-1"),
                HierarchyChildSpec(goal="grand two", role="checker", agent_name="grand-2"),
            ],
            apply=True,
        )
    )

    assert result.blocked is False
    assert len(result.created_run_ids) == 2
