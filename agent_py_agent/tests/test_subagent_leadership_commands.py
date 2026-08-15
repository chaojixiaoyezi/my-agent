"""LLM: CLI tests for subagent leadership recovery command bundles.

模块用途: 覆盖 coordinator 领导权恢复 plan/apply 命令的 bundle 参数传递。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


class TestCmdSubagentsLeadershipRecoveryPlan:
    """测试 cmd_subagents_leadership_recovery_plan 命令。"""

    def test_cmd_subagents_leadership_recovery_plan_passes_bundle(self, tmp_path: Path):
        """领导权恢复计划 CLI 应把 root、leader 和容量传给业务层 bundle。"""
        from agent_py_agent.cli.subagents import cmd_subagents_leadership_recovery_plan

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.capability_config = str(tmp_path / "capability.yaml")
        args.root_id = "root-a"
        args.leader = ["leader-a", "leader-b"]
        args.max_children_per_leader = 3
        args.json = False

        mock_agent = MagicMock()
        mock_report = MagicMock()
        mock_report.summary = {"assignments": 0, "assigned_children": 0, "unassigned_children": 0}
        mock_report.assignments = []
        mock_report.unassigned = []
        mock_agent.subagents.write_leadership_recovery_plan.return_value = mock_report

        with patch("agent_py_agent.cli._leadership.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli._leadership.load_capability_config", return_value=MagicMock()):
            result = cmd_subagents_leadership_recovery_plan(args)

        assert result == 0
        call_kwargs = mock_agent.subagents.write_leadership_recovery_plan.call_args.kwargs
        assert call_kwargs["params"].root_id == "root-a"
        assert call_kwargs["params"].leader_ids == ["leader-a", "leader-b"]
        assert call_kwargs["params"].max_children_per_leader == 3

    def test_cmd_subagents_leadership_recovery_apply_passes_bundle(self, tmp_path: Path):
        """领导权恢复 apply CLI 应只通过 bundle 传递显式 child 子集。"""
        from agent_py_agent.cli.subagents import cmd_subagents_leadership_recovery_apply

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.root_id = "root-a"
        args.coordinator = "coord-a"
        args.leader = "leader-a"
        args.child_run_id = ["child-1", "child-2"]
        args.max_children_per_leader = 3
        args.apply = True
        args.json = False

        mock_agent = MagicMock()
        mock_report = MagicMock()
        mock_report.summary = {"total": 1, "ok": 1, "applied": 1, "moved_children": 2}
        mock_report.records = []
        mock_agent.subagents.write_leadership_recovery_apply.return_value = mock_report

        with patch("agent_py_agent.cli._leadership.make_agent", return_value=mock_agent):
            result = cmd_subagents_leadership_recovery_apply(args)

        assert result == 0
        call_kwargs = mock_agent.subagents.write_leadership_recovery_apply.call_args.kwargs
        assert call_kwargs["params"].root_id == "root-a"
        assert call_kwargs["params"].coordinator_id == "coord-a"
        assert call_kwargs["params"].leader_id == "leader-a"
        assert call_kwargs["params"].child_ids == ["child-1", "child-2"]
        assert call_kwargs["params"].apply is True
        assert call_kwargs["params"].max_children_per_leader == 3
