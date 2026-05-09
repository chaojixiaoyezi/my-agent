"""orchestration_tools.py 单元测试。

测试编排工具注册、权限控制、执行边界等功能。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestToolWorkflowMode:
    """测试 _tool_workflow_mode() 函数。"""

    def test_explicit_off(self):
        """显式 off 模式。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import _tool_workflow_mode

        assert _tool_workflow_mode("off", "auto") == "off"

    def test_explicit_plan(self):
        """显式 plan 模式。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import _tool_workflow_mode

        assert _tool_workflow_mode("plan", "auto") == "plan"

    def test_explicit_auto(self):
        """显式 auto 模式。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import _tool_workflow_mode

        assert _tool_workflow_mode("auto", "manual") == "auto"

    def test_config_auto_falls_through(self):
        """配置 auto 但无显式值时。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import _tool_workflow_mode

        result = _tool_workflow_mode(None, "auto")
        assert result == "auto"

    def test_config_manual_maps_to_plan(self):
        """配置 manual 映射为 plan。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import _tool_workflow_mode

        result = _tool_workflow_mode(None, "manual")
        assert result == "plan"

    def test_default_off(self):
        """默认返回 off。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import _tool_workflow_mode

        assert _tool_workflow_mode("invalid", "invalid") == "off"

    def test_whitespace_handling(self):
        """验证空格处理。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import _tool_workflow_mode

        assert _tool_workflow_mode("  off  ", "auto") == "off"


class TestReadOnlySubagentTools:
    """测试 READ_ONLY_SUBAGENT_TOOLS 常量。"""

    def test_contains_read_tools(self):
        """验证只读工具列表。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import READ_ONLY_SUBAGENT_TOOLS

        assert "list_files" in READ_ONLY_SUBAGENT_TOOLS
        assert "read_file" in READ_ONLY_SUBAGENT_TOOLS
        assert "search_text" in READ_ONLY_SUBAGENT_TOOLS


class TestCodingSubagentTools:
    """测试 CODING_SUBAGENT_TOOLS 常量。"""

    def test_contains_write_tools(self):
        """验证包含写工具。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CODING_SUBAGENT_TOOLS

        assert "write_file" in CODING_SUBAGENT_TOOLS
        assert "replace_in_file" in CODING_SUBAGENT_TOOLS
        assert "append_file" in CODING_SUBAGENT_TOOLS

    def test_contains_read_tools(self):
        """验证也包含读工具。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CODING_SUBAGENT_TOOLS

        assert "read_file" in CODING_SUBAGENT_TOOLS
        assert "list_files" in CODING_SUBAGENT_TOOLS


class TestCreateSubagentsToolExecute:
    """测试 CreateSubagentsTool.execute() 方法。"""

    def test_disabled_by_config(self):
        """配置禁用时返回错误。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = False

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({"goal": "测试任务"})

        assert result.ok is False
        assert "禁用" in result.output

    def test_missing_goal_returns_error(self):
        """缺少必填 goal 参数时返回错误。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({})

        assert result.ok is False
        assert "goal" in result.output.lower()

    def test_empty_goal_returns_error(self):
        """空 goal 返回错误。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({"goal": "   "})

        assert result.ok is False

    def test_count_capped_by_max_subagents(self):
        """count 超过 max_subagents 时被限制。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 2

        mock_task = MagicMock()
        mock_task.id = "run_1"
        mock_task.goal = ""
        mock_task.status = "PENDING"
        mock_task.verification_status = "PENDING"
        mock_task.task_dir = "/tmp"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({"goal": "测试", "count": 10})

        # 应该最多只创建 max_subagents 个
        assert mock_agent.subagents.create_run.call_count <= 2

    def test_default_tool_preset_read_only(self):
        """默认 tool_preset 为 read_only。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10

        mock_task = MagicMock()
        mock_task.id = "run_default"
        mock_task.goal = ""
        mock_task.status = "PENDING"
        mock_task.verification_status = "PENDING"
        mock_task.task_dir = "/tmp"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({"goal": "测试"})

        # 验证调用时使用了默认的只读工具
        call_kwargs = mock_agent.subagents.create_run.call_args[1]
        assert "read_file" in call_kwargs["params"].allowed_tools


