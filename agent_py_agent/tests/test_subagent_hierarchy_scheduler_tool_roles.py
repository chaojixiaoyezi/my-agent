"""LLM: Focused tests for hierarchy scheduler role/tool repair.

函数/模块用途: 验证模型写错 role 或 tool 名时，层级调度器仍能保持 coordinator/leaf 权限边界。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


# LLM: test_hierarchy_schedule_infers_coordinator_role_from_tools keeps model role slips recoverable.
# 函数用途: 当模型把带层级调度权限的下一层误写成 worker 时，系统按 depth 纠正为 coordinator。
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
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
                )
            ],
            apply=True,
        )
    )
    grandchild = manager.load(result.created_run_ids[0])

    assert grandchild.role == "grandchild_coordinator"


# LLM: test_hierarchy_schedule_keeps_write_intent_as_leaf_role covers real model over-granting scheduler tools.
# 函数用途: 当模型给叶子写文件任务也带了调度工具时，系统仍按交付物写入意图归一成 leaf_worker。
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
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
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
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
                )
            ],
            apply=True,
        )
    )
    leaf = manager.load(result.created_run_ids[0])

    assert leaf.role == "leaf_worker"
    assert "write_file" in leaf.allowed_tools
    assert "schedule_child_subagents" not in leaf.allowed_tools
    assert "dispatch_subagents" not in leaf.allowed_tools


# LLM: test_hierarchy_schedule_strips_orchestration_tools_from_leaf_write_tasks prevents leaf recursion.
# 函数用途: 当模型给叶子交付任务误带调度工具时，系统去掉调度权限，避免 leaf 再创建孩子。
def test_hierarchy_schedule_strips_orchestration_tools_from_leaf_write_tasks(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    parent = manager.create_run(
        goal="child coordinator",
        thought="coordinate",
        plan=["plan"],
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
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
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board", "write"],
                )
            ],
            apply=True,
        )
    )
    leaf = manager.load(result.created_run_ids[0])

    assert leaf.role == "leaf_worker"
    assert "write_file" in leaf.allowed_tools
    assert "schedule_child_subagents" not in leaf.allowed_tools
    assert "dispatch_subagents" not in leaf.allowed_tools
    assert "subagent_board" not in leaf.allowed_tools


# LLM: test_hierarchy_schedule_strips_write_tools_from_coordinators covers copied bad catalog examples.
# 函数用途: 如果模型误给 child_coordinator 加 write_file，调度器也要剥掉，避免 coordinator 绕过 leaf 写产物。
def test_hierarchy_schedule_strips_write_tools_from_coordinators(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    parent = manager.create_run(
        goal="root",
        thought="split",
        plan=["plan"],
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
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
                        "subagent_board",
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
    assert "write_file" not in coordinator.allowed_tools
