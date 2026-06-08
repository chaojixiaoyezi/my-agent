"""LLM: focused tests for explicit subagent hierarchy scheduling.

函数/模块用途: 验证父代理只能通过 bundle 化层级调度入口创建子/孙代理，默认 dry-run，显式 apply 才写入。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)
from agent_py_agent.cli.parser import build_parser


def _child_specs(count: int, *, prefix: str = "worker") -> list[HierarchyChildSpec]:
    return [
        HierarchyChildSpec(
            goal=f"{prefix} goal {index}",
            agent_name=f"{prefix}-{index}",
            role=prefix,
            acceptance_checks=[f"{prefix} check {index}"],
        )
        for index in range(1, count + 1)
    ]


def test_hierarchy_schedule_dry_run_previews_without_writing_children(tmp_path):
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="parent", thought="split safely", plan=["plan"])

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(parent_run_id=parent.id, child_specs=_child_specs(2))
    )

    assert result.dry_run is True
    assert result.blocked is False
    assert result.created_run_ids == []
    assert [(item.depth, item.parent_id, item.root_id, item.created) for item in result.items] == [
        (1, parent.id, parent.id, False),
        (1, parent.id, parent.id, False),
    ]
    assert manager.load(parent.id).child_ids == []


def test_hierarchy_schedule_apply_builds_two_child_four_grandchild_tree(tmp_path):
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="orchestrate", plan=["plan"])

    first = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=_child_specs(2, prefix="child"),
            apply=True,
            requested_by="root",
        )
    )
    assert first.created_run_ids
    assert len(first.created_run_ids) == 2

    for child_id in first.created_run_ids:
        grand = manager.hierarchy.schedule_child_runs(
            params=HierarchyScheduleRequest(
                parent_run_id=child_id,
                child_specs=_child_specs(2, prefix="grand"),
                apply=True,
                requested_by="child",
            )
        )
        assert len(grand.created_run_ids) == 2
        assert {manager.load(grand_id).depth for grand_id in grand.created_run_ids} == {2}
        assert {manager.load(grand_id).root_id for grand_id in grand.created_run_ids} == {root.id}

    loaded_root = manager.load(root.id)
    assert loaded_root.child_ids == first.created_run_ids
    assert sum(len(manager.load(child_id).child_ids) for child_id in first.created_run_ids) == 4


def test_hierarchy_schedule_repairs_literal_wildcard_names_with_default_display_name(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(goal="root", thought="orchestrate", plan=["plan"])
    child = manager.create_run(
        goal="child",
        thought="split",
        plan=["plan"],
        parent_id=root.id,
        root_id=root.id,
        depth=2,
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="leaf does controlled exec",
                    agent_name="小小小傻妞-*-*",
                    role="worker",
                )
            ],
            apply=True,
            max_depth=3,
        )
    )
    leaf = manager.load(result.created_run_ids[0])

    assert leaf.agent_name == "agent-d3-worker-1"


def test_hierarchy_schedule_blocks_leaf_when_four_layer_token_has_no_space(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(
        goal=f"必须覆盖4层链路，depth=3 worker 最终写 {deliverables}/refs.json。",
        thought="split",
        plan=["delegate"],
        extra_write_roots=[str(deliverables)],
    )
    child = manager.create_run(
        goal=(
            "depth=1 coordinator，继承父级目标/边界：必须覆盖4层链路，"
            "depth=3 worker 最终执行 controlled_exec。"
        ),
        thought="split",
        plan=["delegate"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        role="coordinator",
        extra_write_roots=[str(deliverables)],
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=f"depth=2 worker 直接写 {deliverables}/refs.json。",
                    agent_name="小小傻妞-r07-d2",
                    role="worker",
                )
            ],
            apply=True,
            max_depth=3,
        )
    )

    assert result.blocked is False
    leaf = manager.load(result.created_run_ids[0])
    assert leaf.parent_id == child.id
    assert leaf.depth == 2


def test_hierarchy_schedule_allows_internal_task_dir_context_with_product_root(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    parent = manager.create_run(
        goal=f"协调下层，把最终报告写到 {deliverables}/report.md。",
        thought="split",
        plan=["delegate"],
        role="coordinator",
        depth=1,
        extra_write_roots=[str(deliverables)],
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=(
                        f"创建下一层 coordinator；可在 task_dir {parent.task_dir} 写内部哨兵，"
                        f"最终用户产物仍写到 {deliverables}/report.md。"
                    ),
                    agent_name="小小傻妞-r07-d2",
                    role="coordinator",
                )
            ],
            apply=True,
            max_depth=3,
        )
    )

    assert result.blocked is False
    assert result.created_run_ids


def test_hierarchy_schedule_inherits_parent_extra_write_roots(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(
        goal="root",
        thought="orchestrate",
        plan=["plan"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(parent_run_id=root.id, child_specs=_child_specs(1), apply=True)
    )
    child = manager.load(result.created_run_ids[0])

    assert str(deliverables) in child.allowed_write_roots
    assert root.task_dir not in child.allowed_write_roots


def test_hierarchy_schedule_infers_leaf_write_tools_from_explicit_deliverables(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(
        goal="root",
        thought="orchestrate",
        plan=["plan"],
        extra_write_roots=[str(deliverables)],
    )
    child = manager.create_run(
        goal="child coordinator",
        thought="split",
        plan=["plan"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=f"写入 {deliverables}/leaf/solution.py 和 README.md。",
                    agent_name="leaf-writer",
                    role="worker",
                    acceptance_checks=["solution.py 必须存在"],
                )
            ],
            apply=True,
        )
    )
    leaf = manager.load(result.created_run_ids[0])

    assert "write_file" in leaf.allowed_tools
    assert "read_artifact" in leaf.allowed_tools
    assert "apply_patch" in leaf.allowed_tools
    assert str(deliverables) in leaf.allowed_write_roots


def test_hierarchy_schedule_does_not_normalize_model_write_alias_for_leaf_tasks(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    parent = manager.create_run(
        goal="child coordinator",
        thought="split",
        plan=["plan"],
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=f"写入 {deliverables}/proof.txt，内容为 trace-hierarchy-ok。",
                    agent_name="leaf-writer",
                    role="worker",
                    allowed_tools=["write", "read_file", "list_files"],
                )
            ],
            apply=True,
        )
    )
    leaf = manager.load(result.created_run_ids[0])

    assert "write" in leaf.allowed_tools
    assert "write_file" in leaf.allowed_tools
    assert "read_artifact" in leaf.allowed_tools
    assert "apply_patch" in leaf.allowed_tools
    assert "apply_patch" in leaf.allowed_tools


def test_hierarchy_schedule_carries_parent_context_to_child_thought(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(
        goal="父级收口文件，且 coordinator 不能代写。",
        thought="父级补充：只能通过当前节点继续派下一层。",
        plan=["plan"],
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(parent_run_id=root.id, child_specs=_child_specs(1), apply=True)
    )
    child = manager.load(result.created_run_ids[0])

    assert "父级收口文件" in child.thought
    assert "父级补充：只能通过当前节点继续派下一层" in child.thought
    assert "必须把下一层 goal 写成自包含任务" in child.thought


def test_hierarchy_schedule_carries_parent_boundary_into_vague_child_goal(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(
        goal="需要 proof 目录里的 solution.py。",
        thought="root thought",
        plan=["plan"],
        extra_write_roots=[str(deliverables)],
        attributes={"required_files": [str(deliverables / "leaf_outputs" / "proof" / "solution.py")]},
    )

    child_result = _schedule_vague_child(manager, root.id)
    child = manager.load(child_result.created_run_ids[0])

    assert "创建 leaf worker，实现 add(a,b) 并写测试" in child.goal
    assert str(deliverables / "leaf_outputs" / "proof" / "solution.py") in child.goal
    assert "继承父级目标/边界" in child.goal
    assert "inherited_parent_context=true" not in child.goal
    assert child.attributes.get("inherited_parent_context") is True

    leaf_result = _schedule_vague_leaf(manager, child.id)
    leaf = manager.load(leaf_result.created_run_ids[0])

    assert str(deliverables / "leaf_outputs" / "proof" / "solution.py") in leaf.goal
    assert leaf.attributes.get("inherited_parent_context") is True
    assert "write_file" in leaf.allowed_tools


def test_hierarchy_schedule_keeps_sibling_scope_out_of_child_handoff(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(
        goal="只负责 arithmetic 领域，并交付 solution.py。",
        thought="root 只做分派。",
        plan=["plan"],
        extra_write_roots=[str(deliverables)],
        attributes={
            "required_files": [str(deliverables / "leaf_outputs" / "worker_arithmetic" / "solution.py")],
        },
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="arithmetic领域：创建worker_arithmetic，写入solution.py",
                    role="coordinator",
                    agent_name="arithmetic-lead",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
                )
            ],
            apply=True,
        )
    )
    child = manager.load(result.created_run_ids[0])

    assert "worker_arithmetic" in child.goal
    assert "worker_text" not in child.goal
    assert "worker_text" not in child.thought
    assert "当前子任务只执行" in child.goal


def test_hierarchy_schedule_keeps_exact_file_contract_when_child_goal_only_has_dir(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(
        goal="只负责 arithmetic 领域，并交付指定文件。",
        thought="root 只做分派。",
        plan=["plan"],
        extra_write_roots=[str(deliverables)],
        attributes={
            "required_files": [
                str(deliverables / "leaf_outputs" / "worker_arithmetic" / "solution.py"),
                "test_solution.py",
                "README.md",
            ]
        },
    )

    child_result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=f"创建 arithmetic worker，产物写至 {deliverables}/leaf_outputs/worker_arithmetic/",
                    role="coordinator",
                    agent_name="arithmetic-lead",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
                )
            ],
            apply=True,
        )
    )
    child = manager.load(child_result.created_run_ids[0])

    assert "worker_arithmetic/solution.py" in child.goal
    assert "test_solution.py" in child.goal
    assert "README.md" in child.goal
    assert "worker_text" not in child.goal

    leaf_result = _schedule_vague_leaf(manager, child.id)
    leaf = manager.load(leaf_result.created_run_ids[0])

    assert "worker_arithmetic/solution.py" in leaf.goal
    assert "test_solution.py" in leaf.goal
    assert "README.md" in leaf.goal


def test_hierarchy_schedule_keeps_controlled_exec_contract_for_leaf(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = _controlled_exec_contract_root(manager, deliverables)

    child_result = _schedule_vague_child(manager, root.id)
    early_leaf_result = _schedule_vague_leaf(manager, child_result.created_run_ids[0])
    assert early_leaf_result.blocked is False
    early_leaf = manager.load(early_leaf_result.created_run_ids[0])
    assert early_leaf.attributes["controlled_exec_contract_required"] is True
    assert "controlled_exec" in early_leaf.attributes["required_tools"]
    assert any("task_trash" in item for item in early_leaf.attributes["capability_contracts"])
    leaf = _schedule_controlled_exec_contract_leaf(manager, child_result.created_run_ids[0])

    assert leaf.attributes["controlled_exec_contract_required"] is True
    assert "controlled_exec" in leaf.attributes["required_tools"]
    assert any("capability_request" in item for item in leaf.attributes["capability_contracts"])
    assert any("requested_commands" in item for item in leaf.attributes["capability_contracts"])
    assert any("task_trash" in item for item in leaf.attributes["capability_contracts"])
    assert any("stdout_ref" in item for item in leaf.attributes["capability_contracts"])



def _controlled_exec_contract_root(manager: SubAgentManager, deliverables):
    return manager.create_run(
        goal=(
            f"在 {deliverables} 交付 controlled_exec 验收包。"
            "depth=3 worker 必须先写 sentinel.txt，然后提交 capability_request："
            "requested_tools=[\"controlled_exec\"], requested_commands=[\"pwd\",\"python3\",\"rm\"], "
            "path_scope 限定 task_dir，output_budget 包含 stdout_bytes/stderr_bytes。"
            "grant 后必须用 controlled_exec 执行 pwd、大输出 python3，并验证 rm 走 task_trash/move_to_task_trash；"
            "refs 必须包含 stdout_ref、audit_ref、trash_manifest_ref。"
        ),
        thought="root 只观察，不替 leaf 执行。",
        plan=["plan"],
        extra_write_roots=[str(deliverables)],
        attributes={
            "controlled_exec_contract_required": True,
            "required_tools": ["controlled_exec"],
            "capability_contracts": [
                'capability_request: requested_tools=["controlled_exec"], requested_commands=["pwd","python3","rm"]',
                "path_scope=task_dir; output_budget=stdout_bytes/stderr_bytes",
                "trash_policy: rm must use task_trash/move_to_task_trash",
                "refs_required: stdout_ref, audit_ref, trash_manifest_ref",
            ],
        },
    )


def _schedule_controlled_exec_contract_leaf(manager: SubAgentManager, child_id: str):
    grand_result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child_id,
            child_specs=[
                HierarchyChildSpec(
                    goal="继续创建 depth=3 worker，保留 controlled_exec 合同。",
                    role="coordinator",
                    agent_name="grand-lead",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
                )
            ],
            apply=True,
            max_depth=3,
        )
    )
    leaf_result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=grand_result.created_run_ids[0],
            child_specs=[
                HierarchyChildSpec(
                    goal="执行 leaf 工作。",
                    role="worker",
                    agent_name="leaf",
                )
            ],
            apply=True,
            max_depth=3,
        )
    )
    return manager.load(leaf_result.created_run_ids[0])

def _schedule_vague_child(manager: SubAgentManager, parent_id: str):
    return manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent_id,
            child_specs=[
                HierarchyChildSpec(
                    goal="创建 leaf worker，实现 add(a,b) 并写测试",
                    role="coordinator",
                    agent_name="child",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
                )
            ],
            apply=True,
        )
    )


def _schedule_vague_leaf(manager: SubAgentManager, parent_id: str):
    return manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent_id,
            child_specs=[
                HierarchyChildSpec(
                    goal="实现 add(a,b)",
                    role="worker",
                    agent_name="leaf",
                )
            ],
            apply=True,
            max_depth=2,
        )
    )
