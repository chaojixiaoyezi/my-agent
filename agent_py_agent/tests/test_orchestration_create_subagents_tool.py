"""Split orchestration tool execution tests for code-size guard clarity."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


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



class TestCreateSubagentsToolTemplatePolicy:
    """测试 create_subagents 的角色模板和工具推断策略。"""

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



class TestCreateSubagentsToolCoordinatorSeed:
    """测试 coordinator/root seed 的边界继承和 workflow 保护。"""

    def test_slash_separated_deliverable_labels_do_not_trip_external_write_guard(self):
        """交付物标签里的斜杠不是绝对路径，不能误拦截 coordinator seed。"""
        from pathlib import Path

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

    def test_explicit_coordinator_seed_without_name_gets_lineage_prefix(self):
        """模型没传 agent_name 时，第一层 root/coordinator 也必须有小傻妞前缀。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "off"

        mock_task = MagicMock()
        mock_task.id = "root_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/root_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "创建 root/coordinator 并让它继续派工。",
            "role": "coordinator",
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.agent_name == "小傻妞-coordinator"

    def test_explicit_coordinator_seed_inherits_raw_user_file_and_hierarchy_contract(self):
        """主代理摘要 root goal 时，工具层要补回原始用户 prompt 的硬合同。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent._current_user_prompt = (
            "在 /tmp/shop/build 交付购物站。必须包含 index.html、products.html、"
            "product-detail.html、style.css、app.js。"
            "禁止文件名：product.html/old-product.html/legacy.html/obsolete.html。"
            "禁止在 build 写 output.json/RUNNER_RESULT.md/execution_context.json。"
            "必须正好覆盖一条 4层总链路：root -> 子 -> 孙 -> 孙孙。"
            "depth=1 用小傻妞-*，depth=2 用小小傻妞-*，depth=3 用小小小傻妞-*，max_depth=3。"
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
                "完成购物站，交付 index.html、products.html、product-detail.html、style.css、app.js，"
                "文件名不要改，建立 4层链路。"
            ),
            "role": "coordinator",
            "extra_write_roots": ["/tmp/shop/build"],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert "用户原始禁止文件/反例名" in params.goal
        assert "product.html" in params.goal
        assert "RUNNER_RESULT.md" in params.goal
        assert "用户原始层级/命名约束" in params.goal
        assert "depth=3" in params.goal

    def test_explicit_coordinator_seed_repairs_wrong_lineage_summary_from_raw_prompt(self):
        """模型写错层级前缀时，root seed 必须保留用户原始精确命名合同。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent._current_user_prompt = (
            "命名必须按层级规则：depth=1 用“小傻妞-*”，"
            "depth=2 用“小小傻妞-*”，depth=3 用“小小小傻妞-*”。"
            "本轮 max_depth=3，禁止创建 depth>=4，禁止创建“小小小小傻妞-*”节点。"
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
                "层级要求：depth=1 用“小傻妞-*”，depth=2 用“小小傻妞-*”，"
                "depth=3 用“小小的傻妞-*”；本轮 max_depth=3，禁止创建“小小小傻妞-*”节点。"
            ),
            "role": "coordinator",
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert "用户原始层级/命名约束" in params.goal
        assert "小小小傻妞-*" in params.goal
        assert "小小小小傻妞-*" in params.goal
