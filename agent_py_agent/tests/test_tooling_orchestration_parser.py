"""LLM: focused parser tests for model-emitted orchestration tool calls.

模块用途: 验证真实模型常见的 orchestration 参数包能被展开成稳定 bundle 接口。
"""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.tests.test_tools.backends import make_tool_registry


# LLM: orchestration wrappers are accepted because real runners often group child specs under that key.
# 函数用途: 模型把 schedule_child_subagents 参数包在 orchestration 字段里时，解析器要展开成工具可执行参数。
def test_tool_call_parser_unwraps_model_orchestration_bundle():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n'
        '{"tool":"schedule_child_subagents","orchestration":{"apply":true,'
        '"children":[{"goal":"child goal","agent_name":"child"}]}}\n'
        '[/TOOL_CALL]'
    )

    assert calls == [
        {
            "tool": "schedule_child_subagents",
            "apply": True,
            "children": [{"goal": "child goal", "agent_name": "child"}],
        }
    ]
