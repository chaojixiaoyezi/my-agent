"""LLM: focused tests for orchestration workflow-mode normalization.

模块用途: 验证模型/配置传入 workflow_mode 时，会稳定归一到 off/plan/auto。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.agent_core.orchestration_tools import _tool_workflow_mode


# LLM: test_tool_workflow_mode_cases keeps workflow mode compatibility compact.
# 函数用途: 用参数化覆盖显式值、配置 fallback、默认 off 和空格处理。
@pytest.mark.parametrize(
    ("explicit", "configured", "expected"),
    [
        ("off", "auto", "off"),
        ("auto", "off", "off"),
        ("plan", "off", "off"),
        ("plan", "auto", "plan"),
        ("auto", "manual", "auto"),
        (None, "auto", "auto"),
        (None, "manual", "plan"),
        ("invalid", "invalid", "off"),
        ("execute", "auto", "off"),
        ("  off  ", "auto", "off"),
    ],
)
def test_tool_workflow_mode_cases(explicit, configured, expected):
    assert _tool_workflow_mode(explicit, configured) == expected
