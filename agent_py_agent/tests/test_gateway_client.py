from __future__ import annotations

"""gateway client regression tests."""

from agent_py_agent.cli import gateway_client


def test_default_gateway_entry_can_reach_chat_handler():
    """默认 gateway 入口必须能找到 chat 处理函数。"""

    assert callable(gateway_client.cmd_chat)
