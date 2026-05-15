"""Split orchestration tool execution tests for code-size guard clarity."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


class TestDispatchSubagentsToolExecute:
    """测试 DispatchSubagentsTool.execute() 方法。"""

    def test_execute_runners_requires_apply(self):
        """execute_runners=true 必须配合 apply=true。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.tools.specs.return_value = []

        tool = DispatchSubagentsTool(mock_agent)
        result = tool.execute({"execute_runners": True, "apply": False})

        assert result.ok is False
        assert "apply" in result.output.lower()

    def test_dispatch_without_apply(self):
        """apply=false 时执行 dry-run。"""
        from agent_py_agent.agent.agent_core.dispatch_params import DispatchParams
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = True
        mock_report.summary = "dry-run summary"
        mock_report.records = []

        mock_agent = MagicMock()
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/workspace")

        tool = DispatchSubagentsTool(mock_agent)
        result = tool.execute({"apply": False})

        assert result.ok is True
        mock_agent.dispatch_subagents.assert_called_once()
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert isinstance(call_kwargs["params"], DispatchParams)
        assert "apply" not in call_kwargs

    def test_dispatch_with_apply_and_execute_runners(self):
        """apply=true 且 execute_runners=true 时执行真实 runner。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = False
        mock_report.summary = "真实执行"
        mock_report.records = []

        mock_agent = MagicMock()
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/workspace")

        tool = DispatchSubagentsTool(mock_agent)
        result = tool.execute({"apply": True, "execute_runners": True})

        assert result.ok is True

    def test_top_level_execute_runners_defaults_acceptance_test_closure(self):
        """顶层真实跑 runner 时，默认进入受控验收测试和通过后自动闭环。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = False
        mock_report.summary = "真实执行"
        mock_report.records = []

        mock_agent = MagicMock()
        mock_agent._current_subagent_run_id = ""
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/workspace")
        mock_agent.subagents.list_runs.return_value = []

        result = DispatchSubagentsTool(mock_agent).execute({"apply": True, "execute_runners": True})

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].execute_acceptance_tests is True
        assert call_kwargs["params"].auto_apply_acceptance_followup is True

    def test_model_cannot_skip_acceptance_tests_for_live_runner_dispatch(self):
        """模型工具调用误写 false 时，真实 runner 仍必须进入父级验收。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = False
        mock_report.summary = "真实执行"
        mock_report.records = []

        mock_agent = MagicMock()
        mock_agent._current_subagent_run_id = ""
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/workspace")
        mock_agent.subagents.list_runs.return_value = []

        result = DispatchSubagentsTool(mock_agent).execute({
            "apply": True,
            "execute_runners": True,
            "execute_acceptance_tests": False,
        })

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].execute_acceptance_tests is True
        assert call_kwargs["params"].auto_apply_acceptance_followup is True

    def test_dispatch_exact_run_ids_are_passed_to_params(self):
        """run_ids 让父 runner 精确指定本轮孩子执行顺序。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = False
        mock_report.summary = {}
        mock_report.records = []

        mock_agent = MagicMock()
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/workspace")

        tool = DispatchSubagentsTool(mock_agent)
        result = tool.execute({
            "apply": True,
            "execute_runners": True,
            "run_ids": ["child-auth", "child-catalog"],
        })

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].include_run_ids == ["child-auth", "child-catalog"]

    def test_dispatch_scope_memory_ignores_non_runner_report_records(self):
        """当前轮作用域只记本轮显式/runner run_id，不把旧验收记录写进最终收口范围。"""
        from agent_py_agent.agent.agent_core.orchestration_dispatch_tool import (
            _run_ids_from_dispatch,
        )

        report = SimpleNamespace(
            records=[
                SimpleNamespace(step="runner", action="execute_runner", run_id="current-worker"),
                SimpleNamespace(step="acceptance", action="review", run_id="old-worker"),
            ]
        )

        ids = _run_ids_from_dispatch({"run_ids": ["current-worker"]}, report)

        assert ids == ["current-worker"]

    def test_runner_timeout_off_removes_default_runtime_thresholds(self):
        """runner 不限时时不再保留隐藏心跳/运行超时墙。"""
        from agent_py_agent.agent.agent_core.orchestration_dispatch_tool import (
            _dispatch_capability_config,
        )

        mock_agent = MagicMock()
        mock_agent.root = "/tmp/workspace"
        mock_agent.config.runner_timeout_seconds = "off"
        mock_agent.capability_config_path = ""

        cfg = _dispatch_capability_config(mock_agent)

        assert cfg.subagent_run_timeout == 0
        assert cfg.subagent_heartbeat_timeout == 0



