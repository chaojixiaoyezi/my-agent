"""Split orchestration tool execution tests for code-size guard clarity."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
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

    def test_count_mode_uses_agent_config_default_when_max_subagents_missing(self):
        """轻量配置对象缺少 max_subagents 时，count 模式也使用 AgentConfig 默认值。"""
        from types import SimpleNamespace

        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
        from agent_py_agent.agent.settings import AgentConfig

        mock_agent = MagicMock()
        mock_agent.config = SimpleNamespace(enable_subagents=True, subagent_workflow_mode="off")
        mock_task = MagicMock()
        mock_task.id = "run_1"
        mock_task.goal = ""
        mock_task.status = "PENDING"
        mock_task.verification_status = "PENDING"
        mock_task.task_dir = "/tmp"
        mock_agent.subagents.create_run.return_value = mock_task

        result = CreateSubagentsTool(mock_agent).execute({"goal": "测试", "count": 60})

        assert result.ok is True
        assert mock_agent.subagents.create_run.call_count == AgentConfig().max_subagents

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

    def test_auto_start_process_command_targets_created_run_ids(self):
        """真实后台进程必须显式只推进本轮创建的 run_id，不能靠全局候选猜。"""
        from agent_py_agent.agent.agent_core.orchestration_background_dispatch import (
            _background_dispatch_command,
            _BackgroundDispatchRequest,
        )

        mock_agent = _mock_create_items_agent(task_count=2)
        mock_agent.config.config_path = "/tmp/my-agent-config.yaml"
        request = _BackgroundDispatchRequest(
            agent=mock_agent,
            run_ids=["run_a", "run_b"],
            launch_id="launch-1",
            router=object(),
            cfg=object(),
            params=object(),
        )

        command = _background_dispatch_command(mock_agent, request)

        assert command[1:4] == ["-u", "-m", "agent_py_agent"]
        assert command[command.index("--config") + 1] == "/tmp/my-agent-config.yaml"
        assert "subagents-dispatch" in command
        assert "-u" in command
        assert "--background-launch-id" in command
        assert command[command.index("--background-launch-id") + 1] == "launch-1"
        assert command.count("--run-id") == 2
        assert command[command.index("--run-id") + 1] == "run_a"
        assert command[command.index("--run-id", command.index("--run-id") + 1) + 1] == "run_b"

    def test_dispatch_cli_accepts_run_id_scope(self):
        """subagents-dispatch CLI 入口要把 --run-id 传成 include_run_ids。"""
        from argparse import Namespace

        from agent_py_agent.cli._dispatch import _dispatch_params, _subagents_dispatch_options

        args = Namespace(
            apply=True,
            execute_runners=True,
            planner=False,
            workflow_mode="off",
            max_runners=2,
            limit=20,
            reviewer="test",
            note="",
            instruction="",
            max_cards=0,
            no_probe=False,
            take_over_by="",
            locked_file=[],
            interval=None,
            max_cycles=0,
            advance=False,
            force_lock=False,
            watch=False,
            run_id=["run_a", "run_b,run_c"],
            background_launch_id="launch-1",
        )

        options = _subagents_dispatch_options(args)
        params = _dispatch_params(options)

        assert options.background_launch_id == "launch-1"
        assert params.include_run_ids == ["run_a", "run_b", "run_c"]


class TestCreateSubagentsToolStartControls:
    """测试 create_subagents 的启动和接管控制。"""

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

    def test_item_defer_start_only_holds_that_child(self, monkeypatch):
        """单个 item.defer_start=true 只挂起该 child，不拖住同批生产 worker。"""
        from types import SimpleNamespace

        import agent_py_agent.agent.agent_core.orchestration_background_dispatch as background_dispatch

        captured: dict[str, object] = {}

        def fake_start(agent, run_ids):
            del agent
            captured["run_ids"] = list(run_ids)
            return {"status": "started", "run_ids": list(run_ids)}

        monkeypatch.setattr(background_dispatch, "_start_background_dispatch", fake_start)
        tasks = [
            SimpleNamespace(id="developer", status="PLANNING", verification_status="UNVERIFIED", attributes={}),
            SimpleNamespace(id="tester", status="PLANNING", verification_status="UNVERIFIED", attributes={"defer_start": True}),
        ]

        payload = background_dispatch.auto_start_tasks(SimpleNamespace(dispatch_subagents=lambda: None), tasks, {})

        assert captured["run_ids"] == ["developer"]
        assert payload["run_ids"] == ["developer"]
        assert payload["deferred_run_ids"] == ["tester"]

    def test_create_subagents_records_explicit_replacement(self, tmp_path):
        """新 child 显式替换旧 run 时，旧 run 进入 TAKEN_OVER，不再被当作活跃任务。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
        from agent_py_agent.agent.subagents.manager import SubAgentManager

        manager = SubAgentManager(tmp_path, workspace_root=tmp_path)
        source = manager.create_run(goal="旧开发代理", thought="卡住了", plan=["写页面"], role="worker")
        agent = SimpleNamespace(
            config=SimpleNamespace(enable_subagents=True, max_subagents=10, subagent_workflow_mode="off", access_mode="workspace-write"),
            subagents=manager,
            tools=SimpleNamespace(specs=lambda: []),
        )

        result = CreateSubagentsTool(agent).execute({
            "goal": "接管旧开发代理继续完成页面",
            "role": "worker",
            "replacement_for_run_ids": [source.id],
            "defer_start": True,
        })
        payload = json.loads(result.output)
        replacement_id = payload["created_run_ids"][0]
        reloaded = manager.load(source.id)

        assert result.ok is True
        assert reloaded.status == "TAKEN_OVER"
        assert reloaded.takeover_by == replacement_id
        assert payload["replacement_records"] == [
            {"source_run_id": source.id, "replacement_run_id": replacement_id, "status": "recorded"}
        ]


