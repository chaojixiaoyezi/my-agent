"""LLM: focused tests for orchestration workflow-mode normalization.

模块用途: 验证模型/配置传入 workflow_mode 时，会稳定归一到 off/plan/auto。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.subagents.services.workflow import tool_workflow_mode


@pytest.mark.parametrize(
    ("explicit", "configured", "expected"),
    [
        ("off", "auto", "off"),
        ("auto", "off", "auto"),
        ("plan", "off", "plan"),
        ("plan", "auto", "plan"),
        ("auto", "manual", "auto"),
        (None, "auto", "off"),
        (None, "manual", "off"),
        ("invalid", "invalid", "off"),
        ("execute", "auto", "off"),
        ("  off  ", "auto", "off"),
    ],
)
def test_tool_workflow_mode_cases(explicit, configured, expected):
    assert tool_workflow_mode(explicit, configured) == expected
