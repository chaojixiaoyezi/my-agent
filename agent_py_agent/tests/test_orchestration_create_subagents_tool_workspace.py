from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.tests.test_orchestration_create_subagents_tool import (
    _mock_create_items_agent,
    _mock_created_task,
)


class TestCreateSubagentsToolWorkspaceDefaults:
    """测试任务工作区默认写入根和保守调度提示。"""

    def test_create_next_action_auto_starts_by_default(self, tmp_path):
        """创建多个任务后默认直接开跑，下一步只建议登记非阻塞等待。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        # 后台派工的结构化前置闸要求 workspace 是真实路径类型;给 tmp 路径,
        # 派工启动日志落 tmp 而不是按 MagicMock.__fspath__ 污染仓库目录。
        mock_agent.subagents.workspace = tmp_path
        tasks = []
        for index in range(2):
            task = MagicMock()
            task.id = f"run_{index}"
            task.goal = ""
            task.status = "PLANNING"
            task.verification_status = "UNVERIFIED"
            task.task_dir = f"/tmp/run_{index}"
            tasks.append(task)
        mock_agent.subagents.create_run.side_effect = tasks

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "并行生成周数据并形成报告",
            "items": [
                {"goal": "生成 data/weekly_data.json", "role": "worker"},
                {"goal": "读取 data/weekly_data.json，生成 final_report.md", "role": "worker"},
            ]
        })

        payload = json.loads(result.output)
        assert result.ok is True
        assert payload["auto_start"]["status"] == "started"
        assert payload["auto_start"]["run_ids"] == ["run_0", "run_1"]
        assert payload["pending_start_run_ids"] == []
        assert payload["next_action"]["action"] == "continue_independent_work"
        assert payload["next_action"]["run_ids"] == ["run_0", "run_1"]

    def test_items_mode_does_not_infer_sibling_output_dependencies(self):
        """items 不再根据 sibling 输出自动制造等待；显式读线索原样保留。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent()

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "并行整理、核验并汇总数据",
            "items": [
                {
                    "goal": "整理三周 代码平台 star 数据。",
                    "output_files": ["data/source_data.md"],
                    "agent_name": "小傻妞-数据搜集",
                    "role": "worker",
                },
                {
                    "goal": "核验并翻译。",
                    "dependencies": ["小傻妞-数据搜集"],
                    "output_files": ["data/source_analysis.md"],
                    "agent_name": "小傻妞-核验翻译",
                    "role": "worker",
                },
                {
                    "goal": "生成报告。",
                    "required_read_paths": ["data/source_analysis.md"],
                    "output_files": ["xlsx/final_report.md"],
                    "agent_name": "小傻妞-生成报告",
                    "role": "worker",
                },
            ]
        })

        calls = mock_agent.subagents.create_run.call_args_list
        assert result.ok is True
        assert "required_read_paths" not in calls[1].kwargs["params"].context_manifest
        assert calls[2].kwargs["params"].context_manifest["required_read_paths"] == [
            "data/source_analysis.md",
        ]

    def test_items_mode_preserves_explicit_read_refs_as_hints(self):
        """显式 required_read_paths 作为读线索保留，不再成为启动硬门。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent()

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "并行形成数据、说明与报告",
            "items": [
                {"goal": "收集项目数据。", "output_files": ["data_collection.md"], "agent_name": "小傻妞-数据收集"},
                {
                    "goal": "写中文说明。",
                    "required_read_paths": ["data_collection.md"],
                    "output_files": ["content_writeup.md"],
                    "agent_name": "小傻妞-内容编写",
                },
                {
                    "goal": "生成报告。",
                    "required_read_paths": ["data_collection.md", "content_writeup.md"],
                    "output_files": ["final_report.md"],
                    "agent_name": "小傻妞-生成报告",
                },
            ]
        })

        calls = mock_agent.subagents.create_run.call_args_list
        assert result.ok is True
        assert calls[1].kwargs["params"].context_manifest["required_read_paths"] == [
            "data_collection.md",
        ]
        assert calls[2].kwargs["params"].context_manifest["required_read_paths"] == [
            "data_collection.md",
            "content_writeup.md",
        ]

    def test_items_mode_allows_shared_concrete_output_refs(self):
        """同批子代理可共享工作区，output_files 只是交付元数据。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent(task_count=2)

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "并行检查两个来源",
            "items": [
                {
                    "goal": "检查来源 A，把自己的发现写成中间结果。",
                    "output_files": ["outputs/final_report.json"],
                    "agent_name": "source-a",
                    "role": "worker",
                },
                {
                    "goal": "检查来源 B，把自己的发现写成中间结果。",
                    "output_files": ["outputs/final_report.json"],
                    "agent_name": "source-b",
                    "role": "worker",
                },
            ]
        })

        payload = json.loads(result.output)
        assert result.ok is True
        assert len(payload["created_run_ids"]) == 2
        assert mock_agent.subagents.create_run.call_count == 2


