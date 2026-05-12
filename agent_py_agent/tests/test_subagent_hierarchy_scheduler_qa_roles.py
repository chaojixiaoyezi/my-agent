"""LLM: QA role auto-scheduling tests for explicit subagent hierarchy contracts.

函数/模块用途: 验证父任务点名 tester / bug_finder / acceptor 时，层级调度会创建真实 QA 子任务且不会重复补派。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)
from agent_py_agent.agent.subagents.services.qa_role_contract import qa_roles_required_by_task

ORCHESTRATION_TOOLS = ["schedule_child_subagents", "dispatch_subagents", "subagent_board"]


# LLM: _qa_parent creates a coordinator that explicitly requires all broad QA roles.
# 函数用途: 构造点名 tester/bug_finder/acceptor 的父任务，复用在 QA advice 测试里。
def _qa_parent(manager: SubAgentManager, *, extra_write_roots: list[str] | None = None):
    return manager.create_run(
        goal="购物网站任务必须覆盖 tester / bug_finder / acceptor 三类 QA 子代理。",
        thought="父级要做真实分工。",
        plan=["delegate"],
        role="coordinator",
        allowed_tools=ORCHESTRATION_TOOLS,
        extra_write_roots=extra_write_roots or [],
    )


# LLM: explicit QA role contracts should become LLM-facing advice, not hardcoded child creation.
# 函数用途: 父级点名 tester/bug_finder/acceptor 时，调度器提示缺失 QA 角色，但不替 LLM 固定创建。
def test_hierarchy_schedule_advises_required_qa_roles_without_auto_creation(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    parent = _qa_parent(manager, extra_write_roots=[str(build)])

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                        goal=f"实现购物车模块到 {build}，写回证据 refs。",
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
    assert set(result.quality_advice.suggested_roles) == {"tester", "bug_finder", "acceptor"}
    assert len(manager.load(parent.id).child_ids) == 1


# LLM: persisted QA children should narrow LLM advice instead of forcing deterministic auto-fill.
# 函数用途: 父级已存在 tester 时，调度器建议缺失 QA 角色，但不自动创建 bug_finder/acceptor。
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
    assert result.quality_advice.suggested_roles == ["bug_finder", "acceptor"]


# LLM: product delivery parents should not auto-create QA before implementation is ready.
# 函数用途: 有产物根的父任务先创建/完成 worker 或 leaf，再给 LLM QA advice，避免空 build 上测试空转。
def test_hierarchy_schedule_defers_auto_qa_until_implementation_ready(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    parent = _qa_parent(manager, extra_write_roots=[str(build)])

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=f"实现购物站页面，写到 {build}。",
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


# LLM: once implementation reaches acceptance, required QA advice should invite LLM-chosen checkers.
# 函数用途: worker 已等待验收后，调度器建议可创建 QA wave，但不直接替 LLM 创建固定角色。
def test_hierarchy_schedule_quality_advice_after_implementation_ready(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    parent = _qa_parent(manager, extra_write_roots=[str(build)])
    worker = manager.create_run(
        goal=f"实现购物站页面，写到 {build}。",
        thought="work",
        plan=["write"],
        parent_id=parent.id,
        root_id=parent.id,
        role="worker",
        agent_name="小傻妞-shop-worker",
        extra_write_roots=[str(build)],
    )
    worker.status = "AWAITING_ACCEPTANCE"
    worker.verification_status = "NEEDS_ACCEPTANCE"
    manager.save(worker)

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[],
            apply=True,
        )
    )

    assert result.blocked is True
    assert result.reason == "no_child_specs"
    assert result.created_run_ids == []
    assert result.quality_advice is not None
    assert result.quality_advice.phase == "quality_wave_ready"
    assert set(result.quality_advice.suggested_roles) == {"tester", "bug_finder", "acceptor"}


# LLM: QA advice must notice ready implementation descendants, not only direct worker children.
# 函数用途: root 先通过 coordinator 链路完成 leaf 后，再询问 schedule_child_subagents 时应收到 quality_wave_ready。
def test_hierarchy_schedule_quality_advice_after_implementation_descendant_ready(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    parent = _qa_parent(manager, extra_write_roots=[str(build)])
    coordinator = manager.create_run(
        goal=f"协调 leaf 写购物站页面到 {build}。",
        thought="coord",
        plan=["delegate"],
        parent_id=parent.id,
        root_id=parent.id,
        role="child_coordinator",
        agent_name="小傻妞-shop-lead",
        extra_write_roots=[str(build)],
    )
    leaf = manager.create_run(
        goal=f"实现购物站页面，写到 {build}。",
        thought="work",
        plan=["write"],
        parent_id=coordinator.id,
        root_id=parent.id,
        role="leaf_worker",
        agent_name="小小傻妞-shop-worker",
        extra_write_roots=[str(build)],
    )
    leaf.status = "AWAITING_ACCEPTANCE"
    leaf.verification_status = "NEEDS_ACCEPTANCE"
    manager.save(leaf)

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[],
            apply=True,
        )
    )

    assert result.blocked is True
    assert result.reason == "no_child_specs"
    assert result.created_run_ids == []
    assert result.quality_advice is not None
    assert result.quality_advice.phase == "quality_wave_ready"
    assert set(result.quality_advice.suggested_roles) == {"tester", "bug_finder", "acceptor"}


# LLM: QA workers are terminal reviewer roles, not parents that must spawn another copy of themselves.
# 函数用途: tester/bug_finder/acceptor 自己的目标会出现角色名，但验收时不应再要求它们创建同名 QA 子代理。
def test_qa_role_tasks_do_not_inherit_their_own_required_role_contract(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(
        goal="你是小小傻妞-acceptor（第三层验收子代理），验收购物网站。",
        thought="qa",
        plan=["inspect refs"],
        role="acceptor",
        agent_name="小小傻妞-acceptor",
    )

    assert qa_roles_required_by_task(task) == []
