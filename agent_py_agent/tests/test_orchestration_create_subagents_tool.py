"""Split orchestration tool execution tests for code-size guard clarity."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock


# LLM: _mock_create_items_agent keeps items-mode tests focused on create params, not fixture setup.
# 函数用途: 构造支持 create_subagents items[] 测试的最小 agent mock 和固定数量任务。
def _mock_create_items_agent(task_count: int = 3):
    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.subagents.workspace_root = Path("/tmp/project")
    mock_agent.subagents.workspace_roots = [Path("/tmp/project")]
    mock_agent.subagents.workspace = Path("/tmp/project/.my-agent/subagents")
    tasks = [_mock_created_task(index) for index in range(task_count)]
    mock_agent._created_tasks = tasks
    mock_agent.subagents.create_run.side_effect = tasks
    return mock_agent


# LLM: _mock_created_task gives create_subagents payload rendering stable task fields.
# 函数用途: 为 create_run side_effect 提供带 id/status/task_dir 的任务替身。
def _mock_created_task(index: int):
    task = MagicMock()
    task.id = f"run_{index}"
    task.goal = ""
    task.status = "PLANNING"
    task.verification_status = "UNVERIFIED"
    task.task_dir = f"/tmp/run_{index}"
    return task


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

    def test_create_subagents_auto_starts_created_runs_without_waiting_for_completion(self, monkeypatch):
        """create_subagents 默认创建并后台启动，父代理不等子代理全部结束。"""
        from agent_py_agent.agent.agent_core import orchestration_background_dispatch
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent(task_count=2)
        mock_agent.tools.specs.return_value = []
        launched: dict[str, object] = {}

        def fake_background_start(agent, run_ids):
            launched["agent"] = agent
            launched["run_ids"] = list(run_ids)
            return {
                "status": "started",
                "dispatch_mode": "background",
                "run_ids": list(run_ids),
                "agent_tree": {"schema_version": "agent_tree_status.v1"},
            }

        monkeypatch.setattr(orchestration_background_dispatch, "_start_background_dispatch", fake_background_start)

        result = CreateSubagentsTool(mock_agent).execute({"goal": "分别整理两份资料", "count": 2})
        payload = json.loads(result.output)

        assert result.ok is True
        mock_agent.dispatch_subagents.assert_not_called()
        assert launched["run_ids"] == ["run_0", "run_1"]
        assert payload["auto_start"]["status"] == "started"
        assert payload["auto_start"]["dispatch_mode"] == "background"
        assert payload["auto_start"]["agent_tree"]["schema_version"] == "agent_tree_status.v1"
        assert payload["next_action"]["tool"] == "subagent_board"

    def test_defer_start_keeps_created_runs_unstarted(self):
        """只有显式 defer_start=true 时，create_subagents 才只建记录不启动。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent(task_count=2)
        mock_agent.tools.specs.return_value = []

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "先登记两个后续任务",
            "count": 2,
            "defer_start": True,
        })
        payload = json.loads(result.output)

        assert result.ok is True
        mock_agent.dispatch_subagents.assert_not_called()
        assert payload["auto_start"]["status"] == "deferred"
        assert payload["next_action"]["tool"] == "dispatch_subagents"

class TestCreateSubagentsToolTemplatePolicy:
    """测试 create_subagents 的角色模板和工具推断策略。"""

    def test_default_tools_use_role_template_policy(self):
        """默认给子代理基础内置工具，避免少填 allowed_tools 变成残废代理。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.access_mode = "full-access"
        mock_agent.config.subagent_memory_retention_policy = "delete_after_days"
        mock_agent.config.subagent_memory_delete_after_days = 7
        mock_agent.config.subagent_destroy_summary_required = False

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
        assert "read_file" in call_kwargs["params"].allowed_tools
        assert "write_file" in call_kwargs["params"].allowed_tools
        assert "apply_patch" in call_kwargs["params"].allowed_tools
        assert "run_command" in call_kwargs["params"].allowed_tools
        assert call_kwargs["params"].parent_access_mode == "full-access"
        assert call_kwargs["params"].memory_retention_policy == "delete_after_days"
        assert call_kwargs["params"].memory_delete_after_days == 7
        assert call_kwargs["params"].destroy_summary_required is False
        assert call_kwargs["params"].role == "worker"
        assert result.ok is True

    def test_explicit_tool_preset_read_only_keeps_baseline_write_tools(self):
        """显式 read_only 只表达职责偏好，不能让子代理失去基础读写能力。"""
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
        assert "read_file" in call_kwargs["params"].allowed_tools
        assert "write_file" in call_kwargs["params"].allowed_tools
        assert "apply_patch" in call_kwargs["params"].allowed_tools

    def test_tool_preset_none_does_not_create_toolless_subagent(self):
        """模型传 tool_preset=none 时回退自动策略，不创建空工具子代理。"""
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
        tool.execute({"goal": "测试", "tool_preset": "none"})

        call_kwargs = mock_agent.subagents.create_run.call_args[1]
        assert "read_file" in call_kwargs["params"].allowed_tools
        assert "run_command" in call_kwargs["params"].allowed_tools

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
        assert "read_file" in params.allowed_tools
        assert "run_command" in params.allowed_tools
        assert params.role == "coordinator"

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
        assert "apply_patch" in params.allowed_tools
        assert "apply_patch" in params.allowed_tools
        assert "read_artifact" in params.allowed_tools

    def test_vague_deliverable_worker_requires_extra_write_root_without_workspace(self):
        """没有真实 workspace_root 时仍拒绝模糊目标目录，避免 worker 写进自己的任务目录。"""
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
            "goal": "写一个极简单文件。",
            "role": "writer",
            "output_files": ["index.html"],
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
