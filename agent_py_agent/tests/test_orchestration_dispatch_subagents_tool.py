"""Split orchestration tool execution tests for code-size guard clarity."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


class TestDispatchSubagentsToolExecute:
    """测试 DispatchSubagentsTool.execute() 方法。"""

    def test_legacy_execution_params_are_rejected(self):
        """模型入口不再接受旧 apply/start_runners/execute_runners 执行开关。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.tools.specs.return_value = []

        tool = DispatchSubagentsTool(mock_agent)
        result = tool.execute({"start_runners": True, "execute_runners": True, "apply": False})

        assert result.ok is False
        assert "只接受 dry_run" in result.output

    def test_dispatch_dry_run_true_previews(self):
        """dry_run=true 时执行预览。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchParams
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
        result = tool.execute({"dry_run": True})

        assert result.ok is True
        mock_agent.dispatch_subagents.assert_called_once()
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert isinstance(call_kwargs["params"], DispatchParams)
        assert "apply" not in call_kwargs

    def test_dispatch_payload_keeps_current_subagent_paths(self):
        """dispatch_subagents 返回当前任务路径时不再做旧路径隐藏。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = True
        mock_report.summary = {}
        mock_report.records = []

        mock_agent = MagicMock()
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/project/tasks/current/work/agents")

        result = DispatchSubagentsTool(mock_agent).execute({"dry_run": True})

        assert result.ok is True
        assert "/tmp/project/tasks/current/work/agents" in result.output
        assert "[internal_legacy_subagent_path_hidden]" not in result.output

    def test_dispatch_dry_run_false_executes_runners(self):
        """dry_run=false 时执行真实 runner。"""
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
        result = tool.execute({"dry_run": False})

        assert result.ok is True

    def test_top_level_start_runners_dispatches_plain_runner(self):
        """顶层真实跑 runner 时，只推进 runner，并返回普通调度结果。"""
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

        result = DispatchSubagentsTool(mock_agent).execute({"dry_run": False, "run_ids": ["child-1"]})

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].start_runners is True

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
            "dry_run": False,
            "run_ids": ["child-auth", "child-catalog"],
        })

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].include_run_ids == ["child-auth", "child-catalog"]

    def test_dispatch_explicit_run_ids_default_max_runners_to_all_targets(self):
        """模型给多个 run_ids 但漏 max_runners 时，默认推进全部显式目标。"""
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

        result = DispatchSubagentsTool(mock_agent).execute({
            "dry_run": False,
            "run_ids": ["child-auth", "child-catalog", "child-cart"],
        })

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].max_runners == 3

    def test_dispatch_scope_memory_ignores_non_runner_report_records(self):
        """当前轮作用域只记本轮显式/runner run_id，不把旧验收记录写进最终收口范围。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.tool_helpers import (
            _run_ids_for_scope,
        )

        report = SimpleNamespace(
            records=[
                SimpleNamespace(step="runner", action="execute_runner", run_id="current-worker"),
                SimpleNamespace(step="acceptance", action="review", run_id="old-worker"),
            ]
        )

        ids = _run_ids_for_scope({"run_ids": ["current-worker"]}, report)

        assert ids == ["current-worker"]

    def test_runner_timeout_off_removes_default_runtime_thresholds(self):
        """runner 不限时时不再保留隐藏心跳/运行超时墙。"""
        from agent_py_agent.agent.agent_core.orchestration.dispatch.tool import (
            _dispatch_capability_config,
        )

        mock_agent = MagicMock()
        mock_agent.root = "/tmp/workspace"
        mock_agent.config.runner_timeout_seconds = "off"
        mock_agent.capability_config_path = ""

        cfg = _dispatch_capability_config(mock_agent)

        assert cfg.subagent_run_timeout == 0
        assert cfg.subagent_heartbeat_timeout == 0


class TestDispatchSubagentsToolRunnerInstruction:
    """测试 dispatch runner_instruction 的路径占位符归一。"""

    def test_runner_instruction_replaces_workspace_placeholder(self):
        """模型把 {workspace_root} 当自然语言模板传进来时，工具入口应替换成真实路径。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = False
        mock_report.summary = {}
        mock_report.records = []

        mock_agent = MagicMock()
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/project/.my-agent/subagents")
        mock_agent.subagents.workspace_root = Path("/tmp/project")

        result = DispatchSubagentsTool(mock_agent).execute({
            "dry_run": False,
            "run_ids": ["child-report"],
            "runner_instruction": "请把研究结果写入 {workspace_root}/data/subagents/report.md",
        })

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        root = Path("/tmp/project").resolve(strict=False)
        assert call_kwargs["params"].runner_instruction == f"请把研究结果写入 {root}/data/subagents/report.md"

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
        result = tool.execute({"dry_run": False, "workflow_mode": "auto"})

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].workflow_mode == "off"

    def test_top_level_real_dispatch_does_not_spawn_workflow_for_active_root_coordinator(self):
        """真实推进时也不能给 active root/coordinator 套 producer/critic/repair。"""
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

        result = DispatchSubagentsTool(mock_agent).execute({"dry_run": False, "workflow_mode": "auto"})

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].workflow_mode == "off"

    def test_top_level_dispatch_does_not_treat_prose_file_path_as_workflow_fact(self):
        """顶层推进时，普通 goal 里的文件路径不再作为关闭 workflow 的机器事实。"""
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

        result = DispatchSubagentsTool(mock_agent).execute({"dry_run": False, "workflow_mode": "auto"})

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].workflow_mode == "auto"



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
        result = tool.execute({"dry_run": False})

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].workflow_mode == "off"
        assert call_kwargs["params"].parent_run_id == "subagent-root"
        assert call_kwargs["params"].exclude_run_ids == ["subagent-root"]

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
        assert call_kwargs["params"].start_runners is True
        assert call_kwargs["params"].max_runners == 6
        assert call_kwargs["params"].parent_run_id == "subagent-root"
        assert call_kwargs["params"].exclude_run_ids == ["subagent-root"]

    def test_runner_context_dispatch_ignores_explicit_sibling_parent_scope(self):
        """runner 内 dispatch 不能用显式 parent_run_id 跳到平行子树。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = False
        mock_report.summary = {"runner": 1}
        mock_report.records = []

        mock_agent = MagicMock()
        mock_agent._current_subagent_run_id = "current-child"
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/workspace")

        result = DispatchSubagentsTool(mock_agent).execute({"parent_run_id": "sibling-parent"})

        assert result.ok is True
        params = mock_agent.dispatch_subagents.call_args.kwargs["params"]
        assert params.parent_run_id == "current-child"
        assert params.exclude_run_ids == ["current-child"]

    def test_runner_context_dispatch_payload_reports_scope_conflict(self):
        """runner 内显式指定外部 parent 时，payload 要暴露身份裁决而不是静默覆盖。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = False
        mock_report.summary = {"runner": 0}
        mock_report.records = []

        mock_agent = MagicMock()
        mock_agent._current_subagent_run_id = "current-child"
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/workspace")

        result = DispatchSubagentsTool(mock_agent).execute(
            {"parent_run_id": "sibling-parent", "run_ids": ["sibling-child"]}
        )

        assert result.ok is True
        payload = json.loads(result.output)
        assert "explicit_scope_overridden_by_current_runner" in payload["scope_warnings"]
        assert payload["scope_resolution"]["source"] == "current_runner_context"
        assert payload["scope_resolution"]["effective"]["parent_run_id"] == "current-child"
        assert payload["scope_resolution"]["ignored_explicit"]["parent_run_id"] == "sibling-parent"

    def test_max_runners_controls_runner_count(self):
        """真实执行时，runner 数量只由 max_runners 控制，limit 只管记录条数。"""
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
            "dry_run": False,
            "max_runners": 3,
            "limit": 10,
        })

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].max_runners == 3
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

        result = DispatchSubagentsTool(mock_agent).execute({"dry_run": False})

        assert result.ok is True
        capability_config = mock_agent.dispatch_subagents.call_args.args[1]
        assert capability_config.subagent_run_timeout == 0
        assert capability_config.subagent_heartbeat_timeout == 0
