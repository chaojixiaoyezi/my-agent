"""LLM: focused tests for orchestration tool constant sets.

模块用途: 验证子代理编排工具常量保持基础读写能力，避免主测试文件继续膨胀。
"""

from __future__ import annotations


def test_read_only_subagent_tools_contains_read_tools():
    from agent_py_agent.agent.agent_core.orchestration_tools import READ_ONLY_SUBAGENT_TOOLS

    assert "list_files" in READ_ONLY_SUBAGENT_TOOLS
    assert "read_file" in READ_ONLY_SUBAGENT_TOOLS
    assert "search_text" in READ_ONLY_SUBAGENT_TOOLS
    assert "write_file" not in READ_ONLY_SUBAGENT_TOOLS
    assert "apply_patch" not in READ_ONLY_SUBAGENT_TOOLS
    assert "run_command" not in READ_ONLY_SUBAGENT_TOOLS


def test_coding_subagent_tools_contains_file_tools():
    from agent_py_agent.agent.agent_core.orchestration_tools import CODING_SUBAGENT_TOOLS

    assert "write_file" in CODING_SUBAGENT_TOOLS
    assert "apply_patch" in CODING_SUBAGENT_TOOLS
    assert "apply_patch" in CODING_SUBAGENT_TOOLS
    assert "read_file" in CODING_SUBAGENT_TOOLS
    assert "list_files" in CODING_SUBAGENT_TOOLS
    assert "schedule_child_subagents" in CODING_SUBAGENT_TOOLS
    assert "dispatch_subagents" in CODING_SUBAGENT_TOOLS
    assert "inspect_agent_tree" in CODING_SUBAGENT_TOOLS