class TestCreateSubagentsToolTaskWorkspaceGuards:
    """测试当前 task workspace 下的追加派工和协作输出边界。"""

    def test_nested_child_inherits_parent_canonical_run_workspace(self, tmp_path):
        """孙代理继续落父任务根，不能在 manager runtime 下另造第二棵任务树。"""
        from agent_py_agent.agent.agent_core.orchestration.create_policy import (
            create_task_attributes,
        )
        from agent_py_agent.agent.agent_core.runner.context import (
            restore_current_subagent_context,
            set_current_subagent_context,
        )

        task_root = (
            tmp_path
            / "home"
            / "owners"
            / "providers"
            / "tui"
            / "users"
            / "u1"
            / "tasks"
            / "2026-08-30"
            / "gwreq-root"
        )
        owner_home = task_root.parents[2]
        project_cwd = owner_home / "tasks" / "older-project"
        parent_attrs = {
            "conversation_execution_cwd": str(project_cwd),
            "conversation_runtime_workspace_roots": [str(owner_home)],
            "run_workspace": {
                "task_root": str(task_root),
                "work_dir": str(task_root / "work"),
                "output_dir": str(task_root / "output"),
            }
        }
        agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner_home))
        previous = set_current_subagent_context(
            agent,
            run_id="subagent-parent",
            attempt_id="attempt-parent",
            task_attributes=parent_attrs,
        )
        try:
            attrs = create_task_attributes(
                {
                    "goal": "创建孙代理",
                    "attributes": {
                        "run_workspace": {
                            "task_root": str(tmp_path / "wrong-manager-runtime-root")
                        }
                    },
                },
                agent,
            )
        finally:
            restore_current_subagent_context(agent, previous)

        assert attrs["run_workspace"] == parent_attrs["run_workspace"]
        assert attrs["conversation_execution_cwd"] == str(project_cwd)
        assert attrs["conversation_runtime_workspace_roots"] == [str(owner_home)]

        from agent_py_agent.agent.subagents.manager import SubAgentManager
        from agent_py_agent.agent.subagents.services.base import CreateRunParams

        manager = SubAgentManager(tmp_path / "manager-runtime")
        nested = manager.create_run(
            params=CreateRunParams(
                goal="孙代理写同一任务产物",
                thought="沿父级任务根工作",
                plan=["读取父级输入", "写入父级 output"],
                root_id="gwreq-root",
                parent_id="subagent-parent",
                depth=2,
                attributes=attrs,
            )
        )

        assert nested.task_workspace_dir == str(task_root)
        assert nested.agent_run_workspace_dir.startswith(str(task_root / "work" / "agents"))
        assert not nested.task_workspace_dir.startswith(str(tmp_path / "manager-runtime"))

    def test_allows_second_batch_while_task_workspace_children_active(self, tmp_path):
        from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), subagent_workspace="subs"), tmp_path)
        task_root = tmp_path / "home" / "owners" / "local" / "main" / "tasks" / "2026-06-01" / "all-agent-架构分析"
        agent._current_run_task_workspace = str(task_root)
        active = agent.subagents.create_run(
            params=create_run_params(agent, {"role": "worker"}, "existing child", ["read_file"])
        )
        active.status = "RUNNING"
        agent.subagents.save(active)

        result = CreateSubagentsTool(agent).execute(
            {"goal": "补读另一批项目源码", "items": [{"goal": "补读另一批项目源码", "role": "worker"}]}
        )

        assert result.ok is True
        payload = json.loads(result.output)
        assert payload["created"] == 1
        assert payload["created_run_ids"][0] != active.id

    def test_allows_child_staged_output_files_in_task_output(self, tmp_path):
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), subagent_workspace="subs"), tmp_path)
        task_root = tmp_path / "home" / "owners" / "local" / "main" / "tasks" / "2026-06-01" / "all-agent-架构分析"
        agent._current_run_task_workspace = str(task_root)

        result = CreateSubagentsTool(agent).execute(
            {
                "goal": "阅读项目并写内部草稿",
                "items": [
                    {
                        "goal": "阅读项目并写内部草稿",
                        "role": "worker",
                        "output_files": [str(task_root / "output" / "agent-group-1-analysis.md")],
                    }
                ]
            }
        )

        assert result.ok is True
        created = agent.subagents.list_runs()[-1]
        assert str(task_root / "output" / "agent-group-1-analysis.md") in created.attributes["output_refs"]

    def test_preserves_external_output_ref_without_granting_external_authority(self, tmp_path):
        from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        workspace = tmp_path / "all-agent"
        workspace.mkdir()
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), subagent_workspace="subs"),
            workspace,
        )
        task_root = tmp_path / "home" / "owners" / "local" / "main" / "tasks" / "2026-06-01" / "all-agent-架构分析"
        agent._current_run_task_workspace = str(task_root)

        params = create_run_params(
            agent,
            {
                "role": "worker",
                "input_refs": [str(workspace / "sample_a-main")],
                "output_files": [str(workspace / "output" / "sample_a-analysis.md")],
            },
            "分析 sample_a-main",
            ["read_file", "write_file"],
        )

        expected = str((workspace / "output" / "sample_a-analysis.md").resolve(strict=False))
        # 显式绝对路径保持身份，不能静默搬家；它是否可写由后续结构化权限门裁决。
        assert params.attributes["output_refs"] == [expected]
        assert params.extra_write_roots == [
            str(agent.home_paths.owner_home_dir.resolve(strict=False)),
        ]

    def test_owner_looking_relative_output_refs_keep_literal_path_segments(self, tmp_path):
        from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        workspace = tmp_path / "fixture"
        workspace.mkdir()
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(workspace / ".my_agent" / "home"), subagent_workspace="subs"),
            workspace,
        )
        task_root = (
            workspace
            / ".my_agent"
            / "home"
            / "owners"
            / "local"
            / "main"
            / "tasks"
            / "2026-06-09"
            / "gwreq-current"
        )
        agent._current_run_task_workspace = str(task_root)

        params = create_run_params(
            agent,
            {
                "role": "worker",
                "output_files": [
                    ".my_agent/home/owners/local/main/tasks/2026-06-09/gwreq-current/output/helper1.md",
                    "my_agent/home/owners/local/main/tasks/2026-06-09/gwreq-current/output/helper2.md",
                ],
            },
            "写两份报告",
            ["read_file", "write_file"],
        )

        assert params.attributes["output_refs"] == [
            str((agent.home_paths.owner_home_dir / ".my_agent/home/owners/local/main/tasks/2026-06-09/gwreq-current/output/helper1.md").resolve(strict=False)),
            str((agent.home_paths.owner_home_dir / "my_agent/home/owners/local/main/tasks/2026-06-09/gwreq-current/output/helper2.md").resolve(strict=False)),
        ]
        assert all("/.my_agent/home/" in ref for ref in params.attributes["output_refs"])
        assert all("/output/" in ref for ref in params.attributes["output_refs"])

    def test_owner_looking_relative_work_refs_keep_literal_path_segments(self, tmp_path):
        from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        workspace = tmp_path / "fixture"
        workspace.mkdir()
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(workspace / ".my_agent" / "home"), subagent_workspace="subs"),
            workspace,
        )
        task_root = (
            workspace
            / ".my_agent"
            / "home"
            / "owners"
            / "local"
            / "main"
            / "tasks"
            / "2026-06-09"
            / "gwreq-current"
        )
        agent._current_run_task_workspace = str(task_root)

        params = create_run_params(
            agent,
            {
                "role": "worker",
                "output_files": [
                    ".my_agent/home/owners/local/main/tasks/2026-06-09/gwreq-current/work/helper1.md",
                    "my_agent/home/owners/local/main/tasks/2026-06-09/gwreq-current/work/helper2.md",
                ],
            },
            "写两份内部报告",
            ["read_file", "write_file"],
        )

        assert params.attributes["output_refs"] == [
            str((agent.home_paths.owner_home_dir / ".my_agent/home/owners/local/main/tasks/2026-06-09/gwreq-current/work/helper1.md").resolve(strict=False)),
            str((agent.home_paths.owner_home_dir / "my_agent/home/owners/local/main/tasks/2026-06-09/gwreq-current/work/helper2.md").resolve(strict=False)),
        ]
        assert not any("/output/.my_agent/" in ref for ref in params.attributes["output_refs"])