class TestCreateSubagentsAutoStartLifecycle:
    """测试后台启动生命周期写回任务树。"""

    def test_dispatch_cli_background_launch_marker_updates_task_tree(self):
        """后台 dispatch 进程要把生命周期写回任务树，父代理才能查到启动状态。"""
        from agent_py_agent.cli.dispatch_background import (
            BackgroundLaunchUpdate,
            mark_background_launch,
        )
        from agent_py_agent.cli.models import SubagentsDispatchOptions

        task = SimpleNamespace(id="run_a", attributes={})
        manager = MagicMock()
        manager.load.return_value = task
        agent = SimpleNamespace(subagents=manager)
        options = SubagentsDispatchOptions(
            apply=True,
            execute_runners=True,
            planner=False,
            workflow_mode="off",
            max_runners=1,
            limit=20,
            reviewer="test",
            note="",
            instruction="",
            max_cards=0,
            probe=True,
            take_over_by="",
            locked_files=[],
            interval=0.0,
            max_cycles=0,
            advance=False,
            force_lock=False,
            watch=False,
            run_ids=["run_a"],
            background_launch_id="launch-1",
        )

        mark_background_launch(agent, options, BackgroundLaunchUpdate("running"))

        assert task.attributes["background_start"]["launch_id"] == "launch-1"
        assert task.attributes["background_start"]["status"] == "running"
        manager.save.assert_called_once_with(task)


class TestCreateSubagentsToolConfigDefaults:
    """测试 create_subagents 对轻量配置对象的默认值兜底。"""

    def test_missing_access_mode_uses_agent_config_default(self):
        """轻量配置对象缺少 access_mode 时，子代理权限仍回退统一配置默认值。"""
        from types import SimpleNamespace

        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
        from agent_py_agent.agent.settings import AgentConfig

        mock_agent = MagicMock()
        mock_agent.config = SimpleNamespace(
            enable_subagents=True,
            max_subagents=10,
            subagent_workflow_mode="off",
        )

        mock_task = MagicMock()
        mock_task.id = "run_default"
        mock_task.goal = ""
        mock_task.status = "PENDING"
        mock_task.verification_status = "PENDING"
        mock_task.task_dir = "/tmp"
        mock_agent.subagents.create_run.return_value = mock_task

        result = CreateSubagentsTool(mock_agent).execute({"goal": "测试"})
        call_kwargs = mock_agent.subagents.create_run.call_args[1]

        assert result.ok is True
        assert call_kwargs["params"].parent_access_mode == AgentConfig().access_mode


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

    def test_vague_deliverable_worker_uses_output_files_without_extra_write_root(self):
        """output_files 是目标事实；即使没有额外写根，也不应阻止普通子代理开工。"""
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

        assert result.ok is True
        mock_agent.subagents.create_run.assert_called_once()
        created_params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert created_params.extra_write_roots == []

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
