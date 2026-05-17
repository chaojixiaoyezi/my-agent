"""LLM: coordinator seed tests stay separate so orchestration tool tests stay reviewable.

模块用途: 验证 root/coordinator 派工意图不会被模型的 role/workflow 参数写偏。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


class TestCreateSubagentsToolCoordinatorSeed:
    """测试 coordinator/root seed 的边界继承和 workflow 保护。"""

    def test_slash_separated_deliverable_labels_do_not_trip_external_write_guard(self):
        """交付物标签里的斜杠不是绝对路径，不能误拦截 coordinator seed。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.subagents.workspace_root = Path("/Users/example/my-claude-code")
        mock_agent.subagents.workspace_roots = [Path("/Users/example/my-claude-code")]

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
            "extra_write_roots": ["/Users/example/my-claude-code/deliverables/role-template"],
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

    def test_natural_coordinator_seed_intent_does_not_repair_model_worker_role(self):
        """模型把 root coordinator 误写成 worker 时，工具层不从自然语言目标纠偏。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent._current_user_prompt = (
            "请建立主代理 -> 小傻妞-root-coordinator -> 小小傻妞-child-coordinator "
            "-> 小小小傻妞-leaf-worker 的链路。第一层必须创建下一层，不能自己写最终产物。"
        )

        mock_task = MagicMock()
        mock_task.id = "root_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/root_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": (
                "创建 小傻妞-root-coordinator，并让它使用 schedule_child_subagents "
                "继续创建 小小傻妞-child-coordinator；本节点不要写最终产物。"
            ),
            "role": "worker",
            "workflow_mode": "auto",
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "worker"
        assert params.workflow_mode == "auto"

    def test_child_dispatch_tool_grant_repairs_worker_to_coordinator(self):
        """显式给 schedule/dispatch 工具时，工具层按结构化能力纠偏为 coordinator。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "auto"

        mock_task = MagicMock()
        mock_task.id = "root_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/root_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "创建第一层 root coordinator。",
            "role": "worker",
            "allowed_tools": ["read_file", "write_file", "schedule_child_subagents", "dispatch_subagents"],
            "workflow_mode": "auto",
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "coordinator"
        assert params.workflow_mode == "off"

    def test_user_style_delegate_to_next_layer_does_not_repair_without_structured_signal(self):
        """用户说小傻妞可再找小小傻妞时，代码层不靠自然语言把 worker 改成 coordinator。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent._current_user_prompt = (
            "请你派小傻妞来完成这个任务，不要你自己亲自写页面。"
            "如果任务比较多，可以让小傻妞再找小小傻妞帮忙。"
        )

        mock_task = MagicMock()
        mock_task.id = "root_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/root_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "做 3 个高端现代家具品牌网站首页 HTML 文件。",
            "role": "worker",
            "workflow_mode": "auto",
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "worker"
        assert params.workflow_mode == "auto"

    def test_user_style_if_needed_delegate_does_not_repair_child_without_structured_signal(self):
        """用户说小傻妞必要时找小小傻妞时，role=child 不靠自然语言变 coordinator。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent._current_user_prompt = "请组织小傻妞协作完成；每个小傻妞如果需要，可以再找小小傻妞帮忙。"

        mock_task = MagicMock()
        mock_task.id = "market_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/market_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "研究东南亚市场环境，输出国家优先级和证据摘要。",
            "role": "child",
            "agent_name": "小傻妞-市场环境",
            "allowed_tools": ["read_file", "write_file"],
            "workflow_mode": "auto",
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "child"


class TestCreateSubagentsToolCoordinatorPlan:
    """测试 coordinator plan 和 lineage 名称兼容。"""

    def test_items_count_and_nested_tasks_are_tolerated_as_coordinator_plan(self):
        """模型自然传 items+count+tasks 时，不应报错逼它丢掉多层计划。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent._current_user_prompt = "请派小傻妞研究市场；每个小傻妞可以找小小傻妞帮忙。"

        mock_task = MagicMock()
        mock_task.id = "market_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/market_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "items": [{
                "goal": "研究印尼、泰国、越南市场环境，汇总评分。",
                "role": "worker",
                "agent_name": "小傻妞-市场",
                "tasks": [
                    {"goal": "分析印尼市场", "role": "grandchild", "agent_name": "印尼研究员"},
                    {"goal": "分析泰国和越南市场", "role": "grandchild", "agent_name": "泰越研究员"},
                ],
            }],
            "count": 3,
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "coordinator"
        assert any("分析印尼市场" in item for item in params.plan)
        assert any("泰越研究员" in item for item in params.plan)

    def test_explicit_child_dispatch_tools_repair_worker_to_coordinator(self):
        """显式给子调度工具时，role=worker 按带队节点创建。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent._current_user_prompt = "请让小傻妞分别研究市场、竞争和策略。"
        mock_agent.subagents.workspace_root = Path("/tmp/project")
        mock_agent.subagents.workspace_roots = [Path("/tmp/project")]

        mock_task = MagicMock()
        mock_task.id = "market_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/market_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "items": [{
                "goal": (
                    "研究市场环境。每个子代理需要创建并调度至少2个孙代理负责细分研究，"
                    "最终产出写到 /tmp/project/deliverables/market_env_report.md"
                ),
                "role": "worker",
                "agent_name": "小傻妞-市场环境",
                "allowed_tools": ["read_file", "write_file", "schedule_child_subagents", "dispatch_subagents"],
            }],
            "extra_write_roots": ["/tmp/project/deliverables"],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "coordinator"

    def test_lineage_agent_name_in_role_field_becomes_name_not_role(self):
        """模型把“小傻妞-root-coordinator”写进 role 时，应拆成标准 role 和显示名。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "auto"

        mock_task = MagicMock()
        mock_task.id = "root_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/root_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "创建第一层 root coordinator，并使用 schedule_child_subagents 创建下一层。",
            "role": "小傻妞-root-coordinator",
            "workflow_mode": "auto",
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "coordinator"
        assert params.agent_name == "小傻妞-root-coordinator"
        assert params.workflow_mode == "off"
