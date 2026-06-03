"""LLM: Focused tests for hierarchy scheduler role/tool repair.

函数/模块用途: 验证模型写错 role 或 tool 名时，层级调度器仍能保持 coordinator/leaf 权限边界。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


def test_hierarchy_schedule_infers_coordinator_role_from_tools(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(goal="root", thought="split", plan=["plan"])
    child = manager.create_run(
        goal="child",
        thought="coordinate",
        plan=["plan"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="创建三个 leaf worker 并调度执行。",
                    agent_name="child-01-grandchild-01",
                    role="worker",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
                )
            ],
            apply=True,
        )
    )
    grandchild = manager.load(result.created_run_ids[0])

    assert grandchild.role == "grandchild_coordinator"


def test_hierarchy_schedule_applies_depth_agent_name_prefixes(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(goal="root", thought="split", plan=["plan"], agent_name="root")

    child_result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[HierarchyChildSpec(goal="catalog child", agent_name="catalog-lead", role="coordinator")],
            apply=True,
        )
    )
    child = manager.load(child_result.created_run_ids[0])
    grand_result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[HierarchyChildSpec(goal="product grandchild", agent_name="小傻妞-product-worker", role="worker")],
            apply=True,
        )
    )
    grandchild = manager.load(grand_result.created_run_ids[0])
    great_result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=grandchild.id,
            child_specs=[HierarchyChildSpec(goal="sku great grandchild", agent_name="sku-leaf", role="worker")],
            apply=True,
            max_depth=3,
        )
    )
    great_grandchild = manager.load(great_result.created_run_ids[0])

    assert child.agent_name == "小傻妞-catalog-lead"
    assert child.owner == "小傻妞-catalog-lead"
    assert child_result.items[0].agent_name == "小傻妞-catalog-lead"
    assert grandchild.agent_name == "小小傻妞-product-worker"
    assert grandchild.owner == "小小傻妞-product-worker"
    assert grand_result.items[0].agent_name == "小小傻妞-product-worker"
    assert great_grandchild.agent_name == "小小小傻妞-sku-leaf"
    assert great_grandchild.owner == "小小小傻妞-sku-leaf"
    assert great_result.items[0].agent_name == "小小小傻妞-sku-leaf"


def test_hierarchy_schedule_advances_from_parent_lineage_prefix(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(goal="root", thought="split", plan=["plan"], agent_name="小傻妞-shop-root")

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[HierarchyChildSpec(goal="继续协调页面任务", agent_name="coord-r78", role="coordinator")],
            apply=True,
        )
    )
    child = manager.load(result.created_run_ids[0])

    assert child.agent_name == "小小傻妞-coord-r78"
    assert result.items[0].agent_name == "小小傻妞-coord-r78"


def test_hierarchy_schedule_repairs_bare_lineage_agent_name(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(goal="root", thought="split", plan=["plan"], agent_name="root")
    child = manager.create_run(
        goal="child",
        thought="coordinate",
        plan=["plan"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[HierarchyChildSpec(goal="继续协调页面任务", agent_name="小小傻妞", role="coordinator")],
            apply=True,
        )
    )
    grandchild = manager.load(result.created_run_ids[0])

    assert grandchild.agent_name == "小小傻妞-coordinator-1"
    assert result.items[0].agent_name == "小小傻妞-coordinator-1"


def test_hierarchy_schedule_infers_coordinator_even_with_report_write_tools(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(goal="root", thought="split", plan=["plan"])
    child = manager.create_run(
        goal="child",
        thought="coordinate",
        plan=["plan"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="创建三个 leaf worker，自己只写协调计划和 evidence.json。",
                    agent_name="child-01-grandchild-01",
                    role="worker",
                    allowed_tools=[
                        "schedule_child_subagents",
                        "dispatch_subagents",
                        "inspect_agent_tree",
                        "read_file",
                        "write_file",
                    ],
                )
            ],
            apply=True,
        )
    )
    grandchild = manager.load(result.created_run_ids[0])

    assert grandchild.role == "grandchild_coordinator"
    assert "write_file" in grandchild.allowed_tools


def test_hierarchy_schedule_keeps_write_intent_as_leaf_role(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(goal="root", thought="split", plan=["plan"], extra_write_roots=[str(deliverables)])
    parent = manager.create_run(
        goal="child coordinator",
        thought="coordinate",
        plan=["plan"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=f"实现 normalize_text 并写入 {deliverables}/leaf_outputs/leaf_normalize/solution.py。",
                    agent_name="leaf-normalize",
                    role="worker",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
                )
            ],
            apply=True,
        )
    )
    leaf = manager.load(result.created_run_ids[0])

    assert leaf.role == "leaf_worker"
    assert "write_file" in leaf.allowed_tools
    assert "schedule_child_subagents" in leaf.allowed_tools
    assert "dispatch_subagents" in leaf.allowed_tools


def test_hierarchy_schedule_keeps_orchestration_tools_for_leaf_write_tasks(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    parent = manager.create_run(
        goal="child coordinator",
        thought="coordinate",
        plan=["plan"],
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=f"写入 {deliverables}/leaf_outputs/leaf_text/solution.py 和 test_solution.py。",
                    agent_name="leaf-text",
                    role="worker",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree", "write"],
                )
            ],
            apply=True,
        )
    )
    leaf = manager.load(result.created_run_ids[0])

    assert leaf.role == "leaf_worker"
    assert "write_file" in leaf.allowed_tools
    assert "schedule_child_subagents" in leaf.allowed_tools
    assert "dispatch_subagents" in leaf.allowed_tools
    assert "inspect_agent_tree" in leaf.allowed_tools


def test_hierarchy_schedule_preserves_report_write_tools_for_coordinators(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    parent = manager.create_run(
        goal="root",
        thought="split",
        plan=["plan"],
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="创建 leaf worker 写 solution.py，但当前节点只是 coordinator。",
                    agent_name="arithmetic-lead",
                    role="child_coordinator",
                    allowed_tools=[
                        "schedule_child_subagents",
                        "dispatch_subagents",
                        "inspect_agent_tree",
                        "read_file",
                        "write_file",
                    ],
                )
            ],
            apply=True,
        )
    )
    coordinator = manager.load(result.created_run_ids[0])

    assert coordinator.role == "child_coordinator"
    assert "schedule_child_subagents" in coordinator.allowed_tools
    assert "dispatch_subagents" in coordinator.allowed_tools
    assert "write_file" in coordinator.allowed_tools
    assert coordinator.allowed_write_roots == [coordinator.task_dir, str(deliverables)]
    assert str(deliverables) in coordinator.goal
