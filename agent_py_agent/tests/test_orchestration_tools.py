"""orchestration_tools.py 单元测试。

测试编排工具注册、权限控制、执行边界等功能。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# LLM: test_create_subagents_tool_spec_uses_template_index_not_full_prompt protects startup token budget.
# 函数用途: create_subagents 工具说明只暴露角色模板索引，不把完整角色系统提示词放进主代理常驻工具说明。
def test_create_subagents_tool_spec_uses_template_index_not_full_prompt():
    from agent_py_agent.agent.agent_core.orchestration_tool_specs import build_create_subagents_spec

    spec = build_create_subagents_spec()
    role_detail = spec.parameter_details["role"]

    assert "模板位置" in role_detail
    assert "worker" in role_detail
    assert "你是执行子代理" not in role_detail


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

    def test_default_tools_use_role_template_policy(self):
        """默认不再要求用户选工具，而是交给角色模板/任务上下文推断。"""
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

        call_kwargs = mock_agent.subagents.create_run.call_args[1]
        assert call_kwargs["params"].allowed_tools is None
        assert call_kwargs["params"].role == "worker"
        assert result.ok is True

    def test_explicit_tool_preset_read_only_still_works(self):
        """显式 tool_preset=read_only 仍然会限制为只读工具。"""
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
        tool.execute({"goal": "测试", "tool_preset": "read_only"})

        call_kwargs = mock_agent.subagents.create_run.call_args[1]
        assert call_kwargs["params"].allowed_tools == ["list_files", "read_file", "search_text"]

    def test_unknown_tool_preset_does_not_override_role_template(self):
        """模型误把 role 写到 tool_preset 时，应回退给 role template 自动决定工具。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10

        mock_task = MagicMock()
        mock_task.id = "coordinator_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/coordinator_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "Seed a root coordinator and let the role template choose tools.",
            "role": "coordinator",
            "tool_preset": "coordinator",
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.allowed_tools is None

    def test_slash_separated_deliverable_labels_do_not_trip_external_write_guard(self):
        """交付物标签里的斜杠不是绝对路径，不能误拦截 coordinator seed。"""
        from pathlib import Path

        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.subagents.workspace_root = Path("/Users/xiaoyezi/my-claude-code")
        mock_agent.subagents.workspace_roots = [Path("/Users/xiaoyezi/my-claude-code")]

        mock_task = MagicMock()
        mock_task.id = "coordinator_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/coordinator_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": (
                "Create deliverables named requirements/research-brief/implementation/"
                "README/bug-report/test-report/acceptance-verdict inside the approved root."
            ),
            "role": "coordinator",
            "extra_write_roots": ["/Users/xiaoyezi/my-claude-code/deliverables/role-template"],
        })

        assert result.ok is True
        mock_agent.subagents.create_run.assert_called_once()

    def test_explicit_coordinator_seed_ignores_model_workflow_auto(self):
        """coordinator/root seed 只能创建根节点，不能被模型的 workflow_mode=auto 自动污染孩子。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "auto"

        mock_task = MagicMock()
        mock_task.id = "coordinator_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/coordinator_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "Seed one root coordinator. Children must be created by that coordinator.",
            "role": "coordinator",
            "workflow_mode": "auto",
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.workflow_mode == "off"

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

    def test_dispatch_tool_respects_runner_timeout_off_for_auto_due_check(self):
        """runner_timeout_seconds=off 时，模型调度工具不应自动生成 run/heartbeat 接管动作。"""
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

    def test_runner_context_schedule_defaults_to_apply_direct_child(self, tmp_path):
        """runner 内部省略 apply 时，应真实创建当前节点的直接 child。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
        root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
        agent._current_subagent_run_id = root.id
        tool = ScheduleChildSubagentsTool(agent)

        result = tool.execute({"children": [{"goal": "leaf", "role": "leaf_worker", "agent_name": "leaf"}]})
        payload = json.loads(result.output)

        assert result.ok
        assert payload["dry_run"] is False
        assert len(payload["created_run_ids"]) == 1
        assert agent.subagents.load(root.id).child_ids == payload["created_run_ids"]

    def test_runner_context_schedule_accepts_orchestration_wrapper(self, tmp_path):
        """真实模型常把 children 包进 orchestration；工具入口要展开后再调度。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
        root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
        agent._current_subagent_run_id = root.id
        tool = ScheduleChildSubagentsTool(agent)

        result = tool.execute({
            "tool": "schedule_child_subagents",
            "orchestration": {
                "apply": True,
                "children": [{"goal": "grandchild coordinator", "role": "coordinator", "agent_name": "页面组"}],
            },
        })
        payload = json.loads(result.output)
        child = agent.subagents.load(payload["created_run_ids"][0])

        assert result.ok
        assert payload["dry_run"] is False
        assert child.parent_id == root.id
        assert child.agent_name == "小傻妞-页面组"