class TestCreateSubagentsToolRelativeOutputResolution:
    def test_normalizes_bare_relative_output_refs_to_current_workspace(self, tmp_path):
        from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        workspace = tmp_path / "fixture_project"
        workspace.mkdir()
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), subagent_workspace="subs"),
            workspace,
        )
        task_root = tmp_path / "home" / "owners" / "local" / "main" / "tasks" / "2026-06-09" / "gwreq-current"
        agent._current_run_task_workspace = str(task_root)

        params = create_run_params(
            agent,
            {
                "role": "worker",
                "output_files": ["report_by_helper1.md"],
                "required_read_paths": ["README.md"],
            },
            "读 README 并写报告",
            ["read_file", "write_file"],
        )

        expected = str((agent.home_paths.owner_home_dir / "report_by_helper1.md").resolve(strict=False))
        assert params.attributes["output_refs"] == [expected]
        assert params.extra_write_roots == [str(agent.home_paths.owner_home_dir.resolve(strict=False))]

    def test_output_and_work_are_ordinary_home_relative_names(self, tmp_path):
        from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        workspace = tmp_path / "fixture_project"
        workspace.mkdir()
        agent = SimpleAgent(
            AgentConfig(
                model_backend="echo",
                my_agent_home=str(tmp_path / "home"),
                subagent_workspace="subs",
            ),
            workspace,
        )
        task_root = (
            tmp_path
            / "home"
            / "owners"
            / "local"
            / "main"
            / "tasks"
            / "2026-06-09"
            / "gwreq-current"
        )
        agent._current_run_task_workspace = str(task_root)

        params = create_run_params(
            agent,
            {"role": "worker", "output_files": ["output/report.md"]},
            "写内部阶段报告",
            ["read_file", "write_file"],
        )

        assert params.attributes["output_refs"] == [
            str((agent.home_paths.owner_home_dir / "output" / "report.md").resolve(strict=False))
        ]

        work_params = create_run_params(
            agent,
            {"role": "worker", "output_files": ["work/notes.md"]},
            "写内部工作笔记",
            ["read_file", "write_file"],
        )
        assert work_params.attributes["output_refs"] == [
            str((agent.home_paths.owner_home_dir / "work" / "notes.md").resolve(strict=False))
        ]

    def test_preserves_old_owner_project_output_instead_of_rebasing(self, tmp_path):
        from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        workspace = tmp_path / "all-agent"
        workspace.mkdir()
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), subagent_workspace="subs"),
            workspace,
        )
        current_task = tmp_path / "home" / "owners" / "local" / "main" / "tasks" / "2026-06-07" / "all-agent-架构分析"
        stale_output = (
            tmp_path
            / "home"
            / "owners"
            / "local"
            / "main"
            / "tasks"
            / "2026-06-01"
            / "all-agent-架构分析"
            / "output"
            / "analyzer-1-report.md"
        )
        agent._current_run_task_workspace = str(current_task)

        params = create_run_params(
            agent,
            {"role": "worker", "output_files": [str(stale_output)]},
            "分析 sample_a-main",
            ["read_file", "write_file"],
        )

        expected = str(stale_output.resolve(strict=False))
        assert params.attributes["output_refs"] == [expected]
        assert params.extra_write_roots == [
            str(agent.home_paths.owner_home_dir.resolve(strict=False)),
        ]


