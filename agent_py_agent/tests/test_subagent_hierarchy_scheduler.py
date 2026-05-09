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
        goal=f"必须让 leaf 写到 {deliverables}/leaf_outputs/proof/solution.py，coordinator 不得代写。",
        thought="root thought",
        plan=["plan"],
        extra_write_roots=[str(deliverables)],
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


# LLM: test_hierarchy_schedule_infers_coordinator_role_from_tools keeps model role slips recoverable.
# 函数用途: 当模型把带层级调度权限的下一层误写成 worker 时，系统按 depth 纠正为 coordinator。
def test_hierarchy_schedule_infers_coordinator_role_from_tools(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(goal="root", thought="split", plan=["plan"])
    child = manager.create_run(
        goal="child",
        thought="coordinate",
        plan=["plan"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="创建三个 leaf worker 并调度执行。",
                    agent_name="child-01-grandchild-01",
                    role="worker",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
                )
            ],
            apply=True,
        )
    )
    grandchild = manager.load(result.created_run_ids[0])

    assert grandchild.role == "grandchild_coordinator"


# LLM: test_hierarchy_schedule_blocks_depth_and_child_limits keeps fan-out bounded.
# 函数用途: 确认超过最大深度或最大子任务数量时不会创建新任务。
def test_hierarchy_schedule_blocks_depth_and_child_limits(tmp_path):
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="orchestrate", plan=["plan"])
    child_result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(parent_run_id=root.id, child_specs=_child_specs(1), apply=True)
    )
    child_id = child_result.created_run_ids[0]

    too_deep = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child_id,
            child_specs=_child_specs(1),
            apply=True,
            max_depth=1,
        )
    )
    assert too_deep.blocked is True
    assert too_deep.reason == "max_depth_exceeded:1"
    assert manager.load(child_id).child_ids == []

    too_many = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=_child_specs(2),
            apply=True,
            max_children=2,
        )
    )
    assert too_many.blocked is True
    assert too_many.reason == "max_children_exceeded:2"
    assert manager.load(root.id).child_ids == [child_id]


# LLM: test_subagents_hierarchy_cli_is_dry_run_by_default covers the command boundary.
# 函数用途: 确认 CLI 可以解析 child spec，默认不写入，输出可读 JSON 摘要。
def test_subagents_hierarchy_cli_is_dry_run_by_default(tmp_path, capsys):
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="parent", thought="split", plan=["plan"])
    agent = MagicMock()
    agent.subagents = manager

    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(tmp_path / "config.yaml"),
            "subagents-hierarchy",
            parent.id,
            "--child",
            "checker:check-agent:check output quality",
            "--json",
        ]
    )
    with patch("agent_py_agent.cli._hierarchy.make_agent", return_value=agent):
        code = args.func(args)

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["dry_run"] is True
    assert payload["blocked"] is False
    assert payload["items"][0]["role"] == "checker"
    assert manager.load(parent.id).child_ids == []


# LLM: test_subagents_hierarchy_cli_apply_creates_child proves explicit materialization.
# 函数用途: 确认 CLI 只有带 --apply 才创建子任务，并返回 created_run_ids。
def test_subagents_hierarchy_cli_apply_creates_child(tmp_path, capsys):
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="parent", thought="split", plan=["plan"])
    agent = MagicMock()
    agent.subagents = manager

    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(tmp_path / "config.yaml"),
            "subagents-hierarchy",
            parent.id,
            "--child",
            "reporter:report-agent:collect refs and summarize",
            "--apply",
            "--json",
        ]
    )
    with patch("agent_py_agent.cli._hierarchy.make_agent", return_value=agent):
        code = args.func(args)

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["dry_run"] is False
    assert len(payload["created_run_ids"]) == 1
    assert manager.load(parent.id).child_ids == payload["created_run_ids"]
