"""LLM: split hierarchy scheduling tests for focused guard coverage.

函数/模块用途: 验证层级调度的边界场景，同时让单个测试文件保持可维护大小。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


# LLM: scheduler no longer turns scope hints into hard blockers.
# 函数用途: parent 里的领域提示只作为上下文，不再阻断父级显式派工。
def test_hierarchy_schedule_allows_forbidden_sibling_scope_hint(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(goal="root", thought="root", plan=["root"], extra_write_roots=[str(deliverables)])
    parent = manager.create_run(
        goal="只负责 text 领域任务。",
        thought="text only",
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
                    goal="创建 arithmetic worker。",
                    agent_name="arithmetic-worker",
                )
            ],
            apply=True,
        )
    )

    assert result.blocked is False
    assert len(result.created_run_ids) == 1
    assert manager.load(parent.id).child_ids == result.created_run_ids


# LLM: test_hierarchy_schedule_allows_depth_limit_text_without_scope_block reproduces R15's depth token bug.
# 函数用途: 父级写“不要创建 depth>=4”只是深度限制，不应被解析成禁止创建所有包含 depth 的 child。
def test_hierarchy_schedule_allows_depth_limit_text_without_scope_block(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(goal="root", thought="root", plan=["root"], extra_write_roots=[str(deliverables)])
    parent = manager.create_run(
        goal=(
            "depth=1 coordinator。必须覆盖四层链路，depth=3 leaf_worker 最终执行。"
            "不要创建 depth>=4 的下级。"
        ),
        thought="child coordinator",
        plan=["plan"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        role="child_coordinator",
        agent_name="小傻妞-A",
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=(
                        "构建 depth=2 coordinator，继续创建 depth=3 leaf_worker。"
                        f"最终交付到 {deliverables}/controlled_exec_refs.json。"
                    ),
                    role="child_coordinator",
                    agent_name="小小傻妞-A",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
                )
            ],
            apply=True,
            max_depth=3,
        )
    )

    assert result.blocked is False
    assert result.created_run_ids


# LLM: test_forbidden_scope_ignores_parent_thought keeps debug notes from becoming hard constraints.
# 函数用途: thought 里的测试说明不应被解析为“不得创建 X”的硬禁止规则。
def test_hierarchy_schedule_forbidden_scope_ignores_parent_thought(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(goal="root", thought="root", plan=["root"])
    parent = manager.create_run(
        goal="作为 arithmetic-lead coordinator，只负责 arithmetic 领域任务。",
        thought="测试说明：text-lead 不得创建 arithmetic leaf。",
        plan=["plan"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        allowed_tools=["schedule_child_subagents"],
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[HierarchyChildSpec(goal="创建 leaf_worker_arithmetic 并写 solution.py")],
            apply=True,
        )
    )

    assert result.blocked is False
    assert len(result.created_run_ids) == 1


# LLM: QA order is left to the parent model, not scheduler warnings.
# 函数用途: 有产物根但没有 ready worker/leaf child 时，scheduler 仍只执行父级显式派工。
def test_hierarchy_schedule_allows_qa_before_implementation_ready(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    root = manager.create_run(
        goal=f"交付示例网站到 {build}，需要 tester / bug_finder。",
        thought="root",
        plan=["root"],
        extra_write_roots=[str(build)],
        role="coordinator",
    )
    parent = manager.create_run(
        goal=f"继续创建 depth=3 leaf 写示例网站到 {build}，之后再做 QA。",
        thought="coord",
        plan=["plan"],
        parent_id=root.id,
        root_id=root.id,
        depth=2,
        role="coordinator",
        extra_write_roots=[str(build)],
        allowed_tools=["schedule_child_subagents"],
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(goal="检查示例流程", role="tester", agent_name="小小小傻妞-tester"),
                HierarchyChildSpec(goal="找坏链接和坏按钮", role="bug_finder", agent_name="小小小傻妞-bug_finder"),
            ],
            apply=True,
            max_depth=3,
        )
    )

    assert result.blocked is False
    assert len(manager.load(parent.id).child_ids) == 2


# LLM: test_hierarchy_schedule_allows_qa_after_implementation_child keeps normal QA follow-up possible.
# 函数用途: 父节点已有实现 child 后，可以继续创建 tester/bug_finder 检查产物。
def test_hierarchy_schedule_allows_qa_after_implementation_ready(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    root = manager.create_run(goal="root", thought="root", plan=["root"], extra_write_roots=[str(build)])
    parent = manager.create_run(
        goal=f"交付示例网站到 {build}，之后做 QA。",
        thought="coord",
        plan=["plan"],
        parent_id=root.id,
        root_id=root.id,
        depth=2,
        role="coordinator",
        extra_write_roots=[str(build)],
        allowed_tools=["schedule_child_subagents"],
    )
    manager.create_run(
        goal=f"写示例网站文件到 {build}",
        thought="leaf",
        plan=["write"],
        parent_id=parent.id,
        root_id=root.id,
        depth=3,
        role="leaf_worker",
        agent_name="小小小傻妞-leaf",
        extra_write_roots=[str(build)],
    )
    child = manager.load(manager.load(parent.id).child_ids[0])
    child.status = "DONE"
    child.verification_status = "VERIFIED"
    manager.save(child)

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[HierarchyChildSpec(goal="检查示例流程", role="tester", agent_name="小小小傻妞-tester")],
            apply=True,
            max_depth=3,
        )
    )

    assert result.blocked is False
    assert len(result.created_run_ids) == 1


# LLM: delegated production branches should also unlock QA at the parent that owns the contract.
# 函数用途: root 通过 coordinator 链路完成 leaf 后，root 仍能创建 tester/bug_finder，不被“直接 child 不是 worker”误挡。
def test_hierarchy_schedule_allows_qa_after_implementation_descendant_ready(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    root = manager.create_run(
        goal=f"交付示例网站到 {build}，需要 tester / bug_finder。",
        thought="root",
        plan=["root"],
        extra_write_roots=[str(build)],
        role="coordinator",
    )
    coordinator = manager.create_run(
        goal=f"协调 leaf 写示例网站到 {build}",
        thought="coord",
        plan=["delegate"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        role="child_coordinator",
        extra_write_roots=[str(build)],
    )
    leaf = manager.create_run(
        goal=f"写示例网站文件到 {build}",
        thought="leaf",
        plan=["write"],
        parent_id=coordinator.id,
        root_id=root.id,
        depth=2,
        role="leaf_worker",
        agent_name="小小傻妞-leaf",
        extra_write_roots=[str(build)],
    )
    leaf.status = "DONE"
    leaf.verification_status = "VERIFIED"
    manager.save(leaf)

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[HierarchyChildSpec(goal="检查完整示例流程", role="tester", agent_name="小傻妞-tester")],
            apply=True,
            max_depth=3,
        )
    )

    assert result.blocked is False
    assert len(result.created_run_ids) == 1


# LLM: scheduler does not infer hidden blockers from prose.
# 函数用途: text/arithmetic 这类领域词不再变成 Python 层硬卡点。
def test_hierarchy_schedule_allows_different_declared_work_topics(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(goal="root", thought="root", plan=["root"], extra_write_roots=[str(deliverables)])
    parent = manager.create_run(
        goal="只负责 text 领域任务。",
        thought="text only",
        plan=["plan"],
        agent_name="text-lead",
        role="child_coordinator",
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[HierarchyChildSpec(goal="创建 arithmetic worker。")],
            apply=True,
        )
    )

    assert result.blocked is False
    assert len(result.created_run_ids) == 1
    assert manager.load(parent.id).child_ids == result.created_run_ids
