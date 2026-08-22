"""Focused tests for create_subagents items-mode role policy."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


def _mock_items_agent(task_count: int = 3):
    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.subagents.workspace_root = Path("/tmp/project")
    mock_agent.subagents.workspace_roots = [Path("/tmp/project")]
    mock_agent.subagents.workspace = Path("/tmp/project/.my-agent/subagents")
    tasks = [_mock_task(index) for index in range(task_count)]
    mock_agent._created_tasks = tasks
    mock_agent.subagents.create_run.side_effect = tasks
    return mock_agent


def _mock_task(index: int):
    task = MagicMock()
    task.id = f"run_{index}"
    task.goal = ""
    task.status = "PLANNING"
    task.verification_status = "UNVERIFIED"
    task.task_dir = f"/tmp/run_{index}"
    return task


def test_items_worker_with_dispatch_tools_stays_worker():
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    mock_agent = _mock_items_agent()
    mock_agent._current_user_prompt = (
        "请你派三个小傻妞一起完成流水线任务。第一位收集数据，第二位基于第一位结果写解释，"
        "第三位整合前两位结果生成报告。"
    )

    result = CreateSubagentsTool(mock_agent).execute({
        "goal": "并行完成数据收集、内容编写与报告整合",
        "items": [
            {
                "goal": "收集 3 个项目基础信息，写到 data_collection.md",
                "agent_name": "小傻妞-数据收集",
                "role": "worker",
            },
            {
                "goal": "基于小傻妞-数据收集提供的结果，写中文说明到 content_writeup.md",
                "agent_name": "小傻妞-内容编写",
                "role": "worker",
            },
            {
                "goal": "整合数据收集和内容编写的结果，生成 final_report.md",
                "agent_name": "小傻妞-生成报告",
                "role": "worker",
            },
        ],
        "allowed_tools": ["read_file", "write_file", "create_subagents", "dispatch_subagents"],
    })

    calls = mock_agent.subagents.create_run.call_args_list
    assert result.ok is True
    assert [call.kwargs["params"].role for call in calls] == ["worker", "worker", "worker"]
    child_controls = {
        "create_subagents",
        "send_guidance",
        "cancel_subagents",
        "resolve_capability_requests",
    }
    assert all(
        child_controls.isdisjoint(call.kwargs["params"].allowed_tools)
        for call in calls
    )
    assert all("用户原始层级" not in call.kwargs["params"].goal for call in calls)


# 真要流水线由父代理显式按顺序派工。
def test_items_path_refs_do_not_persist_hidden_workflow_depends_on():
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    mock_agent = _mock_items_agent()

    result = CreateSubagentsTool(mock_agent).execute({
        "goal": "并行形成有明确引用的三阶段报告",
        "items": [
            {
                "goal": "收集项目基础信息，结果写入 data/subagents/data_collection.md",
                "agent_name": "小傻妞-数据收集",
                "role": "worker",
                "output_refs": ["data/subagents/data_collection.md"],
            },
            {
                "goal": "读取 data/subagents/data_collection.md，写说明依据到 data/subagents/content_writeup.md",
                "agent_name": "小傻妞-内容编写",
                "role": "worker",
                "input_refs": ["data/subagents/data_collection.md"],
                "output_refs": ["data/subagents/content_writeup.md"],
            },
            {
                "goal": "读取 data/subagents/data_collection.md 和 data/subagents/content_writeup.md，生成 final_report.md",
                "agent_name": "小傻妞-生成报告",
                "role": "worker",
                "input_refs": [
                    "data/subagents/data_collection.md",
                    "data/subagents/content_writeup.md",
                ],
            },
        ],
    })

    calls = mock_agent.subagents.create_run.call_args_list
    assert result.ok is True
    assert [hasattr(call.kwargs["params"], "workflow_depends_on") for call in calls] == [
        False,
        False,
        False,
    ]
    assert all(
        "workflow_depends_on" not in (call.kwargs["params"].attributes or {})
        for call in calls
    )
