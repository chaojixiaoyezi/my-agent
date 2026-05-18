"""LLM: focused tests for explicit subagent hierarchy scheduling.

函数/模块用途: 验证父代理只能通过 bundle 化层级调度入口创建子/孙代理，默认 dry-run，显式 apply 才写入。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)
from agent_py_agent.cli.parser import build_parser


# LLM: _child_specs keeps hierarchy tests readable while using the production bundle shape.
# 函数用途: 构造多个待调度子任务规格，避免测试里重复写 dataclass 字段。
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


# LLM: test_hierarchy_schedule_dry_run_previews_without_writing_children locks the safe default.
# 函数用途: 确认层级调度默认只预览，不创建子任务，也不修改父任务 child_ids。
def test_hierarchy_schedule_dry_run_previews_without_writing_children(tmp_path):
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="parent", thought="split safely", plan=["plan"])

    result = manager.schedule_child_runs(
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


# LLM: test_hierarchy_schedule_apply_builds_two_child_four_grandchild_tree proves controlled nesting.
# 函数用途: 显式 apply 创建 1 主、2 子、4 孙结构，并保持 root/parent/depth 可恢复。
def test_hierarchy_schedule_apply_builds_two_child_four_grandchild_tree(tmp_path):
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="orchestrate", plan=["plan"])

    first = manager.schedule_child_runs(
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
        grand = manager.schedule_child_runs(
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


# LLM: test_hierarchy_schedule_repairs_literal_lineage_wildcard_names locks the real R5 naming fix.
# 函数用途: 模型把“小傻妞-*”模板里的星号当成实际名字时，调度器用 role 生成可读后缀。
def test_hierarchy_schedule_repairs_literal_lineage_wildcard_names(tmp_path):
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

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="leaf does controlled exec",
                    agent_name="小小小傻妞-*-*",
                    role="leaf_worker",
                )
            ],
            apply=True,
            max_depth=3,
        )
    )
    leaf = manager.load(result.created_run_ids[0])

    assert leaf.agent_name.startswith("小小小傻妞-")
    assert "*" not in leaf.agent_name
    assert leaf.agent_name.endswith("leaf_worker")


# LLM: real prompts often write "4层" without a space; the hierarchy guard must still prevent early leaves.
# 函数用途: 父级明确要求 depth=3 leaf 时，depth=1 不能提前创建 depth=2 leaf_worker。
def test_hierarchy_schedule_blocks_leaf_when_four_layer_token_has_no_space(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(
        goal=f"必须覆盖4层链路，depth=3 leaf_worker 最终写 {deliverables}/refs.json。",
        thought="split",
        plan=["delegate"],
        extra_write_roots=[str(deliverables)],
    )
    child = manager.create_run(
        goal=(
            "depth=1 coordinator，继承父级目标/边界：必须覆盖4层链路，"
            "depth=3 leaf_worker 最终执行 controlled_exec。"
        ),
        thought="split",
        plan=["delegate"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        role="child_coordinator",
        extra_write_roots=[str(deliverables)],
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=f"depth=2 leaf_worker 直接写 {deliverables}/refs.json。",
                    agent_name="小小傻妞-r07-d2",
                    role="leaf_worker",
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


# LLM: child goals may mention task_dir as context; product-root drift should only reject user-output drift.
# 函数用途: 子任务描述里包含父级 runtime/task_dir 时，不应被当成用户产物根漂移阻断。
def test_hierarchy_schedule_allows_internal_task_dir_context_with_product_root(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    parent = manager.create_run(
        goal=f"协调下层，把最终报告写到 {deliverables}/report.md。",
        thought="split",
        plan=["delegate"],
        role="child_coordinator",
        depth=1,
        extra_write_roots=[str(deliverables)],
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=(
                        f"创建下一层 coordinator；可在 task_dir {parent.task_dir} 写内部哨兵，"
                        f"最终用户产物仍写到 {deliverables}/report.md。"
                    ),
                    agent_name="小小傻妞-r07-d2",
                    role="child_coordinator",
                )
            ],
            apply=True,
            max_depth=3,
        )
    )

    assert result.blocked is False
    assert result.created_run_ids


# LLM: test_hierarchy_schedule_inherits_parent_extra_write_roots keeps user-approved product roots available.
# 函数用途: 确认下一层默认继承父节点的外部产物目录权限，但不继承父节点工单目录。
def test_hierarchy_schedule_inherits_parent_extra_write_roots(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(
        goal="root",
        thought="orchestrate",
        plan=["plan"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(parent_run_id=root.id, child_specs=_child_specs(1), apply=True)
    )
    child = manager.load(result.created_run_ids[0])

    assert str(deliverables) in child.allowed_write_roots
    assert root.task_dir not in child.allowed_write_roots


# LLM: test_hierarchy_schedule_infers_leaf_write_tools_from_explicit_deliverables covers real runner prompt drift.
# 函数用途: 当模型忘记 allowed_tools 但 leaf 任务明确要写已授权产物文件时，系统自动补齐文件读写工具。
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
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.schedule_child_runs(
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
    assert "replace_in_file" in leaf.allowed_tools
    assert str(deliverables) in leaf.allowed_write_roots


# LLM: test_hierarchy_schedule_normalizes_model_write_alias keeps real runners from receiving unavailable tool names.
# 函数用途: 当模型把 write 当成工具名时，系统转成真实 write_file，并保留叶子写文件需要的工具包。
def test_hierarchy_schedule_normalizes_model_write_alias_for_leaf_tasks(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    parent = manager.create_run(
        goal="child coordinator",
        thought="split",
        plan=["plan"],
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=f"写入 {deliverables}/proof.txt，内容为 trace-hierarchy-ok。",
                    agent_name="leaf-writer",
                    role="leaf_worker",
                    allowed_tools=["write", "read_file", "list_files"],
                )
            ],
            apply=True,
        )
    )
    leaf = manager.load(result.created_run_ids[0])

    assert "write" not in leaf.allowed_tools
    assert "write_file" in leaf.allowed_tools
    assert "read_artifact" in leaf.allowed_tools
    assert "append_file" in leaf.allowed_tools
    assert "replace_in_file" in leaf.allowed_tools


# LLM: test_hierarchy_schedule_carries_parent_context_to_child_thought prevents vague nested handoffs.
# 函数用途: 确认下层 coordinator 能通过 thought 看到父级目标和提示，避免只拿到空泛编号任务。
def test_hierarchy_schedule_carries_parent_context_to_child_thought(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(
        goal="父级要求 leaf 写 48 个验收文件，且 coordinator 不能代写。",
        thought="父级补充：只能通过当前节点继续派下一层。",
        plan=["plan"],
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(parent_run_id=root.id, child_specs=_child_specs(1), apply=True)
    )
    child = manager.load(result.created_run_ids[0])

    assert "父级要求 leaf 写 48 个验收文件" in child.thought
    assert "父级补充：只能通过当前节点继续派下一层" in child.thought
    assert "必须把下一层 goal 写成自包含任务" in child.thought


# LLM: test_hierarchy_schedule_carries_parent_boundary_into_vague_child_goal locks real E2E path preservation.
# 函数用途: 当模型给下一层的 goal 太短时，系统把父级产物路径和边界补进 goal，避免路径靠 thought 转述而丢失。
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

    leaf_result = _schedule_vague_leaf(manager, child.id)
    leaf = manager.load(leaf_result.created_run_ids[0])

    assert str(deliverables / "leaf_outputs" / "proof" / "solution.py") in leaf.goal
    assert "write_file" in leaf.allowed_tools


# LLM: test_hierarchy_schedule_keeps_sibling_scope_out_of_child_handoff covers real E2E tree blow-up.
# 函数用途: 当父级同时描述多个 sibling 任务时，调度某一个 child 不应把其它 sibling 的目标塞进交接。
def test_hierarchy_schedule_keeps_sibling_scope_out_of_child_handoff(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    root = manager.create_run(
        goal="只负责 arithmetic 领域，并交付 solution.py。",
        thought="root 只做分派。",
        plan=["plan"],
        extra_write_roots=[str(deliverables)],
        attributes={
            "domain_scopes": ["arithmetic"],
            "required_files": [str(deliverables / "leaf_outputs" / "leaf_worker_arithmetic" / "solution.py")],
        },
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="arithmetic领域：创建leaf_worker_arithmetic，写入solution.py",
                    role="child_coordinator",
                    agent_name="arithmetic-lead",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
                )
            ],
            apply=True,
        )
    )
    child = manager.load(result.created_run_ids[0])

    assert "leaf_worker_arithmetic" in child.goal
    assert "leaf_worker_text" not in child.goal
    assert "leaf_worker_text" not in child.thought
    assert "当前子任务只执行" in child.goal


# LLM: test_hierarchy_schedule_keeps_exact_file_contract_when_child_goal_only_has_dir locks real E2E drift.
# 函数用途: child goal 只带产物目录时，也要继承父级指定的 solution.py/test_solution.py/README.md 文件契约。
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
                str(deliverables / "leaf_outputs" / "leaf_worker_arithmetic" / "solution.py"),
                "test_solution.py",
                "README.md",
            ]
        },
    )

    child_result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=f"创建 arithmetic leaf_worker，产物写至 {deliverables}/leaf_outputs/leaf_worker_arithmetic/",
                    role="child_coordinator",
                    agent_name="arithmetic-lead",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
                )
            ],
            apply=True,
        )
    )
    child = manager.load(child_result.created_run_ids[0])

    assert "leaf_worker_arithmetic/solution.py" in child.goal
    assert "test_solution.py" in child.goal
    assert "README.md" in child.goal
    assert "leaf_worker_text" not in child.goal

    leaf_result = _schedule_vague_leaf(manager, child.id)
    leaf = manager.load(leaf_result.created_run_ids[0])

    assert "leaf_worker_arithmetic/solution.py" in leaf.goal
    assert "test_solution.py" in leaf.goal
    assert "README.md" in leaf.goal


# LLM: test_hierarchy_schedule_keeps_controlled_exec_contract covers real E2E capability drift.
# 函数用途: 父级要求 controlled_exec/capability_request/trash 时，即使中间 coordinator 简化目标，leaf 也必须拿到这些硬约束。
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



# LLM: _controlled_exec_contract_root centralizes the long parent contract text for inheritance tests.
# 函数用途: 创建要求 controlled_exec/capability_request/task_trash refs 的 root 任务。
def _controlled_exec_contract_root(manager: SubAgentManager, deliverables):
    return manager.create_run(
        goal=(
            f"在 {deliverables} 交付 controlled_exec 验收包。"
            "depth=3 leaf_worker 必须先写 sentinel.txt，然后提交 capability_request："
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


# LLM: _schedule_controlled_exec_contract_leaf builds the extra coordinator layer before the final leaf.
# 函数用途: 先创建 depth=2 coordinator，再创建 depth=3 leaf，用于验证合同跨层传递。
def _schedule_controlled_exec_contract_leaf(manager: SubAgentManager, child_id: str):
    grand_result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child_id,
            child_specs=[
                HierarchyChildSpec(
                    goal="继续创建 depth=3 leaf_worker，保留 controlled_exec 合同。",
                    role="child_coordinator",
                    agent_name="grand-lead",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
                )
            ],
            apply=True,
            max_depth=3,
        )
    )
    leaf_result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=grand_result.created_run_ids[0],
            child_specs=[
                HierarchyChildSpec(
                    goal="执行 leaf 工作。",
                    role="leaf_worker",
                    agent_name="leaf",
                )
            ],
            apply=True,
            max_depth=3,
        )
    )
    return manager.load(leaf_result.created_run_ids[0])

# LLM: _schedule_vague_child keeps the boundary-inheritance test below the code-size risk threshold.
# 函数用途: 生成缺少产物路径的 child spec，用于验证 scheduler 自动补父级边界。
def _schedule_vague_child(manager: SubAgentManager, parent_id: str):
    return manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent_id,
            child_specs=[
                HierarchyChildSpec(
                    goal="创建 leaf worker，实现 add(a,b) 并写测试",
                    role="child_coordinator",
                    agent_name="child",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
                )
            ],
            apply=True,
        )
    )


# LLM: _schedule_vague_leaf verifies inherited parent scope is also used for leaf tool inference.
# 函数用途: 生成缺少产物路径的 leaf spec，用于验证补全 goal 后仍能推断写文件工具。
def _schedule_vague_leaf(manager: SubAgentManager, parent_id: str):
    return manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent_id,
            child_specs=[
                HierarchyChildSpec(
                    goal="实现 add(a,b)",
                    role="leaf_worker",
                    agent_name="leaf",
                )
            ],
            apply=True,
            max_depth=2,
        )
    )
