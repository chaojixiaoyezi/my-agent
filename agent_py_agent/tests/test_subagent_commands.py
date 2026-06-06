"""subagent_commands CLI 命令测试。

测试 subagent list/show/run/workflow 命令。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestCmdSubagents:
    """测试 cmd_subagents 命令（子代理看板）。"""

    def test_cmd_subagents_basic(self, tmp_path: Path):
        """正常显示子代理看板。"""
        from agent_py_agent.cli.subagents import cmd_subagents

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.capability_config = str(tmp_path / "capability.yaml")
        args.limit = 10
        args.all = False
        args.status = None
        args.owner = None
        args.root_id = None
        args.skill_dir = None

        mock_agent = MagicMock()
        mock_board = MagicMock()
        mock_board.summary = {"total": 0}
        mock_board.hot_list = []
        mock_board.recent = []
        mock_agent.subagents.write_board.return_value = mock_board

        with patch("agent_py_agent.cli._board.make_agent", return_value=mock_agent):
            result = cmd_subagents(args)
            assert result == 0

    def test_cmd_subagents_passes_scope_to_board_builder(self, tmp_path: Path):
        """CLI 范围参数应直接传给 board 生成，避免写出全局看板后再二次过滤。"""
        from agent_py_agent.cli.subagents import cmd_subagents

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.capability_config = str(tmp_path / "capability.yaml")
        args.limit = 10
        args.all = False
        args.status = "RUNNING"
        args.owner = "alice"
        args.root_id = "root-a"
        args.skill_dir = None

        mock_agent = MagicMock()
        mock_board = MagicMock()
        mock_board.summary = {"total": 0}
        mock_board.hot_list = []
        mock_board.recent = []
        mock_agent.subagents.write_board.return_value = mock_board

        with patch("agent_py_agent.cli._board.make_agent", return_value=mock_agent):
            assert cmd_subagents(args) == 0

        options = mock_agent.subagents.write_board.call_args.kwargs["options"]
        assert options.root_id == "root-a"
        assert options.status == "RUNNING"
        assert options.owner == "alice"

    def test_cmd_subagents_with_items(self, tmp_path: Path):
        """显示带有子代理项的看板。"""
        from agent_py_agent.cli.subagents import cmd_subagents

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.capability_config = str(tmp_path / "capability.yaml")
        args.limit = 10
        args.all = False
        args.status = None
        args.owner = None
        args.root_id = None
        args.skill_dir = None

        mock_item = MagicMock()
        mock_item.id = "run_001"
        mock_item.status = "RUNNING"
        mock_item.verification_status = "pending"
        mock_item.channel_status = "ok"
        mock_item.depth = 0
        mock_item.owner = "parent_agent"
        mock_item.final_owner = None
        mock_item.evidence_count = 2
        mock_item.open_request_count = 0
        mock_item.open_gap_count = 0
        mock_item.risk_flags = []
        mock_item.goal = "测试子代理任务"

        mock_board = MagicMock()
        mock_board.summary = {"total": 1, "running": 1}
        mock_board.hot_list = [mock_item]
        mock_board.recent = []
        mock_agent = MagicMock()
        mock_agent.subagents.write_board.return_value = mock_board

        with patch("agent_py_agent.cli._board.make_agent", return_value=mock_agent):
            result = cmd_subagents(args)
            assert result == 0


class TestCmdSubagentsDueCheck:
    """测试 cmd_subagents_due_check 命令。"""

    def test_cmd_subagents_due_check_no_issues(self, tmp_path: Path):
        """没有问题时显示提示。"""
        from agent_py_agent.cli.subagents import cmd_subagents_due_check

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.capability_config = str(tmp_path / "capability.yaml")
        args.limit = 10
        args.all = False
        args.root_id = "root-a"
        args.skill_dir = None

        mock_agent = MagicMock()
        mock_report = MagicMock()
        mock_report.summary = {"total": 0}
        mock_report.issues = []
        mock_agent.subagents.write_due_check.return_value = mock_report

        with patch("agent_py_agent.cli._inspection.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli._inspection.load_capability_config", return_value=MagicMock()):
            result = cmd_subagents_due_check(args)
            assert result == 0
            params = mock_agent.subagents.write_due_check.call_args.kwargs["params"]
            assert params.root_id == "root-a"


class TestCmdSubagentsBudget:
    """测试 cmd_subagents_budget 命令。"""

    def test_cmd_subagents_budget_passes_scope_and_limits(self, tmp_path: Path):
        """预算命令应把 root 范围和阈值透传给 manager。"""
        from agent_py_agent.agent.subagents.run_budget import SubagentRunBudgetReport
        from agent_py_agent.cli.subagents import cmd_subagents_budget

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.root_id = "root-a"
        args.max_model_calls = 12
        args.max_tool_rounds = 20
        args.max_prompt_response_tokens = 50000
        args.include_dry_runs = False
        args.json = False

        mock_agent = MagicMock()
        mock_agent.subagents.workspace = tmp_path
        mock_agent.subagents.write_run_budget_report.return_value = SubagentRunBudgetReport(
            generated_at=1.0,
            root_id="root-a",
            totals={"model_calls": 3, "tool_rounds": 4, "prompt_response_token_estimate": 1000},
            limits={"model_calls": 12, "tool_rounds": 20, "prompt_response_token_estimate": 50000},
            exceeded=[],
        )

        with patch("agent_py_agent.cli._inspection.make_agent", return_value=mock_agent):
            result = cmd_subagents_budget(args)

        assert result == 0
        params = mock_agent.subagents.write_run_budget_report.call_args.kwargs["params"]
        assert params.root_id == "root-a"
        assert params.max_model_calls == 12
        assert params.max_tool_rounds == 20


class TestCmdSubagentsProbe:
    """测试 cmd_subagents_probe 命令。"""

    def test_cmd_subagents_probe_no_results(self, tmp_path: Path):
        """没有可检查的子代理时显示提示。"""
        from agent_py_agent.cli.subagents import cmd_subagents_probe

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.limit = 10
        args.run_id = None

        mock_agent = MagicMock()
        mock_report = MagicMock()
        mock_report.summary = {"total": 0}
        mock_report.results = []
        mock_agent.subagents.write_channel_probe_report.return_value = mock_report

        with patch("agent_py_agent.cli._inspection.make_agent", return_value=mock_agent):
            result = cmd_subagents_probe(args)
            assert result == 0


class TestCmdSubagentsPlanActions:
    """测试 cmd_subagents_plan_actions 命令。"""

    def test_cmd_subagents_plan_actions_no_actions(self, tmp_path: Path):
        """没有建议动作时显示提示。"""
        from agent_py_agent.cli.subagents import cmd_subagents_plan_actions

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.capability_config = str(tmp_path / "capability.yaml")
        args.limit = 10
        args.all = False
        args.root_id = "root-a"
        args.skill_dir = None

        mock_agent = MagicMock()
        mock_report = MagicMock()
        mock_report.summary = {"total": 0}
        mock_report.actions = []
        mock_agent.subagents.write_action_plan.return_value = mock_report

        with patch("agent_py_agent.cli._actions.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli._actions.load_capability_config", return_value=MagicMock()):
            result = cmd_subagents_plan_actions(args)
            assert result == 0
            call_kwargs = mock_agent.subagents.write_action_plan.call_args.kwargs
            assert call_kwargs["params"].root_id == "root-a"


class TestCmdSubagentsApplyActions:
    """测试 cmd_subagents_apply_actions 命令。"""

    def test_cmd_subagents_apply_actions_dry_run(self, tmp_path: Path):
        """Dry-run 模式执行动作计划。"""
        from agent_py_agent.cli.subagents import cmd_subagents_apply_actions

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.capability_config = str(tmp_path / "capability.yaml")
        args.apply = False
        args.action = None
        args.run_id = None
        args.take_over_by = None
        args.locked_file = None
        args.limit = 10
        args.skill_dir = None

        mock_agent = MagicMock()
        mock_report = MagicMock()
        mock_report.summary = {"total": 0}
        mock_report.records = []
        mock_agent.subagents.write_action_apply_report.return_value = mock_report

        with patch("agent_py_agent.cli._actions.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli._actions.load_capability_config", return_value=MagicMock()):
            result = cmd_subagents_apply_actions(args)
            assert result == 0
        call_kwargs = mock_agent.subagents.write_action_apply_report.call_args.kwargs
        assert call_kwargs["options"].apply is False
        assert call_kwargs["options"].locked_files == []


class TestCmdSubagentsRouteCapabilities:
    """测试 cmd_subagents_route_capabilities 命令。"""

    def test_cmd_subagents_route_capabilities_no_records(self, tmp_path: Path):
        """没有 capability request 时显示提示。"""
        from agent_py_agent.cli.subagents import cmd_subagents_route_capabilities

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.capability_config = str(tmp_path / "capability.yaml")
        args.apply = False
        args.run_id = None
        args.limit = 10
        args.skill_dir = None

        mock_agent = MagicMock()
        mock_router = MagicMock()
        mock_report = MagicMock()
        mock_report.summary = {"total": 0}
        mock_report.records = []
        mock_agent.subagents.write_capability_route_report.return_value = mock_report

        with patch("agent_py_agent.cli._actions.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli._actions.load_capability_config", return_value=MagicMock()), \
             patch("agent_py_agent.cli._actions.make_capability_router", return_value=mock_router):
            result = cmd_subagents_route_capabilities(args)
            assert result == 0


@pytest.mark.skip(reason="旧 subagents acceptance CLI 已删除，统一看 tree/board/closeout")
class TestCmdSubagentsAcceptance:
    """测试 cmd_subagents_acceptance 命令。"""

    def test_cmd_subagents_acceptance_no_records(self, tmp_path: Path):
        """没有等待收口的子代理时显示提示。"""
        from agent_py_agent.cli.subagents import cmd_subagents_acceptance

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.apply = False
        args.run_id = None
        args.reviewer = None
        args.note = ""
        args.limit = 10

        mock_agent = MagicMock()
        mock_report = MagicMock()
        mock_report.summary = {"total": 0}
        mock_report.records = []
        mock_agent.subagents.write_runner_result_report.return_value = mock_report

        with patch("agent_py_agent.cli._review.make_agent", return_value=mock_agent):
            result = cmd_subagents_acceptance(args)
            assert result == 0
        call_kwargs = mock_agent.subagents.write_runner_result_report.call_args.kwargs
        assert call_kwargs["run_ids"] is None
        assert call_kwargs["options"].apply is False
        assert call_kwargs["options"].reviewer == "parent"
        assert call_kwargs["options"].note == ""
        assert call_kwargs["options"].limit == 10


class TestCmdSubagentsPatches:
    """测试 cmd_subagents_patches 命令。"""

    def test_cmd_subagents_patches_review_dry_run(self, tmp_path: Path):
        """Patch 审核 dry-run 模式。"""
        from agent_py_agent.cli.subagents import cmd_subagents_patches

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.patch_action = "review"
        args.run_id = None
        args.reviewer = None
        args.note = ""
        args.limit = 10

        mock_agent = MagicMock()
        mock_report = MagicMock()
        mock_report.summary = {"total": 0}
        mock_report.records = []
        mock_agent.subagents.write_patch_review_report.return_value = mock_report

        with patch("agent_py_agent.cli._review.make_agent", return_value=mock_agent):
            result = cmd_subagents_patches(args)
            assert result == 0
        call_kwargs = mock_agent.subagents.write_patch_review_report.call_args.kwargs
        assert call_kwargs["run_ids"] is None
        assert call_kwargs["options"].apply is False
        assert call_kwargs["options"].reviewer == "parent"
        assert call_kwargs["options"].note == ""
        assert call_kwargs["options"].limit == 10


class TestCmdSubagentsDispatch:
    """测试 cmd_subagents_dispatch 命令。"""

    def test_cmd_subagents_dispatch_no_records(self, tmp_path: Path):
        """没有调度动作时显示提示。"""
        from agent_py_agent.cli.subagents import cmd_subagents_dispatch

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.capability_config = str(tmp_path / "capability.yaml")
        args.apply = False
        args.start_runners = False
        args.planner = False
        args.workflow_mode = None
        args.max_runners = None
        args.limit = 10
        args.reviewer = None
        args.note = ""
        args.instruction = None
        args.max_cards = None
        args.no_probe = False
        args.take_over_by = None
        args.locked_file = None
        args.interval = None
        args.max_cycles = None
        args.force_lock = False
        args.watch = False
        args.skill_dir = None

        mock_agent = MagicMock()
        mock_router = MagicMock()
        mock_report = MagicMock()
        mock_report.summary = {"total": 0}
        mock_report.records = []
        mock_agent.dispatch_subagents.return_value = mock_report

        with patch("agent_py_agent.cli._dispatch.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli._dispatch.load_capability_config", return_value=MagicMock()), \
             patch("agent_py_agent.cli._dispatch.make_capability_router", return_value=mock_router):
            result = cmd_subagents_dispatch(args)
            assert result == 0

    def test_cmd_subagents_dispatch_start_runners_requires_mutate_state(self, tmp_path: Path):
        """start_runners 必须和状态写回一起使用。"""
        from agent_py_agent.cli.subagents import cmd_subagents_dispatch

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.capability_config = str(tmp_path / "capability.yaml")
        args.apply = False
        args.start_runners = True  # 错误组合
        args.planner = False
        args.workflow_mode = None
        args.max_runners = None
        args.limit = 10
        args.reviewer = None
        args.note = ""
        args.instruction = None
        args.max_cards = None
        args.no_probe = False
        args.take_over_by = None
        args.locked_file = None
        args.interval = None
        args.max_cycles = None
        args.force_lock = False
        args.watch = False
        args.skill_dir = None

        with patch("agent_py_agent.cli._dispatch.make_agent", return_value=MagicMock()):
            result = cmd_subagents_dispatch(args)
            assert result == 2


class TestCmdSubagentRun:
    """测试 cmd_subagent_run 命令。"""

    def test_cmd_subagent_run_dry_run(self, tmp_path: Path):
        """Dry-run 运行子代理。"""
        from agent_py_agent.cli.subagents import cmd_subagent_run

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.run_id = "run_001"
        args.instruction = None
        args.execute = False
        args.max_cards = None
        args.no_probe = False

        mock_agent = MagicMock()
        mock_result = MagicMock()
        mock_result.ok = True
        mock_result.run_id = "run_001"
        mock_result.status = "RUNNING"
        mock_result.verification_status = "pending"
        mock_result.message = "Dry-run 成功"
        mock_result.execution_context_json = tmp_path / "exec.json"
        mock_result.result_json = tmp_path / "result.json"
        mock_result.result_file = tmp_path / "result.md"
        mock_result.prompt_file = None
        mock_result.response_file = None
        mock_agent.run_subagent.return_value = mock_result

        with patch("agent_py_agent.cli._dispatch.make_agent", return_value=mock_agent):
            result = cmd_subagent_run(args)
            assert result == 0

    def test_cmd_subagent_run_failure(self, tmp_path: Path):
        """子代理运行失败。"""
        from agent_py_agent.cli.subagents import cmd_subagent_run

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.run_id = "run_001"
        args.instruction = None
        args.execute = False
        args.max_cards = None
        args.no_probe = False

        mock_agent = MagicMock()
        mock_result = MagicMock()
        mock_result.ok = False
        mock_result.run_id = "run_001"
        mock_result.status = "FAILED"
        mock_result.verification_status = "pending"
        mock_result.message = "运行失败"
        mock_result.execution_context_json = tmp_path / "exec.json"
        mock_result.result_json = tmp_path / "result.json"
        mock_result.result_file = tmp_path / "result.md"
        mock_result.prompt_file = None
        mock_result.response_file = None
        mock_agent.run_subagent.return_value = mock_result

        with patch("agent_py_agent.cli._dispatch.make_agent", return_value=mock_agent):
            result = cmd_subagent_run(args)
            assert result == 1

    def test_cmd_subagent_run_execute_uses_timeout_worker(self, tmp_path: Path):
        """执行模式通过带超时的 worker 跑，避免 CLI 入口裸跑模型卡住。"""
        from agent_py_agent.cli.subagents import cmd_subagent_run

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.run_id = "run_001"
        args.instruction = "继续"
        args.execute = True
        args.max_cards = 3
        args.no_probe = False

        mock_task = MagicMock()
        mock_agent = MagicMock()
        mock_agent.root = tmp_path
        mock_agent.local_store = None
        mock_agent.subagents.load.return_value = mock_task

        mock_result = MagicMock()
        mock_result.ok = False
        mock_result.run_id = "run_001"
        mock_result.status = "TIMEOUT"
        mock_result.verification_status = "UNVERIFIED"
        mock_result.message = "runner timed out after 0.05s"
        mock_result.execution_context_json = tmp_path / "exec.json"
        mock_result.result_json = tmp_path / "result.json"
        mock_result.result_file = tmp_path / "result.md"
        mock_result.prompt_file = None
        mock_result.response_file = None

        with (
            patch("agent_py_agent.cli._dispatch.make_agent", return_value=mock_agent),
            patch("agent_py_agent.cli._dispatch._cli_subagent_run_timeout", return_value=0.05),
            patch("agent_py_agent.cli._dispatch._run_subagent_worker", return_value=mock_result) as worker,
        ):
            result = cmd_subagent_run(args)

        assert result == 1
        worker_params = worker.call_args.args[0]
        assert worker_params.run_id == "run_001"
        assert worker_params.dry_run is False
        assert worker_params.instruction == "继续"
        assert worker_params.timeout_seconds == 0.05
        mock_agent.run_subagent.assert_not_called()


class TestCmdSubagentDetail:
    """测试 cmd_subagent_detail 命令。"""

    def test_cmd_subagent_detail_success(self, tmp_path: Path, capsys):
        """显示子代理详情。"""
        from agent_py_agent.cli.subagents import cmd_subagent_detail

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.run_id = "run_001"

        mock_agent = MagicMock()

        # 创建一个简单的 mock 对象，其 __dict__ 可以被 json 序列化
        class SimpleMockTask:
            def __init__(self):
                self.task_id = "run_001"
                self.status = "RUNNING"
                self.goal = "测试任务"

        mock_agent.subagents.load.return_value = SimpleMockTask()

        with patch("agent_py_agent.cli._board.make_agent", return_value=mock_agent):
            result = cmd_subagent_detail(args)
            assert result == 0


class TestCmdSubagentsWorkflowPlan:
    """测试 cmd_subagents_workflow_plan 命令。"""

    def test_cmd_subagents_workflow_plan_basic(self, tmp_path: Path):
        """正常生成 workflow plan。"""
        from agent_py_agent.cli.subagents import cmd_subagents_workflow_plan

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.goal = "测试工作流"
        args.template_id = None
        args.output_dir = None
        args.json = False

        mock_result = MagicMock()
        mock_result.to_dict.return_value = {
            "mode": "auto",
            "enabled": True,
            "ok": True,
            "needs_confirmation": False,
            "selected_template_id": "template_001",
            "task_type": "sequential",
            "reason": "测试",
            "worker_count": 2,
            "workers": [],
            "final_closeout_check_count": 0,
            "final_closeout_checklist": [],
            "issues": []
        }

        with patch("agent_py_agent.cli._dispatch.load_config", return_value=MagicMock()), \
             patch("agent_py_agent.cli._dispatch.plan_workflow_for_goal", return_value=mock_result):
            result = cmd_subagents_workflow_plan(args)
            assert result == 0
