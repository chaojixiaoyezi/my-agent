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

    def test_frontend_preset_completes_partial_explicit_tool_list(self):
        """frontend-dev 这类写页面预设会补齐 append/replace，避免模型少填工具后卡住。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "off"
        mock_task = MagicMock()
        mock_task.id = "frontend_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/frontend_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "修复 /tmp/project/artifacts/index2.html 页面。",
            "tool_preset": "frontend-dev",
            "allowed_tools": ["write_file", "read_file", "run_command"],
            "extra_write_roots": ["/tmp/project/artifacts"],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert "append_file" in params.allowed_tools
        assert "replace_in_file" in params.allowed_tools
        assert "read_artifact" in params.allowed_tools

    def test_vague_deliverable_worker_requires_extra_write_root(self):
        """写真实产物但只说目标目录时必须拒绝，避免 worker 写进自己的任务目录。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "off"
        mock_task = MagicMock()
        mock_task.id = "writer_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/writer_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "在目标目录写一个极简单文件 index.html，并报告路径。",
            "role": "writer",
        })

        assert result.ok is False
        assert "extra_write_roots" in result.output
        assert "目标目录" in result.output
        mock_agent.subagents.create_run.assert_not_called()

    def test_no_comment_constraint_can_be_preserved_in_child_goal(self):
        """用户要求不要注释时，子任务保留“不要写注释”不应被误判为要求写注释。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent._current_user_prompt = "只输出完整 HTML，不要注释。"
        mock_agent.subagents.workspace_root = Path("/tmp/project")
        mock_agent.subagents.workspace_roots = [Path("/tmp/project")]
        mock_task = MagicMock()
        mock_task.id = "frontend_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/frontend_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "请在 /tmp/project/deliverables/index1.html 输出完整 HTML，不要写注释。",
            "role": "worker",
            "extra_write_roots": ["/tmp/project/deliverables"],
        })

        assert result.ok is True
        mock_agent.subagents.create_run.assert_called_once()



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

    def test_coordinator_seed_intent_repairs_model_worker_role(self):
        """模型把 root coordinator 误写成 worker 时，工具层要按目标意图纠偏。"""
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
        assert params.role == "coordinator"
        assert params.workflow_mode == "off"

    def test_user_style_delegate_to_next_layer_repairs_worker_to_coordinator(self):
        """用户说小傻妞可再找小小傻妞时，第一层应按带队角色创建。"""
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
        assert params.role == "coordinator"
        assert params.workflow_mode == "off"

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


class TestCreateSubagentsToolWorkerWorkflow:
    """测试具体 worker 任务不会被泛化 workflow 污染。"""

    def test_concrete_single_file_worker_disables_generic_workflow_auto(self):
        """具体单文件 worker 不应被 workflow=auto 套成 producer/critic/repair。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent._current_user_prompt = "请派小傻妞做两个不同风格的家具品牌首页。"
        mock_agent.subagents.workspace_root = Path("/tmp/project")
        mock_agent.subagents.workspace_roots = [Path("/tmp/project")]

        mock_task = MagicMock()
        mock_task.id = "worker_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/worker_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "在 /tmp/project 目录下创建一个名为 index1.html 的单文件 HTML 页面。",
            "role": "小傻妞",
            "workflow_mode": "auto",
            "extra_write_roots": ["/tmp/project"],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "worker"
        assert params.workflow_mode == "off"

    def test_single_file_child_worker_is_not_repaired_to_coordinator(self):
        """用户允许多层派工时，单文件 child_worker 仍应保持交付角色。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent._current_user_prompt = "请派小傻妞来做，如果任务多，可以让小傻妞再找小小傻妞帮忙。"
        mock_agent.subagents.workspace_root = Path("/tmp/project")
        mock_agent.subagents.workspace_roots = [Path("/tmp/project")]

        mock_task = MagicMock()
        mock_task.id = "worker_001"
        mock_task.goal = ""
        mock_task.status = "PLANNING"
        mock_task.verification_status = "UNVERIFIED"
        mock_task.task_dir = "/tmp/worker_001"
        mock_agent.subagents.create_run.return_value = mock_task

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "在 /tmp/project/artifacts/index1.html 创建一个单文件 HTML 页面。",
            "role": "child_worker",
            "workflow_mode": "auto",
            "extra_write_roots": ["/tmp/project/artifacts"],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "child_worker"
        assert params.workflow_mode == "off"

    def test_repeated_concrete_file_goal_requires_explicit_split(self):
        """count 不能复制同一个带文件名的 worker goal，避免多个子代理抢同一批产物。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent._current_user_prompt = "请派小傻妞做两个家具首页。"
        mock_agent.subagents.workspace_root = Path("/tmp/project")
        mock_agent.subagents.workspace_roots = [Path("/tmp/project")]

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": (
                "在 /tmp/project/artifacts 创建 index1.html 和 index2.html 两个单文件 HTML 页面。"
            ),
            "count": 2,
            "role": "leaf_worker",
            "workflow_mode": "auto",
            "extra_write_roots": ["/tmp/project/artifacts"],
        })

        assert result.ok is False
        assert "ambiguous_repeated_product_goal" in result.output
        assert "count=1 的 coordinator" in result.output
        mock_agent.subagents.create_run.assert_not_called()


class TestCreateSubagentsToolDelegationGuard:
    """测试派工目标不能反转用户的真实交付约束。"""

    def test_create_subagents_rejects_button_constraint_reversal(self):
        """派工目标不能把用户的“不失灵按钮”改写成 href=#。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent._current_user_prompt = "请做家具首页，不要有失灵按钮。"
        mock_agent.subagents.workspace_root = Path("/tmp/project")
        mock_agent.subagents.workspace_roots = [Path("/tmp/project")]

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "创建 index1.html，所有按钮可点击（可指向 #）。",
            "role": "worker",
            "extra_write_roots": ["/tmp/project/artifacts"],
        })

        assert result.ok is False
        assert "delegation_constraint_conflict" in result.output
        mock_agent.subagents.create_run.assert_not_called()

    def test_create_subagents_rejects_hash_anchor_escape_when_user_requires_working_buttons(self):
        """派工目标不能用“#锚点”绕过用户的不失灵按钮约束。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent._current_user_prompt = "请做家具首页，不要有失灵按钮。"
        mock_agent.subagents.workspace_root = Path("/tmp/project")
        mock_agent.subagents.workspace_roots = [Path("/tmp/project")]

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "创建 index1.html，所有按钮都要有 href 属性或 #锚点。",
            "role": "worker",
            "extra_write_roots": ["/tmp/project/artifacts"],
        })

        assert result.ok is False
        assert "delegation_constraint_conflict" in result.output
        mock_agent.subagents.create_run.assert_not_called()

    def test_create_subagents_rejects_unverified_remote_images_when_user_requires_no_broken_images(self):
        """用户要求不失效图片时，派工目标不能擅自要求远程图片 URL。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent._current_user_prompt = "请做家具首页，不要出现失效图片链接。"
        mock_agent.subagents.workspace_root = Path("/tmp/project")
        mock_agent.subagents.workspace_roots = [Path("/tmp/project")]

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "创建 index1.html，使用 Unsplash 的真实图片 URL。",
            "role": "worker",
            "extra_write_roots": ["/tmp/project/artifacts"],
        })

        assert result.ok is False
        assert "delegation_constraint_conflict" in result.output
        mock_agent.subagents.create_run.assert_not_called()


class TestCreateSubagentsToolRawPromptRepair:
    """测试 root seed 能从原始用户 prompt 补回硬合同。"""

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
