"""LLM: focused parser tests for model-emitted orchestration tool calls.

模块用途: 验证 orchestration 工具调用保持顶层参数，不再接受额外包装层。
"""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.tests.test_tools.backends import make_tool_registry


def test_tool_call_parser_keeps_flat_orchestration_params():
    registry = make_tool_registry(Path.cwd())

    calls = registry.parse_tool_calls(
        '[TOOL_CALL]\n'
        '{"tool":"schedule_child_subagents","dry_run":false,'
        '"children":[{"goal":"child goal","agent_name":"child"}]}\n'
        '[/TOOL_CALL]'
    )

    assert calls == [
        {
            "tool": "schedule_child_subagents",
            "dry_run": False,
            "children": [{"goal": "child goal", "agent_name": "child"}],
        }
    ]
