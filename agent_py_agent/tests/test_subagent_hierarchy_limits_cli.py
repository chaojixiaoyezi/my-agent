"""LLM: split hierarchy scheduling tests for focused guard coverage.

函数/模块用途: 验证层级调度的边界场景，同时让单个测试文件保持可维护大小。
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
from agent_py_agent.tests.test_subagent_hierarchy_scheduler import _child_specs


def test_hierarchy_schedule_blocks_explicit_depth_and_child_limits(tmp_path):
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="orchestrate", plan=["plan"])
    child_result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(parent_run_id=root.id, child_specs=_child_specs(1), apply=True)
    )
    child_id = child_result.created_run_ids[0]

    too_deep = manager.hierarchy.schedule_child_runs(
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

    too_many = manager.hierarchy.schedule_child_runs(
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


def test_hierarchy_schedule_allows_mixed_coordinator_and_leaf_children(tmp_path):
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="orchestrate", plan=["plan"])
    child = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(parent_run_id=root.id, child_specs=_child_specs(1), apply=True)
    )
    parent_id = child.created_run_ids[0]

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent_id,
            child_specs=[
                HierarchyChildSpec(goal="create next coordinator", role="coordinator"),
                HierarchyChildSpec(goal="write final proof", role="worker"),
            ],
            apply=True,
        )
    )

    assert result.blocked is False
    assert not hasattr(result, "scheduling_warnings")
    assert len(manager.load(parent_id).child_ids) == 2


def test_hierarchy_schedule_default_depth_is_unlimited(tmp_path):
    """默认不再因为固定层数阻断，只有显式 max_depth 才挡。"""
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="orchestrate", plan=["plan"])
    child = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(parent_run_id=root.id, child_specs=_child_specs(1), apply=True)
    )
    grandchild = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(parent_run_id=child.created_run_ids[0], child_specs=_child_specs(1), apply=True)
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=grandchild.created_run_ids[0],
            child_specs=_child_specs(1),
            apply=True,
        )
    )

    assert result.blocked is False
    assert result.created_run_ids


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
