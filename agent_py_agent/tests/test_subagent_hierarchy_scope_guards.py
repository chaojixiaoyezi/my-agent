"""LLM: split hierarchy scheduling tests for focused guard coverage.

函数/模块用途: 验证层级调度的边界场景，同时让单个测试文件保持可维护大小。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


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
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.hierarchy.schedule_child_runs(
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


def test_hierarchy_schedule_allows_depth_limit_text_without_scope_block(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(goal="root", thought="root", plan=["root"], extra_write_roots=[str(deliverables)])
    parent = manager.create_run(
        goal=(
            "depth=1 coordinator。必须覆盖四层链路，depth=3 worker 最终执行。"
            "不要创建 depth>=4 的下级。"
        ),
        thought="child coordinator",
        plan=["plan"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        role="coordinator",
        agent_name="小傻妞-A",
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=(
                        "构建 depth=2 coordinator，继续创建 depth=3 worker。"
                        f"最终交付到 {deliverables}/controlled_exec_refs.json。"
                    ),
                    role="coordinator",
                    agent_name="小小傻妞-A",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
                )
            ],
            apply=True,
            max_depth=3,
        )
    )

    assert result.blocked is False
    assert result.created_run_ids


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

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[HierarchyChildSpec(goal="创建 worker_arithmetic 并写 solution.py")],
            apply=True,
        )
    )

    assert result.blocked is False
    assert len(result.created_run_ids) == 1


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

    result = manager.hierarchy.schedule_child_runs(
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
        role="worker",
        agent_name="小小小傻妞-leaf",
        extra_write_roots=[str(build)],
    )
    child = manager.load(manager.load(parent.id).child_ids[0])
    artifact = build / "index.html"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("ready\n", encoding="utf-8")
    child.status = "DONE"
    child.verification_status = "VERIFIED"
    child.artifact_refs = [str(artifact)]
    manager.save(child)

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[HierarchyChildSpec(goal="检查示例流程", role="tester", agent_name="小小小傻妞-tester")],
            apply=True,
            max_depth=3,
        )
    )

    assert result.blocked is False
    assert len(result.created_run_ids) == 1


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
        role="coordinator",
        extra_write_roots=[str(build)],
    )
    leaf = manager.create_run(
        goal=f"写示例网站文件到 {build}",
        thought="leaf",
        plan=["write"],
        parent_id=coordinator.id,
        root_id=root.id,
        depth=2,
        role="worker",
        agent_name="小小傻妞-leaf",
        extra_write_roots=[str(build)],
    )
    artifact = build / "index.html"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("ready\n", encoding="utf-8")
    leaf.status = "DONE"
    leaf.verification_status = "VERIFIED"
    leaf.artifact_refs = [str(artifact)]
    manager.save(leaf)

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[HierarchyChildSpec(goal="检查完整示例流程", role="tester", agent_name="小傻妞-tester")],
            apply=True,
            max_depth=3,
        )
    )

    assert result.blocked is False
    assert len(result.created_run_ids) == 1


def test_hierarchy_schedule_allows_different_declared_work_topics(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(goal="root", thought="root", plan=["root"], extra_write_roots=[str(deliverables)])
    parent = manager.create_run(
        goal="只负责 text 领域任务。",
        thought="text only",
        plan=["plan"],
        agent_name="text-lead",
        role="coordinator",
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[HierarchyChildSpec(goal="创建 arithmetic worker。")],
            apply=True,
        )
    )

    assert result.blocked is False
    assert len(result.created_run_ids) == 1
    assert manager.load(parent.id).child_ids == result.created_run_ids
