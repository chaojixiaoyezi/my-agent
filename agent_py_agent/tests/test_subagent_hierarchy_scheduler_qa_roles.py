"""LLM: QA role auto-scheduling tests for explicit subagent hierarchy contracts.

函数/模块用途: 验证父任务点名 tester / bug_finder / acceptor 时，层级调度会创建真实 QA 子任务且不会重复补派。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)

ORCHESTRATION_TOOLS = ["schedule_child_subagents", "dispatch_subagents", "subagent_board"]


# LLM: _qa_parent creates a coordinator that explicitly requires all broad QA roles.
# 函数用途: 构造点名 tester/bug_finder/acceptor 的父任务，复用在自动补派测试里。
def _qa_parent(manager: SubAgentManager, *, extra_write_roots: list[str] | None = None):
    return manager.create_run(
        goal="购物网站任务必须覆盖 tester / bug_finder / acceptor 三类 QA 子代理。",
        thought="父级要做真实分工。",
        plan=["delegate"],
        role="coordinator",
        allowed_tools=ORCHESTRATION_TOOLS,
        extra_write_roots=extra_write_roots or [],
    )


# LLM: explicit QA role contracts should become real child runs even when the model forgets them.
# 函数用途: 父级点名 tester/bug_finder/acceptor 时，层级调度器自动补齐缺失 QA 子代理，避免只创建 worker 后验收才失败。
def test_hierarchy_schedule_auto_adds_required_qa_roles_from_parent_contract(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    parent = _qa_parent(manager)

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="实现购物车模块，写回证据 refs。",
                    role="worker",
                    agent_name="小傻妞-cart-worker",
                )
            ],
            apply=True,
        )
    )
    roles = {manager.load(run_id).role for run_id in result.created_run_ids}

    assert result.blocked is False
    assert result.planned_count == 4
    assert {"tester", "bug_finder", "acceptor"}.issubset(roles)
    assert len(manager.load(parent.id).child_ids) == 4


# LLM: persisted QA children must prevent duplicate auto-scheduling on later parent dispatches.
# 函数用途: 父级已经存在 tester 子代理时，新一轮调度只补缺失 QA 角色，不重复创建同类检查代理。
def test_hierarchy_schedule_avoids_duplicate_required_qa_roles(tmp_path):
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
    roles = [manager.load(run_id).role for run_id in result.created_run_ids]

    assert existing.id in manager.load(parent.id).child_ids
    assert "tester" not in roles
    assert {"bug_finder", "acceptor"}.issubset(set(roles))


# LLM: product delivery parents should not auto-create QA before implementation is ready.
# 函数用途: 有产物根的父任务先创建/完成 worker 或 leaf，再自动补派 QA，避免空 build 上测试空转。
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


# LLM: once implementation reaches acceptance, required QA autofill may create checkers.
# 函数用途: worker 已等待验收后，调度器可以自动补齐 tester/bug_finder/acceptor。
def test_hierarchy_schedule_auto_qa_after_implementation_ready(tmp_path):
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
    roles = {manager.load(run_id).role for run_id in result.created_run_ids}

    assert result.blocked is False
    assert {"tester", "bug_finder", "acceptor"}.issubset(roles)
