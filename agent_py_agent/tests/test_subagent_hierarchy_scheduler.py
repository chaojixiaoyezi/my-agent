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
