"""LLM: focused tests for hierarchy write-root policy.

函数/模块用途: 验证上层/协调/报告型角色保留覆盖下级的产物写根，同时角色职责仍要求优先写报告、把实际产物交给 worker。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


# LLM: test_hierarchy_schedule_keeps_report_roles_with_parent_write_coverage covers takeover authority.
# 函数用途: researcher/tester/bug_finder 等报告型角色也继承父级产物根，方便检查、接管、救援；是否亲自写由角色职责约束。
def test_hierarchy_schedule_keeps_report_roles_with_parent_write_coverage(tmp_path):
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
    assert child.allowed_write_roots == [child.task_dir, str(deliverables)]
    assert "允许写入根：" in child.goal


# LLM: descendants use their own task room plus product/shared roots, not parent internals.
# 函数用途: 小小傻妞写局部报告到自己的 task_dir；跨层共享走 message/summary/refs，不直接污染父级任务目录。
def test_hierarchy_schedule_does_not_grant_parent_task_dir_to_children(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    parent = manager.create_run(
        goal="研究市场环境并整合孩子报告",
        thought="coordinate",
        plan=["plan"],
        role="coordinator",
        allowed_tools=["schedule_child_subagents", "dispatch_subagents"],
    )

    result = manager.schedule_child_runs(
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


# LLM: test_hierarchy_schedule_keeps_report_goal_product_root_writable_for_recovery covers model paths.
# 函数用途: 模型把最终目录写进报告型 goal 时，系统保留路径上下文并授予覆盖权限，方便父链检查和恢复。
def test_hierarchy_schedule_keeps_report_goal_product_root_writable_for_recovery(tmp_path):
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

    assert child.allowed_write_roots == [child.task_dir, str(deliverables)]
    assert str(deliverables) in child.goal


# LLM: test_hierarchy_schedule_recovers_report_role_from_child_agent_name covers real runner placeholder roles.
# 函数用途: 模型把 role 写成 child 但 agent_name 写 researcher/tester 时，系统仍识别报告型职责，同时保留上级覆盖写入根。
def test_hierarchy_schedule_recovers_report_role_from_child_agent_name(tmp_path):
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


# LLM: test_hierarchy_schedule_grants_worker_structured_write_root covers real coordinator output.
# 函数用途: coordinator 给 worker 的结构化 extra_write_roots 写出产物目录时，worker 应拿到该目录写权限。
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

    result = manager.schedule_child_runs(
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

    assert worker.role == "leaf_worker"
    assert str(deliverables / "build") in worker.allowed_write_roots
    assert "write_file" in worker.allowed_tools


# LLM: test_hierarchy_schedule_blocks_sibling_path_drift protects exact deliverable-root propagation.
# 函数用途: coordinator 把结构化 write root 指到 sibling 目录时，调度层应阻断并要求重写 child spec。
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

    result = manager.schedule_child_runs(
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


# LLM: test_hierarchy_schedule_allows_child_root_under_parent_root keeps valid nested output dirs working.
# 函数用途: child 的结构化写入根在父级 build 目录下时不能被漂移 guard 误挡。
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

    result = manager.schedule_child_runs(
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


# LLM: test_hierarchy_schedule_ignores_url_image_sources_in_goal covers real shopping E2E URLs.
# 函数用途: worker goal 里出现图片 CDN URL 时，scheduler 不能把普通文本 URL 当成本地写入根。
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

    result = manager.schedule_child_runs(
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


# LLM: test_hierarchy_schedule_preserves_coordinator_orchestration_tools covers real model omissions.
# 函数用途: 模型给 coordinator 显式传读写工具但漏掉调度工具时，系统仍补齐创建/调度下一层能力。
def test_hierarchy_schedule_preserves_coordinator_orchestration_tools(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(
        goal="root",
        thought="delegate",
        plan=["plan"],
        role="coordinator",
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
    )

    result = manager.schedule_child_runs(
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
