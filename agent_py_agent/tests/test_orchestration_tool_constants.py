"""LLM: focused tests for orchestration tool constant sets.

模块用途: 验证子代理编排工具常量保持基础读写能力，避免主测试文件继续膨胀。
"""

from __future__ import annotations


# LLM: test_read_only_subagent_tools_contains_read_tools locks basic inspection grants.
# 函数用途: 验证只读子代理工具包包含列目录、读文件和文本搜索。
def test_read_only_subagent_tools_contains_read_tools():
    from agent_py_agent.agent.agent_core.orchestration_tools import READ_ONLY_SUBAGENT_TOOLS

    assert "list_files" in READ_ONLY_SUBAGENT_TOOLS
    assert "read_file" in READ_ONLY_SUBAGENT_TOOLS
    assert "search_text" in READ_ONLY_SUBAGENT_TOOLS


# LLM: test_coding_subagent_tools_contains_file_tools locks product-writing grants.
# 函数用途: 验证写代码/产物的子代理工具包同时包含读工具和安全文件写工具。
def test_coding_subagent_tools_contains_file_tools():
    from agent_py_agent.agent.agent_core.orchestration_tools import CODING_SUBAGENT_TOOLS

    assert "write_file" in CODING_SUBAGENT_TOOLS
    assert "replace_in_file" in CODING_SUBAGENT_TOOLS
    assert "append_file" in CODING_SUBAGENT_TOOLS
    assert "read_file" in CODING_SUBAGENT_TOOLS
    assert "list_files" in CODING_SUBAGENT_TOOLS