class TestDispatchSubagentsToolTopLevelWorkflow:
    """测试顶层 dispatch 不会绕过 root coordinator 层级。"""

    def test_top_level_dispatch_does_not_auto_workflow_active_root_coordinator(self):
        """推进 root coordinator 时，模型误传 workflow_mode=auto 也不能绕过 coordinator 生成通用 worker。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = False
        mock_report.summary = {}
        mock_report.records = []

        mock_agent = MagicMock()
        mock_agent._current_subagent_run_id = ""
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/workspace")
        mock_agent.subagents.list_runs.return_value = [
            SimpleNamespace(
                id="root",
                role="coordinator",
                parent_id="",
                status="PLANNING",
                workflow_parent_run_id="",
            )
        ]

        tool = DispatchSubagentsTool(mock_agent)
        result = tool.execute({"apply": True, "execute_runners": True, "workflow_mode": "auto"})

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].workflow_mode == "off"

    def test_top_level_apply_dispatch_does_not_spawn_workflow_for_active_root_coordinator(self):
        """只 apply workflow 计划也不能给 active root/coordinator 套 producer/critic/repair。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = False
        mock_report.summary = {}
        mock_report.records = []

        mock_agent = MagicMock()
        mock_agent._current_subagent_run_id = ""
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/workspace")
        mock_agent.subagents.list_runs.return_value = [
            SimpleNamespace(
                id="root",
                role="coordinator",
                parent_id="",
                status="PLANNING",
                workflow_parent_run_id="",
            )
        ]

        result = DispatchSubagentsTool(mock_agent).execute({"apply": True, "workflow_mode": "auto"})

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].workflow_mode == "off"

    def test_top_level_dispatch_does_not_auto_workflow_concrete_worker_file_task(self):
        """顶层推进明确文件交付 worker 时，模型误传 auto 也不应扩成通用 workflow。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = False
        mock_report.summary = {}
        mock_report.records = []

        mock_agent = MagicMock()
        mock_agent._current_subagent_run_id = ""
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/workspace")
        mock_agent.subagents.list_runs.return_value = [
            SimpleNamespace(
                id="worker",
                role="worker",
                parent_id="",
                status="PLANNING",
                goal="把最终文件写到 /tmp/workspace/deliverables/index.html",
            )
        ]

        result = DispatchSubagentsTool(mock_agent).execute({"apply": True, "execute_runners": True, "workflow_mode": "auto"})

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].workflow_mode == "off"



class TestDispatchSubagentsToolRunnerContext:
    """测试 runner 内部 dispatch 的默认执行边界。"""

    def test_runner_context_dispatch_defaults_workflow_mode_off(self):
        """子代理 runner 内部 dispatch 默认不再套全局 workflow auto。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = False
        mock_report.summary = {}
        mock_report.records = []

        mock_agent = MagicMock()
        mock_agent._current_subagent_run_id = "subagent-root"
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/workspace")

        tool = DispatchSubagentsTool(mock_agent)
        result = tool.execute({"apply": True})

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].workflow_mode == "off"
        assert call_kwargs["params"].parent_run_id == "subagent-root"
        assert call_kwargs["params"].exclude_run_ids == ["subagent-root"]
        assert call_kwargs["params"].execute_acceptance_tests is True
        assert call_kwargs["params"].finalize_acceptance is True

    def test_runner_context_dispatch_defaults_to_execute_direct_children(self):
        """runner 内部省略 apply/execute 时，默认推进当前节点的直接孩子。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = False
        mock_report.summary = {"runner": 1}
        mock_report.records = []

        mock_agent = MagicMock()
        mock_agent._current_subagent_run_id = "subagent-root"
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/workspace")
        mock_agent.subagents.list_runs.return_value = []

        tool = DispatchSubagentsTool(mock_agent)
        result = tool.execute({})

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].apply is True
        assert call_kwargs["params"].execute_runners is True
        assert call_kwargs["params"].execute_acceptance_tests is True
        assert call_kwargs["params"].max_runners == 6
        assert call_kwargs["params"].parent_run_id == "subagent-root"
        assert call_kwargs["params"].exclude_run_ids == ["subagent-root"]
        assert call_kwargs["params"].finalize_acceptance is True

    def test_execute_limit_aliases_runner_count_when_max_runners_missing(self):
        """真实执行时，模型只写 limit 也应按 runner 数量推进，避免误退回单线程。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = False
        mock_report.summary = {"runner": 3}
        mock_report.records = []

        mock_agent = MagicMock()
        mock_agent._current_subagent_run_id = ""
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/workspace")
        mock_agent.subagents.list_runs.return_value = []

        result = DispatchSubagentsTool(mock_agent).execute({
            "apply": True,
            "execute_runners": True,
            "limit": 10,
        })

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].max_runners == 10
        assert call_kwargs["params"].limit == 10

    def test_nested_dispatch_excludes_active_ancestors(self):
        """孙级 dispatch 不应把仍在执行的父级/祖父级误判成可接管目标。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = False
        mock_report.summary = {"runner": 1}
        mock_report.records = []

        root = SimpleNamespace(id="root-run", parent_id="", status="RUNNING", runner_active_attempt_id="a-root")
        parent = SimpleNamespace(
            id="parent-run",
            parent_id="root-run",
            status="RUNNING",
            runner_active_attempt_id="a-parent",
        )
        current = SimpleNamespace(
            id="child-run",
            parent_id="parent-run",
            status="RUNNING",
            runner_active_attempt_id="a-child",
        )
        by_id = {item.id: item for item in [root, parent, current]}

        mock_agent = MagicMock()
        mock_agent._current_subagent_run_id = "child-run"
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/workspace")
        mock_agent.subagents.load.side_effect = lambda run_id: by_id[run_id]

        result = DispatchSubagentsTool(mock_agent).execute({})

        assert result.ok is True
        params = mock_agent.dispatch_subagents.call_args.kwargs["params"]
        assert params.exclude_run_ids == ["child-run", "parent-run", "root-run"]

    def test_dispatch_tool_keeps_heartbeat_stale_detection_when_runner_timeout_off(self):
        """runner_timeout_seconds=off 只关闭总时长接管；心跳挂死仍要能被父级发现。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = False
        mock_report.summary = {}
        mock_report.records = []

        mock_agent = MagicMock()
        mock_agent._current_subagent_run_id = "subagent-root"
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.config.runner_timeout_seconds = "off"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/workspace")
        mock_agent.subagents.list_runs.return_value = []

        result = DispatchSubagentsTool(mock_agent).execute({"apply": True, "execute_runners": True})

        assert result.ok is True
        capability_config = mock_agent.dispatch_subagents.call_args.args[1]
        assert capability_config.subagent_run_timeout == 0
        assert capability_config.subagent_heartbeat_timeout == 0
