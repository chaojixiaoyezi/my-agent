"""LLM: focused tests for explicit root/coordinator seed tool merging.

函数/模块用途: 验证主代理创建 root coordinator 时会保留父级已给工具，并补齐协调工具。
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _mock_coordinator_agent():
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
    return mock_agent


def test_explicit_coordinator_seed_merges_parent_and_coordinator_tools():
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.subagents.role_templates import COORDINATOR_TOOLS

    mock_agent = _mock_coordinator_agent()
    tool = CreateSubagentsTool(mock_agent)
    result = tool.execute({
        "goal": "Seed one root coordinator.",
        "role": "coordinator",
        "allowed_tools": ["schedule_child_subagents", "run_command", "web_fetch", "write_file"],
    })

    params = mock_agent.subagents.create_run.call_args.kwargs["params"]
    assert result.ok is True
    for tool_name in COORDINATOR_TOOLS:
        assert tool_name in params.allowed_tools
    assert "run_command" in params.allowed_tools
    assert "web_fetch" in params.allowed_tools
    assert "write_file" in params.allowed_tools


def test_explicit_coordinator_seed_keeps_product_paths_and_write_grants():
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    mock_agent = _mock_coordinator_agent()
    tool = CreateSubagentsTool(mock_agent)
    result = tool.execute({
        "goal": "Coordinate role boundary evidence for /tmp/product-deliverables.",
        "role": "coordinator",
        "agent_name": "root-coordinator",
        "allowed_tools": ["schedule_child_subagents", "write_file"],
        "extra_write_roots": ["/tmp/product-deliverables"],
    })

    params = mock_agent.subagents.create_run.call_args.kwargs["params"]
    assert result.ok is True
    assert "/tmp/product-deliverables" in params.goal
    assert params.extra_write_roots == ["/tmp/product-deliverables"]
