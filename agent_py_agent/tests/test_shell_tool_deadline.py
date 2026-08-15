
from __future__ import annotations

import time

from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions, _timeout_from_params


def test_shell_timeout_caps_to_runtime_deadline(monkeypatch):
    monkeypatch.setenv("MY_AGENT_TOOL_DEADLINE_UNIX", str(time.time() + 20))
    monkeypatch.setenv("MY_AGENT_TOOL_DEADLINE_MARGIN_SECONDS", "5")

    timeout = _timeout_from_params({"timeout": 100}, default_timeout=30)

    assert 10 <= timeout <= 15


def test_shell_tool_refuses_expired_runtime_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_AGENT_TOOL_DEADLINE_UNIX", str(time.time() - 1))
    monkeypatch.setenv("MY_AGENT_TOOL_DEADLINE_MARGIN_SECONDS", "0")
    tool = ShellTool(tmp_path, options=ShellToolOptions(default_timeout=30))

    result = tool.execute({"command": "echo should-not-run", "timeout": 100})

    assert result.ok is False
    assert result.error_code == "TOOL_TIMEOUT"
    assert "TOOL_DEADLINE_EXCEEDED" in result.output
