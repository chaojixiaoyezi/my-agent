"""Focused create_subagents contract regressions kept out of the large orchestration suite."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


def _mock_items_agent(task_count: int = 3):
    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.subagents.workspace_root = Path("/tmp/project")
    mock_agent.subagents.workspace_roots = [Path("/tmp/project")]
    mock_agent.subagents.workspace = Path("/tmp/project/.my-agent/subagents")
    tasks = []
    for index in range(task_count):
        task = MagicMock()
        task.id = f"run_{index}"
        task.goal = ""
        task.status = "PLANNING"
        task.verification_status = "UNVERIFIED"
        task.task_dir = f"/tmp/run_{index}"
        tasks.append(task)
    mock_agent.subagents.create_run.side_effect = tasks
    return mock_agent


def test_items_mode_preserves_absolute_path_dependencies():
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    mock_agent = _mock_items_agent()
    root = "/tmp/project/task18"

    result = CreateSubagentsTool(mock_agent).execute({
        "items": [
            {
                "goal": f"收集数据，输出到 {root}/data/subagents/data_collection.md",
                "agent_name": "小傻妞-数据收集",
                "output_refs": [f"{root}/data/subagents/data_collection.md"],
            },
            {
                "goal": f"读取 {root}/data/subagents/data_collection.md，写中文说明。",
                "agent_name": "小傻妞-内容编写",
                "input_refs": [f"{root}/data/subagents/data_collection.md"],
            },
        ]
    })

    calls = mock_agent.subagents.create_run.call_args_list
    assert result.ok is True
    assert calls[1].kwargs["params"].context_manifest["required_read_paths"] == [
        f"{root}/data/subagents/data_collection.md"
    ]


def test_researcher_role_gets_web_tools_by_default():
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    mock_agent = _mock_items_agent(task_count=1)

    result = CreateSubagentsTool(mock_agent).execute({
        "items": [
            {
                "goal": "查 代码平台 项目并用 web_fetch 验证页面可访问。",
                "agent_name": "小傻妞-数据收集",
                "role": "researcher",
            }
        ]
    })

    params = mock_agent.subagents.create_run.call_args.kwargs["params"]
    assert result.ok is True
    assert "web_search" in params.allowed_tools
    assert "web_fetch" in params.allowed_tools
