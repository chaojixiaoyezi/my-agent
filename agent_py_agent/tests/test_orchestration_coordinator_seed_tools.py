"""LLM: focused tests for explicit root/coordinator seed tool filtering.

函数/模块用途: 验证主代理创建 root coordinator 时不会因模型多写工具名而扩大权限。
"""

from __future__ import annotations

from unittest.mock import MagicMock


# LLM: _mock_coordinator_agent builds the minimal facade CreateSubagentsTool needs.
# 函数用途: 构造启用 subagent 的 mock agent，避免每个 coordinator seed 测试重复字段。
def _mock_coordinator_agent():
    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.config.subagent_workflow_mode = "off"

    mock_task = MagicMock()
    mock_task.id = "coordinator_001"
    mock_task.goal = ""
    mock_task.status = "PLANNING"
    mock_task.verification_status = "UNVERIFIED"
    mock_task.task_dir = "/tmp/coordinator_001"
    mock_agent.subagents.create_run.return_value = mock_task
    return mock_agent


# LLM: test_explicit_coordinator_seed_filters_model_shell_web_tools covers real model over-grants.
# 函数用途: 模型误给 root 塞 shell/web 工具时，显式 coordinator seed 仍只拿内置协调工具。
def test_explicit_coordinator_seed_filters_model_shell_web_tools():
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.subagents.role_templates import COORDINATOR_TOOLS

    mock_agent = _mock_coordinator_agent()
    tool = CreateSubagentsTool(mock_agent)
    result = tool.execute({
        "goal": "Seed one root coordinator.",
        "role": "coordinator",
        "allowed_tools": ["schedule_child_subagents", "run_command", "fetch_url", "write_file"],
    })

    params = mock_agent.subagents.create_run.call_args.kwargs["params"]
    assert result.ok is True
    assert params.allowed_tools == COORDINATOR_TOOLS
    assert "run_command" not in params.allowed_tools
    assert "fetch_url" not in params.allowed_tools


# LLM: test_explicit_coordinator_seed_keeps_product_paths_as_context_not_write_grants locks the root boundary.
# 函数用途: root coordinator 可以在 goal 里保留产物目录给下级派工，但自己不能拿产物目录写权限。
def test_explicit_coordinator_seed_keeps_product_paths_as_context_not_write_grants():
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
    assert params.extra_write_roots == []
