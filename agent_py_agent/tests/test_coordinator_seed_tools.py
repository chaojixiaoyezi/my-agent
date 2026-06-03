"""LLM: Tests for explicit root/coordinator tool grants.

模块用途: 锁定显式 root/coordinator seed 不再吞掉父级传入的工具。
"""

from __future__ import annotations


def test_explicit_root_allowed_tools_merges_parent_grants():
    from agent_py_agent.agent.agent_core.coordinator_seed_tools import explicit_root_allowed_tools

    result = explicit_root_allowed_tools(["read_file", "run_command"])

    assert result is not None
    assert "read_file" in result
    assert "run_command" in result
    assert "dispatch_subagents" in result
