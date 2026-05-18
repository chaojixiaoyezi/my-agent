"""Focused tests for create_subagents items-mode role policy."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


# LLM: _mock_items_agent keeps items role-policy tests independent from the larger create tool test file.
# 函数用途: 构造最小 create_subagents items 测试替身，避免主测试文件继续变大。
def _mock_items_agent(task_count: int = 3):
    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.config.subagent_workflow_mode = "off"
    mock_agent.subagents.workspace_root = Path("/tmp/project")
    mock_agent.subagents.workspace_roots = [Path("/tmp/project")]
    mock_agent.subagents.workspace = Path("/tmp/project/.my-agent/subagents")
    tasks = [_mock_task(index) for index in range(task_count)]
    mock_agent._created_tasks = tasks
    mock_agent.subagents.create_run.side_effect = tasks
    return mock_agent


# LLM: _mock_task provides only payload fields used by CreateSubagentsTool.
# 函数用途: 返回带 id/status/task_dir 的任务替身，供 create_run side_effect 使用。
def _mock_task(index: int):
    task = MagicMock()
    task.id = f"run_{index}"
    task.goal = ""
    task.status = "PLANNING"
    task.verification_status = "UNVERIFIED"
    task.task_dir = f"/tmp/run_{index}"
    return task


# LLM: This regression separates capability grants from orchestration intent.
# 函数用途: 验证 broad allowed_tools 只是授权，不会把 items[] 普通 worker 误升为 coordinator。
def test_items_worker_with_dispatch_tools_stays_worker():
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    mock_agent = _mock_items_agent()
    mock_agent._current_user_prompt = (
        "请你派三个小傻妞一起完成流水线任务。第一位收集数据，第二位基于第一位结果写解释，"
        "第三位整合前两位结果生成报告。"
    )

    result = CreateSubagentsTool(mock_agent).execute({
        "items": [
            {
                "goal": "收集 3 个项目基础信息，写到 data_collection.md",
                "agent_name": "小傻妞-数据收集",
                "role": "worker",
            },
            {
                "goal": "基于小傻妞-数据收集提供的结果，写中文解释到 content_writeup.md",
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
    assert all("用户原始层级" not in call.kwargs["params"].goal for call in calls)


# LLM: This regression keeps path-based sibling pipelines machine ordered.
# 函数用途: 验证下游读取上游输出文件时，即使没有提上游代理全名，也会写入 workflow_depends_on。
def test_items_path_refs_create_workflow_dependency_edges():
    from agent_py_agent.agent.agent_core.orchestration_create_items import CreateSubagentItem
    from agent_py_agent.agent.agent_core.orchestration_item_dependencies import (
        item_dependency_edges,
    )

    items = [
        CreateSubagentItem(
            goal="收集项目基础信息，结果写入 data/subagents/data_collection.md",
            params={
                "agent_name": "小傻妞-数据收集",
                "output_refs": ["data/subagents/data_collection.md"],
            },
        ),
        CreateSubagentItem(
            goal="读取 data/subagents/data_collection.md，写推荐理由到 data/subagents/content_writeup.md",
            params={
                "agent_name": "小傻妞-内容编写",
                "dependencies": ["data_collection"],
                "output_refs": ["data/subagents/content_writeup.md"],
            },
        ),
        CreateSubagentItem(
            goal=(
                "读取 data/subagents/data_collection.md 和 data/subagents/content_writeup.md，"
                "生成 final_report.md"
            ),
            params={
                "agent_name": "小傻妞-生成报告",
                "input_refs": [
                    "data/subagents/data_collection.md",
                    "data/subagents/content_writeup.md",
                ],
            },
        ),
    ]

    assert item_dependency_edges(items) == [[], [0], [0, 1]]


# LLM: This regression proves create_subagents persists item dependency edges onto tasks.
# 函数用途: 确保真实 create 工具会把路径推断出的依赖写成 workflow_depends_on，而不是只停留在临时推断结果。
def test_items_path_refs_persist_workflow_depends_on():
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    mock_agent = _mock_items_agent()

    result = CreateSubagentsTool(mock_agent).execute({
        "items": [
            {
                "goal": "收集项目基础信息，结果写入 data/subagents/data_collection.md",
                "agent_name": "小傻妞-数据收集",
                "role": "worker",
                "output_refs": ["data/subagents/data_collection.md"],
            },
            {
                "goal": "读取 data/subagents/data_collection.md，写推荐理由到 data/subagents/content_writeup.md",
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

    tasks = mock_agent._created_tasks
    assert result.ok is True
    assert tasks[0].workflow_depends_on == []
    assert tasks[1].workflow_depends_on == ["run_0"]
    assert tasks[2].workflow_depends_on == ["run_0", "run_1"]
