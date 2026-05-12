"""LLM: split hierarchy scheduling tests for focused guard coverage.

函数/模块用途: 验证层级调度的边界场景，同时让单个测试文件保持可维护大小。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


# LLM: test_hierarchy_schedule_blocks_forbidden_sibling_scope prevents wrong-domain leaf creation.
# 函数用途: 当 parent 明确禁止创建 sibling 领域任务时，scheduler 必须阻断错误 child spec。
def test_hierarchy_schedule_blocks_forbidden_sibling_scope(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(goal="root", thought="root", plan=["root"], extra_write_roots=[str(deliverables)])
    parent = manager.create_run(
        goal=(
            "作为 text-lead coordinator，只负责 text 领域任务。"
            f"创建 leaf_worker_text，写入 {deliverables}/leaf_outputs/leaf_worker_text/solution.py。"
            "不得创建 arithmetic 相关任务。"
        ),
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
            child_specs=[HierarchyChildSpec(goal="创建 leaf_worker_arithmetic 并写 solution.py")],
            apply=True,
        )
    )

    assert result.blocked is True
    assert "forbidden_child_scope:arithmetic" in result.reason
    assert manager.load(parent.id).child_ids == []


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


# LLM: test_hierarchy_schedule_blocks_implicit_domain_mismatch catches coordinator sibling drift.
# 函数用途: 即使 parent 没写“不得创建”，text-lead 也不能误创建 arithmetic leaf。
def test_hierarchy_schedule_blocks_implicit_domain_mismatch(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(goal="root", thought="root", plan=["root"], extra_write_roots=[str(deliverables)])
    parent = manager.create_run(
        goal=(
            "创建 text leaf_worker，写入 "
            f"{deliverables}/leaf_outputs/leaf_worker_text/solution.py，"
            "实现 normalize_text(text)。"
        ),
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
            child_specs=[HierarchyChildSpec(goal="创建 arithmetic leaf_worker，写入 solution.py")],
            apply=True,
        )
    )

    assert result.blocked is True
    assert result.reason == "domain_mismatch:text->arithmetic"
    assert manager.load(parent.id).child_ids == []
