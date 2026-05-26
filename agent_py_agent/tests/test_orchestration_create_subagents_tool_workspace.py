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

    def test_create_next_action_starts_conservatively(self):
        """创建多个任务后的默认下一步先推进 1 个，避免流水线下游抢跑。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        mock_agent.config.subagent_workflow_mode = "off"
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
            "items": [
                {"goal": "生成 data/weekly_data.json", "role": "worker"},
                {"goal": "读取 data/weekly_data.json，生成 final_report.md", "role": "worker"},
            ]
        })

        payload = json.loads(result.output)
        assert result.ok is True
        assert payload["next_action"]["params"]["max_runners"] == 2

    def test_items_mode_does_not_infer_sibling_output_dependencies(self):
        """items 不再根据 sibling 输出自动制造等待；显式读线索原样保留。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent()

        result = CreateSubagentsTool(mock_agent).execute({
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
        """共享输出不再由 create 阶段硬拒，交给 prompt、tree 和 closeout 判断。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent(task_count=2)

        result = CreateSubagentsTool(mock_agent).execute({
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

        assert result.ok is True
        assert mock_agent.subagents.create_run.call_count == 2


class TestCreateSubagentsToolWorkspaceRefs:
    """测试 siblings、显式依赖和读写 refs 的边界。"""

    def test_items_mode_sibling_roster_does_not_publish_future_outputs(self):
        """同批 peer 目录只暴露身份，不把未来产物路径注入每个子代理上下文。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent(task_count=2)

        result = CreateSubagentsTool(mock_agent).execute({
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
            roster = next(pack for pack in task.context_packs if pack["kind"] == "sibling_roster")
            assert "outputs/source_a.json" not in json.dumps(roster, ensure_ascii=False)
            assert "outputs/source_b.json" not in json.dumps(roster, ensure_ascii=False)
            assert {item["run_id"] for item in roster["siblings"]} == {"run_0", "run_1"}

    def test_items_mode_allows_shared_output_refs_with_explicit_dependency(self):
        """明确上下游等待时，不把同一文件的修订/复核链误判成并行抢写。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent(task_count=2)

        result = CreateSubagentsTool(mock_agent).execute({
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

        assert result.ok is True
        assert mock_agent.subagents.create_run.call_count == 2

    def test_items_mode_preserves_long_sibling_output_refs_as_read_hints(self):
        """长路径 sibling 输出也只作为读线索，缺失时由 runner 继续处理。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent()

        result = CreateSubagentsTool(mock_agent).execute({
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
            "output_files": ["index1.html"],
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
            "output_files": ["index1.html"],
        })

        params = mock_agent.subagents.create_run.call_args.kwargs["params"]
        assert result.ok is True
        assert params.role == "child_worker"
        assert params.workflow_mode == "off"

    def test_repeated_concrete_file_goal_is_not_blocked_by_hidden_split_gate(self):
        """count 复制具体文件目标时不再被 create 阶段硬拒绝；父代理自行控制分工。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = _mock_create_items_agent(task_count=2)
        mock_agent.config.subagent_workflow_mode = "auto"
        mock_agent._current_user_prompt = "请派小傻妞做两个家具首页。"

        tool = CreateSubagentsTool(mock_agent)
        result = tool.execute({
            "goal": (
                "在 /tmp/project/artifacts 创建 index1.html 和 index2.html 两个单文件 HTML 页面。"
            ),
            "count": 2,
            "role": "leaf_worker",
            "workflow_mode": "auto",
            "extra_write_roots": ["/tmp/project/artifacts"],
            "output_files": ["index1.html", "index2.html"],
        })

        assert result.ok is True
        assert mock_agent.subagents.create_run.call_count == 2
