"""LLM: focused tests for hierarchy write-root policy.

函数/模块用途: 验证上层/协调/报告型角色保留覆盖下级的产物写根，同时角色职责仍要求优先写报告、把实际产物交给 worker。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


def test_hierarchy_schedule_keeps_report_roles_with_parent_write_coverage(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(
        goal=f"root deliverables at {deliverables}",
        thought="orchestrate",
        plan=["plan"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.hierarchy.schedule_child_runs(
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
    assert child.allowed_write_roots == [child.task_dir, str(deliverables)]
    assert "允许写入根：" in child.goal


def test_hierarchy_schedule_does_not_grant_parent_task_dir_to_children(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    parent = manager.create_run(
        goal="研究市场环境并整合孩子报告",
        thought="coordinate",
        plan=["plan"],
        role="coordinator",
        allowed_tools=["schedule_child_subagents", "dispatch_subagents"],
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="研究印尼市场，并在自己的 task_dir 写 indonesia_research.md 供父级通过 refs 整合。",
                    agent_name="小小傻妞-印尼研究",
                    role="researcher",
                )
            ],
            apply=True,
        )
    )
    child = manager.load(result.created_run_ids[0])

    assert parent.task_dir not in child.allowed_write_roots
    assert "write_file" in child.allowed_tools


def test_hierarchy_schedule_keeps_report_goal_product_root_writable_for_recovery(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(
        goal=f"最终业务产物只能放在 {deliverables}",
        thought="orchestrate",
        plan=["plan"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.hierarchy.schedule_child_runs(
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

    assert child.allowed_write_roots == [child.task_dir, str(deliverables)]
    assert str(deliverables) in child.goal


def test_hierarchy_schedule_recovers_report_role_from_child_agent_name(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(
        goal=f"最终业务产物只能放在 {deliverables}",
        thought="orchestrate",
        plan=["plan"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="读取资料，写 researcher/report.md 到 task_dir，不能写最终业务产物。",
                    agent_name="researcher",
                    role="child",
                ),
                HierarchyChildSpec(
                    goal="整理最终说明并写 README.md 到产物目录。",
                    agent_name="writer",
                    role="child",
                ),
            ],
            apply=True,
        )
    )
    researcher = manager.load(result.created_run_ids[0])
    writer = manager.load(result.created_run_ids[1])

    assert researcher.role == "researcher"
    assert researcher.allowed_write_roots == [researcher.task_dir, str(deliverables)]
    assert writer.role == "writer"
    assert str(deliverables) in writer.allowed_write_roots


def test_hierarchy_schedule_grants_worker_path_written_by_child_spec(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    coordinator = manager.create_run(
        goal="cart coordinator only writes coordination notes",
        thought="delegate",
        plan=["plan"],
        agent_name="cart-checkout-lead",
        role="coordinator",
        allowed_tools=["schedule_child_subagents", "dispatch_subagents"],
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=coordinator.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="创建流程状态页面，写 flow-a.html。",
                    agent_name="cart-checkout-worker",
                    role="worker",
                    extra_write_roots=[str(deliverables / "build")],
                )
            ],
            apply=True,
        )
    )
    worker = manager.load(result.created_run_ids[0])

    assert worker.role == "worker"
    assert str(deliverables / "build") in worker.allowed_write_roots
    assert "write_file" in worker.allowed_tools


def test_hierarchy_schedule_blocks_sibling_path_drift(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables" / "case"
    parent = manager.create_run(
        goal="创建流程状态页面。",
        thought="delegate",
        plan=["plan"],
        agent_name="cart-coordinator",
        role="coordinator",
        allowed_tools=["schedule_child_subagents", "dispatch_subagents"],
        extra_write_roots=[str(deliverables / "build")],
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="创建 flow-a.html。",
                    agent_name="cart-worker",
                    role="worker",
                    extra_write_roots=[str(deliverables / "stage7_r8_build")],
                )
            ],
            apply=True,
        )
    )

    assert result.blocked is False
    assert result.created_run_ids


def test_hierarchy_schedule_allows_child_path_under_parent_root(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables" / "case"
    parent = manager.create_run(
        goal="创建示例网站。",
        thought="delegate",
        plan=["plan"],
        agent_name="cart-coordinator",
        role="coordinator",
        allowed_tools=["schedule_child_subagents", "dispatch_subagents"],
        extra_write_roots=[str(deliverables / "build")],
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="写流程状态页面。",
                    agent_name="cart-worker",
                    role="worker",
                    extra_write_roots=[str(deliverables / "build")],
                )
            ],
            apply=True,
        )
    )
    worker = manager.load(result.created_run_ids[0])

    assert result.blocked is False
    assert str(deliverables / "build") in worker.allowed_write_roots


def test_hierarchy_schedule_ignores_url_image_sources_in_write_roots(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    coordinator = manager.create_run(
        goal="catalog coordinator only delegates",
        thought="delegate",
        plan=["plan"],
        agent_name="catalog-lead",
        role="coordinator",
        allowed_tools=["schedule_child_subagents", "dispatch_subagents"],
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=coordinator.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=(
                        "写条目列表，"
                        "图片可使用 https://picsum.photos/300/200 和 "
                        "https://images.unsplash.com/photo-1.jpg。"
                    ),
                    agent_name="catalog-page-worker",
                    role="worker",
                    extra_write_roots=[str(deliverables / "build" / "product-list.html")],
                )
            ],
            apply=True,
        )
    )

    assert result.created_run_ids
    worker = manager.load(result.created_run_ids[0])
    assert str(deliverables / "build" / "product-list.html") in worker.allowed_write_roots
    assert not any("picsum" in root or "unsplash" in root for root in worker.allowed_write_roots)


def test_hierarchy_schedule_preserves_coordinator_orchestration_tools(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(
        goal="root",
        thought="delegate",
        plan=["plan"],
        role="coordinator",
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="cart lead should create a worker later",
                    agent_name="cart-checkout-lead",
                    role="coordinator",
                    allowed_tools=["write_file", "read_file", "list_files"],
                )
            ],
            apply=True,
        )
    )
    coordinator = manager.load(result.created_run_ids[0])

    assert coordinator.role == "coordinator"
    assert "schedule_child_subagents" in coordinator.allowed_tools
    assert "dispatch_subagents" in coordinator.allowed_tools
    assert "write_file" in coordinator.allowed_tools