class TestCreateSubagentsToolOutputAuthority:
    def test_goal_only_path_does_not_create_structured_output_authority(self, tmp_path):
        """Goal 正文只能提示模型，不能自动生成 output refs 或写权限。"""
        from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        workspace = tmp_path / "all-agent"
        workspace.mkdir()
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), subagent_workspace="subs"),
            workspace,
        )
        current_task = tmp_path / "home" / "owners" / "local" / "main" / "tasks" / "2026-06-07" / "current"
        invented = (
            tmp_path
            / "home"
            / "owners"
            / "local"
            / "main"
            / "tasks"
            / "2026-06-07"
            / "invented-name"
            / "output"
            / "project"
        )
        agent._current_run_task_workspace = str(current_task)

        params = create_run_params(
            agent,
            {"role": "worker"},
            f"把项目写到“{invented}”，并运行测试",
            ["read_file", "write_file"],
        )

        assert str(invented) in params.goal
        assert params.attributes.get("output_refs", []) == []
        assert str(invented) not in params.extra_write_roots
        assert params.extra_write_roots == [str(agent.home_paths.owner_home_dir.resolve(strict=False))]

    def test_goal_path_normalization_keeps_explicit_user_output_dir(self, tmp_path):
        """结构化 user_requested_output_dir 是管理员/用户显式选择，不得偷偷改写。"""
        from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        workspace = tmp_path / "all-agent"
        workspace.mkdir()
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), subagent_workspace="subs"),
            workspace,
        )
        current_task = tmp_path / "home" / "owners" / "local" / "main" / "tasks" / "2026-06-07" / "current"
        explicit = tmp_path / "home" / "owners" / "local" / "main" / "tasks" / "shared" / "output" / "project"
        agent._current_run_task_workspace = str(current_task)

        params = create_run_params(
            agent,
            {
                "role": "worker",
                "goal": f"把项目写到 {explicit}",
                "user_requested_output_dir": str(explicit),
            },
            "",
            ["read_file", "write_file"],
        )

        assert str(explicit) in params.goal
        assert "output_refs" not in params.attributes

    def test_create_subagents_attaches_current_main_run_lineage(self, tmp_path):
        from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), subagent_workspace="subs"), tmp_path)
        agent._current_run_params = SimpleNamespace(run_id="run-main", task_id="task-main")

        params = create_run_params(agent, {"role": "worker"}, "分析子项目", ["read_file"])

        assert params.parent_id == "run-main"
        assert params.root_id == "run-main"
        assert params.depth == 1

    def test_explicit_create_subagents_lineage_wins(self, tmp_path):
        from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
        from agent_py_agent.agent.core import SimpleAgent
        from agent_py_agent.agent.settings import AgentConfig

        agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), subagent_workspace="subs"), tmp_path)
        agent._current_run_params = SimpleNamespace(run_id="run-main", task_id="task-main")

        params = create_run_params(
            agent,
            {"role": "worker", "parent_id": "subagent-parent", "root_id": "subagent-root", "depth": 3},
            "分析孙项目",
            ["read_file"],
        )

        assert params.parent_id == "subagent-parent"
        assert params.root_id == "subagent-root"
        assert params.depth == 3


