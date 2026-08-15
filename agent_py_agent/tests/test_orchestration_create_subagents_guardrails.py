from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


class TestCreateSubagentsToolDelegationGuard:
    """测试专项交付约束不会变成 create-time 硬门。"""

    def test_create_subagents_does_not_block_button_constraint_reversal(self):
        """结构化按钮约束冲突由父代理验收，不在派工入口硬拦。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
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
            "goal": "创建 index1.html。",
            "role": "worker",
            "extra_write_roots": ["/tmp/project/artifacts"],
            "delegation_constraints": ["working_buttons"],
            "constraint_overrides": ["allow_dead_buttons"],
        })

        assert result.ok is True
        mock_agent.subagents.create_run.assert_called_once()

    def test_create_subagents_rejects_hash_anchor_escape_when_user_requires_working_buttons(self):
        """自然语言提到按钮不再触发代码层约束，必须由结构化字段承载。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent._current_user_prompt = "请做家具首页，不要有失灵按钮。"
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
            "goal": "创建 index1.html，所有按钮都要有 href 属性或 #锚点。",
            "role": "worker",
            "extra_write_roots": ["/tmp/project/artifacts"],
        })

        assert result.ok is True
        mock_agent.subagents.create_run.assert_called_once()

    def test_create_subagents_does_not_block_image_constraint_reversal(self):
        """图片类专项约束也不在 create_subagents 入口硬拦。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
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
            "goal": "创建 index1.html。",
            "role": "worker",
            "extra_write_roots": ["/tmp/project/artifacts"],
            "delegation_constraints": ["verified_images"],
            "constraint_overrides": ["allow_unverified_remote_images"],
        })

        assert result.ok is True
        mock_agent.subagents.create_run.assert_called_once()


class TestCreateSubagentsToolRawPromptRepair:
    """测试 root seed 能从原始用户 prompt 补回结构化硬合同。"""

    def test_explicit_coordinator_seed_without_name_gets_default_display_name(self):
        """模型没传 agent_name 时，第一层 root/coordinator 使用结构化默认展示名。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10

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
        assert params.agent_name == "agent-d1-coordinator"

    def test_explicit_coordinator_seed_inherits_raw_user_file_and_hierarchy_contract(self):
        """root/coordinator 机器合同必须来自工具参数 attributes，而不是原始用户 prompt。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent._current_user_prompt = "用户自然语言里提到 product.html 和 depth=3 都不能被代码当事实。"

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
                "完成示例站，交付 index.html、items.html、item-detail.html、style.css、app.js，"
                "文件名不要改，建立 4层链路。"
            ),
            "role": "coordinator",
            "extra_write_roots": ["/tmp/site/build"],
            "required_files": ["index.html", "items.html", "item-detail.html", "style.css", "app.js"],
            "forbidden_files": [
                "product.html", "old-product.html", "stale.html", "obsolete.html",
                "output.json", "RUNNER_RESULT.md", "execution_context.json",
            ],
            "hierarchy_contracts": ["depth=1 小傻妞-*", "depth=2 小小傻妞-*", "depth=3 小小小傻妞-*", "max_depth=3"],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.attributes["forbidden_files"][-2:] == ["RUNNER_RESULT.md", "execution_context.json"]
        assert "depth=3 小小小傻妞-*" in params.attributes["hierarchy_contracts"]

    def test_explicit_coordinator_seed_repairs_wrong_lineage_summary_from_raw_prompt(self):
        """模型写错层级前缀时，root seed 必须保留工具参数里的结构化命名合同。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent._current_user_prompt = "自然语言里提到小小小傻妞不应成为机器事实。"

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
            "hierarchy_contracts": [
                "depth=1 小傻妞-*",
                "depth=2 小小傻妞-*",
                "depth=3 小小小傻妞-*",
                "max_depth=3",
                "forbidden_depth>=4 小小小小傻妞-*",
            ],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert "depth=3 小小小傻妞-*" in params.attributes["hierarchy_contracts"]
        assert "forbidden_depth>=4 小小小小傻妞-*" in params.attributes["hierarchy_contracts"]