class TestSubagentBoardToolExecute:
    """测试 SubagentBoardTool.execute() 方法。"""

    def test_returns_board_summary(self):
        """验证返回看板摘要。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import SubagentBoardTool

        mock_agent = MagicMock()
        mock_agent.subagents.workspace = Path("/tmp/workspace")

        mock_board = MagicMock()
        mock_board.summary = {"total": 5, "running": 2}
        mock_item = MagicMock()
        mock_item.id = "item_1"
        mock_item.goal = "目标"
        mock_item.status = "RUNNING"
        mock_item.verification_status = "PENDING"
        mock_item.channel_status = "OK"
        mock_item.risk_flags = []
        mock_item.evidence_count = 0
        mock_item.open_request_count = 0
        mock_item.open_gap_count = 0
        mock_item.task_dir = "/tmp/item"
        mock_board.items = [mock_item]
        mock_agent.subagents.write_board.return_value = mock_board

        tool = SubagentBoardTool(mock_agent)
        result = tool.execute({"limit": 10})

        assert result.ok is True
        assert "summary" in result.output

    def test_status_filter_works(self):
        """状态过滤参数生效。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import SubagentBoardTool

        mock_agent = MagicMock()
        mock_agent.subagents.workspace = Path("/tmp/workspace")

        mock_item = MagicMock()
        mock_item.id = "run_1"
        mock_item.goal = "测试"
        mock_item.status = "RUNNING"
        mock_item.verification_status = "PENDING"
        mock_item.channel_status = "OK"
        mock_item.risk_flags = []
        mock_item.evidence_count = 0
        mock_item.open_request_count = 0
        mock_item.open_gap_count = 0
        mock_item.task_dir = "/tmp"

        mock_board = MagicMock()
        mock_board.summary = {"total": 1}
        mock_board.items = [mock_item]
        mock_agent.subagents.write_board.return_value = mock_board

        tool = SubagentBoardTool(mock_agent)
        result = tool.execute({"status": "RUNNING", "limit": 10})

        assert result.ok is True


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
        assert call_kwargs["params"].finalize_acceptance is False

    def test_runner_context_invalid_workflow_mode_stays_off(self):
        """模型误传 parallel 时，runner dispatch 也不能回退成全局 auto。"""
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
        result = tool.execute({"apply": True, "workflow_mode": "parallel"})

        assert result.ok is True
        call_kwargs = mock_agent.dispatch_subagents.call_args.kwargs
        assert call_kwargs["params"].workflow_mode == "off"

    def test_runner_context_dispatch_reports_direct_child_progress(self):
        """runner 内 dispatch 结果要提示剩余 PLANNING child，避免误判失败。"""
        from types import SimpleNamespace

        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_report = MagicMock()
        mock_report.dry_run = False
        mock_report.summary = {"runner": 1}
        mock_report.records = []

        mock_agent = MagicMock()
        mock_agent._current_subagent_run_id = "parent-run"
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.tools.specs.return_value = []
        mock_agent.dispatch_subagents.return_value = mock_report
        mock_agent.subagents.workspace = Path("/tmp/workspace")
        mock_agent.subagents.list_runs.return_value = [
            SimpleNamespace(id="parent-run", parent_id="", status="RUNNING"),
            SimpleNamespace(id="child-a", parent_id="parent-run", status="AWAITING_ACCEPTANCE"),
            SimpleNamespace(id="child-b", parent_id="parent-run", status="PLANNING"),
        ]

        tool = DispatchSubagentsTool(mock_agent)
        result = tool.execute({"apply": True, "execute_runners": True})

        payload = json.loads(result.output)
        assert payload["direct_children"]["by_status"]["PLANNING"] == 1
        assert payload["direct_children"]["planning_run_ids"] == ["child-b"]
        assert "继续调用 dispatch_subagents" in payload["direct_children"]["continue_hint"]


class TestOrchestrationToolsSpec:
    """测试工具规格定义。"""

    def test_create_subagents_spec_defined(self):
        """CreateSubagentsTool 工具规格已定义。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10

        tool = CreateSubagentsTool(mock_agent)
        spec = tool.spec

        assert spec.name == "create_subagents"
        assert spec.category == "orchestration"

    def test_subagent_board_spec_defined(self):
        """SubagentBoardTool 工具规格已定义。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import SubagentBoardTool

        mock_agent = MagicMock()
        mock_agent.subagents.workspace = Path("/tmp")

        tool = SubagentBoardTool(mock_agent)
        spec = tool.spec

        assert spec.name == "subagent_board"
        assert spec.category == "orchestration"

    def test_dispatch_subagents_spec_defined(self):
        """DispatchSubagentsTool 工具规格已定义。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.tools.specs.return_value = []

        tool = DispatchSubagentsTool(mock_agent)
        spec = tool.spec

        assert spec.name == "dispatch_subagents"
        assert spec.category == "orchestration"

    def test_schedule_child_subagents_spec_defined(self):
        """ScheduleChildSubagentsTool 工具规格已定义。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool

        mock_agent = MagicMock()
        tool = ScheduleChildSubagentsTool(mock_agent)
        spec = tool.spec

        assert spec.name == "schedule_child_subagents"
        assert spec.category == "orchestration"
        assert "当前 subagent runner" in spec.description


class TestScheduleChildSubagentsTool:
    """测试当前 runner 创建下一层子节点的安全边界。"""

    def test_rejects_without_current_runner_context(self):
        """没有当前 runner id 时，不能绕过主节点直接挂 child。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool

        mock_agent = MagicMock()
        mock_agent._current_subagent_run_id = ""
        tool = ScheduleChildSubagentsTool(mock_agent)

        result = tool.execute({"children": [{"goal": "leaf"}], "apply": True})

        assert not result.ok
        assert "顶层派工请使用 create_subagents" in result.output

    def test_runner_context_max_depth_can_mean_one_more_layer(self, tmp_path):
        """模型在 depth=1 传 max_depth=1 时，按“再开一层”兼容处理。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
        root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
        child = agent.subagents.create_run(
            goal="child", thought="child", plan=["child"], parent_id=root.id, root_id=root.id, depth=1,
        )
        agent._current_subagent_run_id = child.id
        tool = ScheduleChildSubagentsTool(agent)

        result = tool.execute(
            {
                "apply": True,
                "max_depth": 1,
                "children": [{"goal": "leaf", "role": "leaf", "agent_name": "leaf"}],
            }
        )
        payload = json.loads(result.output)
        leaf = agent.subagents.load(payload["created_run_ids"][0])

        assert result.ok
        assert leaf.parent_id == child.id
        assert leaf.depth == 2