class TestCreateSubagentsToolWorkspaceRefs:
    """测试同批隔离、显式依赖和读写 refs 的边界。"""

    def test_items_mode_does_not_inject_sibling_goals_or_outputs(self):
        """同批 leaf 只收自己的任务；宿主向父级汇报生命周期，不把兄弟目标复制给每个 child。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent(task_count=2)

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "并行检查两个来源",
            "items": [
                {
                    "goal": "检查来源 A。",
                    "output_files": ["outputs/source_a.json"],
                    "agent_name": "source-a",
                    "role": "worker",
                },
                {
                    "goal": "检查来源 B。",
                    "output_refs": ["outputs/source_b.json"],
                    "agent_name": "source-b",
                    "role": "worker",
                },
            ]
        })

        assert result.ok is True
        for task in mock_agent._created_tasks[:2]:
            serializable_packs = [
                item for item in task.context_packs if isinstance(item, dict)
            ]
            serialized = json.dumps(serializable_packs, ensure_ascii=False)
            assert "sibling_roster" not in serialized
            assert "同一批创建" not in serialized
            assert "run_0" not in serialized
            assert "run_1" not in serialized

    def test_items_mode_dependency_and_shared_output_are_both_preserved(self):
        """依赖表示顺序线索，共享输出不再触发机器所有权拒绝。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent(task_count=2)

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "编写并复核报告",
            "items": [
                {
                    "goal": "先生成初稿。",
                    "output_files": ["outputs/report.md"],
                    "agent_name": "draft-writer",
                    "role": "worker",
                },
                {
                    "goal": "等初稿完成后复核并修订同一个文件。",
                    "dependencies": ["draft-writer"],
                    "output_files": ["outputs/report.md"],
                    "agent_name": "report-reviewer",
                    "role": "worker",
                },
            ]
        })

        payload = json.loads(result.output)
        assert result.ok is True
        assert len(payload["created_run_ids"]) == 2
        assert mock_agent.subagents.create_run.call_count == 2

    def test_items_mode_allows_parent_child_output_scope_overlap(self):
        """目录和子文件可由共享 cwd 的 child 协作，冲突交给实际合并与测试。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent(task_count=2)

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "并行实现端口",
            "items": [
                {
                    "goal": "实现整个核心目录。",
                    "output_files": ["port/core/"],
                    "agent_name": "core-owner",
                },
                {
                    "goal": "实现核心配置。",
                    "output_files": ["port/core/config.py"],
                    "agent_name": "config-owner",
                },
            ],
        })

        payload = json.loads(result.output)
        assert result.ok is True
        assert len(payload["created_run_ids"]) == 2
        assert mock_agent.subagents.create_run.call_count == 2

    def test_items_mode_preserves_long_sibling_output_refs_as_read_hints(self):
        """长路径 sibling 输出也只作为读线索，缺失时由 runner 继续处理。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent()

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "并行收集内容并整合结果",
            "items": [
                {
                    "goal": "收集数据。",
                    "output_files": ["data/subagents/subagent_data_collection/results.md"],
                    "agent_name": "小傻妞-数据收集",
                },
                {
                    "goal": "编写内容。",
                    "output_files": ["data/subagents/subagent_content_writer/results.md"],
                    "agent_name": "小傻妞-内容编写",
                },
                {
                    "goal": "整合结果。",
                    "required_read_paths": [
                        "data/subagents/subagent_data_collection/results.md",
                        "data/subagents/subagent_content_writer/results.md",
                    ],
                    "output_files": ["final_report.md"],
                    "agent_name": "小傻妞-生成报告",
                },
            ]
        })

        calls = mock_agent.subagents.create_run.call_args_list
        assert result.ok is True
        assert calls[2].kwargs["params"].context_manifest["required_read_paths"] == [
            "data/subagents/subagent_data_collection/results.md",
            "data/subagents/subagent_content_writer/results.md",
        ]

    def test_items_mode_does_not_persist_sibling_run_dependencies(self):
        """dependencies 字段不再让 create 阶段替父代理制造流水线等待。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent()

        result = CreateSubagentsTool(mock_agent).execute({
            "goal": "并行完成数据收集、说明与表格生成",
            "items": [
                {"goal": "收集三周 代码平台 star 数据。", "agent_name": "小傻妞-数据收集"},
                {
                    "goal": "写中文说明。",
                    "dependencies": ["小傻妞-数据收集"],
                    "agent_name": "小傻妞-内容编写",
                },
                {
                    "goal": "生成 xlsx 文件。",
                    "dependencies": ["小傻妞-数据收集", "小傻妞-内容编写"],
                    "agent_name": "小傻妞-生成xlsx",
                },
            ]
        })

        assert result.ok is True
        for call in mock_agent.subagents.create_run.call_args_list:
            params = call.kwargs["params"]
            assert not getattr(params, "workflow_depends_on", [])
        assert mock_agent.subagents.save.call_count == 3

class TestCreateSubagentsToolWorkerRoles:
    """测试具体 worker 任务保持显式角色和输出边界。"""

    def test_concrete_single_file_worker_keeps_worker_role(self):
        """具体单文件任务保持 worker 角色。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
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
            "role": "worker",
            "extra_write_roots": ["/tmp/project"],
            "output_files": ["index1.html"],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "worker"

    def test_single_file_worker_is_not_repaired_to_coordinator(self):
        """用户允许多层派工时，单文件 worker 仍应保持交付角色。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
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
            "role": "worker",
            "extra_write_roots": ["/tmp/project/artifacts"],
            "output_files": ["index1.html"],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "worker"

    def test_distinct_file_goals_use_explicit_items(self):
        """多个可写目标必须显式拆成不同 items，而不是克隆同一任务。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent(task_count=2)
        mock_agent._current_user_prompt = "请派小傻妞做两个家具首页。"

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": "在 /tmp/project/artifacts 创建两个不同的单文件 HTML 页面。",
            "items": [
                {
                    "goal": "创建 index1.html。",
                    "role": "worker",
                    "extra_write_roots": ["/tmp/project/artifacts"],
                    "output_files": ["index1.html"],
                },
                {
                    "goal": "创建 index2.html。",
                    "role": "worker",
                    "extra_write_roots": ["/tmp/project/artifacts"],
                    "output_files": ["index2.html"],
                },
            ],
        })

        assert result.ok is True
        assert mock_agent.subagents.create_run.call_count == 2
