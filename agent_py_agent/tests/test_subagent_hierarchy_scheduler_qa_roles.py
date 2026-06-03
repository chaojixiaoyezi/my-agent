"""LLM: QA role auto-scheduling tests for explicit subagent hierarchy contracts.

函数/模块用途: 验证父任务点名 tester / bug_finder 时，层级调度会创建真实 QA 子任务且不会重复补派。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)
from agent_py_agent.agent.subagents.services.qa_role_contract import qa_roles_required_by_task

ORCHESTRATION_TOOLS = ["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"]


def _qa_parent(manager: SubAgentManager, *, extra_write_roots: list[str] | None = None):
    return manager.create_run(
        goal="父级要做真实分工，完成后需要质量检查。",
        thought="父级要做真实分工。",
        plan=["delegate"],
        role="coordinator",
        allowed_tools=ORCHESTRATION_TOOLS,
        extra_write_roots=extra_write_roots or [],
        attributes={"required_qa_roles": ["tester", "bug_finder", "bug_finder"]},
    )


def test_hierarchy_schedule_advises_required_qa_roles_without_auto_creation(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    parent = _qa_parent(manager, extra_write_roots=[str(build)])

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                        goal=f"实现流程状态模块到 {build}，写回证据 refs。",
                    role="worker",
                    agent_name="小傻妞-cart-worker",
                )
            ],
            apply=True,
        )
    )
    roles = {manager.load(run_id).role for run_id in result.created_run_ids}

    assert result.blocked is False
    assert result.planned_count == 1
    assert roles <= {"worker", "leaf_worker"}
    assert result.quality_advice is not None
    assert result.quality_advice.phase == "implementation_first"
    assert set(result.quality_advice.suggested_roles) == {"tester", "bug_finder"}
    assert len(manager.load(parent.id).child_ids) == 1


def test_hierarchy_schedule_advice_omits_existing_qa_roles(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    parent = _qa_parent(manager)
    existing = manager.create_run(
        goal="先测试核心链路。",
        thought="qa",
        plan=["test"],
        parent_id=parent.id,
        root_id=parent.id,
        role="tester",
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="协调登录模块实现。",
                    role="child_coordinator",
                    agent_name="小傻妞-login-lead",
                    allowed_tools=ORCHESTRATION_TOOLS,
                )
            ],
            apply=True,
        )
    )

    assert existing.id in manager.load(parent.id).child_ids
    assert result.blocked is False
    assert len(result.created_run_ids) == 1
    assert result.quality_advice is not None
    assert result.quality_advice.suggested_roles == ["bug_finder"]


def test_hierarchy_schedule_defers_auto_qa_until_implementation_ready(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    parent = _qa_parent(manager, extra_write_roots=[str(build)])

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=f"实现示例站页面，写到 {build}。",
                    role="worker",
                    agent_name="小傻妞-shop-worker",
                )
            ],
            apply=True,
        )
    )
    roles = {manager.load(run_id).role for run_id in result.created_run_ids}

    assert result.blocked is False
    assert roles <= {"worker", "leaf_worker"}
    assert roles
    assert len(manager.load(parent.id).child_ids) == 1


def test_hierarchy_schedule_quality_advice_after_implementation_ready(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    parent = _qa_parent(manager, extra_write_roots=[str(build)])
    worker = manager.create_run(
        goal=f"实现示例站页面，写到 {build}。",
        thought="work",
        plan=["write"],
        parent_id=parent.id,
        root_id=parent.id,
        role="worker",
        agent_name="小傻妞-shop-worker",
        extra_write_roots=[str(build)],
    )
    worker.status = "DONE"
    worker.verification_status = "VERIFIED"
    manager.save(worker)

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[],
            apply=True,
        )
    )

    assert result.blocked is False
    assert result.created_run_ids == []
    assert result.quality_advice is not None
    assert result.quality_advice.phase == "quality_wave_ready"
    assert set(result.quality_advice.suggested_roles) == {"tester", "bug_finder"}


def test_hierarchy_schedule_quality_advice_after_implementation_descendant_ready(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    parent = _qa_parent(manager, extra_write_roots=[str(build)])
    coordinator = manager.create_run(
        goal=f"协调 leaf 写示例站页面到 {build}。",
        thought="coord",
        plan=["delegate"],
        parent_id=parent.id,
        root_id=parent.id,
        role="child_coordinator",
        agent_name="小傻妞-shop-lead",
        extra_write_roots=[str(build)],
    )
    leaf = manager.create_run(
        goal=f"实现示例站页面，写到 {build}。",
        thought="work",
        plan=["write"],
        parent_id=coordinator.id,
        root_id=parent.id,
        role="leaf_worker",
        agent_name="小小傻妞-shop-worker",
        extra_write_roots=[str(build)],
    )
    leaf.status = "DONE"
    leaf.verification_status = "VERIFIED"
    manager.save(leaf)

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[],
            apply=True,
        )
    )

    assert result.blocked is False
    assert result.created_run_ids == []
    assert result.quality_advice is not None
    assert result.quality_advice.phase == "quality_wave_ready"
    assert set(result.quality_advice.suggested_roles) == {"tester", "bug_finder"}


def test_qa_role_tasks_do_not_inherit_their_own_required_role_contract(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(
        goal="你是小小傻妞-bug_finder（第三层验收子代理），验收示例网站。",
        thought="qa",
        plan=["inspect refs"],
        role="bug_finder",
        agent_name="小小傻妞-bug_finder",
    )

    assert qa_roles_required_by_task(task) == []


def test_qa_role_contract_ignores_natural_language_role_requests(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(
        goal="这个示例网站做好后记得测试、找问题、最后验收。",
        thought="qa",
        plan=["inspect refs"],
        role="coordinator",
        agent_name="小傻妞-coordinator",
    )

    assert qa_roles_required_by_task(task) == []
