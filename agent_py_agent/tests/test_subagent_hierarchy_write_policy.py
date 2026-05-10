"""LLM: focused tests for hierarchy write-root policy.

函数/模块用途: 验证报告型角色可读产品路径并写本地报告，但不能继承最终产物写入根。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


# LLM: test_hierarchy_schedule_keeps_report_roles_task_local protects product deliverable boundaries.
# 函数用途: researcher/tester/acceptor 等报告型角色可以写自己的报告，但不能继承父级最终产物目录。
def test_hierarchy_schedule_keeps_report_roles_task_local(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(
        goal=f"root deliverables at {deliverables}",
        thought="orchestrate",
        plan=["plan"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="研究资料并写 task-local researcher/report.md，不写最终业务产物。",
                    agent_name="researcher",
                    role="researcher",
                )
            ],
            apply=True,
        )
    )
    child = manager.load(result.created_run_ids[0])

    assert child.role == "researcher"
    assert child.allowed_write_roots == [child.task_dir]
    assert str(deliverables) not in child.allowed_write_roots
    assert "允许写入根：" not in child.goal


# LLM: test_hierarchy_schedule_keeps_report_goal_product_root_readonly covers model-written product paths.
# 函数用途: 模型把最终目录写进报告型 goal 时，系统保留路径上下文但不授予该目录写权限。
def test_hierarchy_schedule_keeps_report_goal_product_root_readonly(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(
        goal=f"最终业务产物只能放在 {deliverables}",
        thought="orchestrate",
        plan=["plan"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=f"研究资料，并把 researcher/report.md 写到 {deliverables} 供父级查看。",
                    agent_name="researcher",
                    role="researcher",
                )
            ],
            apply=True,
        )
    )
    child = manager.load(result.created_run_ids[0])

    assert child.allowed_write_roots == [child.task_dir]
    assert str(deliverables) not in child.allowed_write_roots
    assert str(deliverables) in child.goal
