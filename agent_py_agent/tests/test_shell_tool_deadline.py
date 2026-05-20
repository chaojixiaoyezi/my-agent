# LLM: Shell deadline tests prove tool timeout contracts honor parent task budgets.
# 模块用途: 验证 run_command 不会因为模型传入过长 timeout 而吃掉整个真实任务时间。

from __future__ import annotations

import time

from agent_py_agent.agent.tooling.shell import ShellTool, _timeout_from_params


# LLM: deadline caps should shorten oversized model-requested shell timeouts.
# 函数用途: 模拟外层任务只剩 20 秒时，run_command timeout=100 会被自动收紧。
def test_shell_timeout_caps_to_runtime_deadline(monkeypatch):
    monkeypatch.setenv("MY_AGENT_TOOL_DEADLINE_UNIX", str(time.time() + 20))
    monkeypatch.setenv("MY_AGENT_TOOL_DEADLINE_MARGIN_SECONDS", "5")

    timeout = _timeout_from_params({"timeout": 100}, default_timeout=30)

    assert 10 <= timeout <= 15


# LLM: expired deadlines should stop shell execution before any subprocess starts.
# 函数用途: 外层任务时间已不足时，run_command 返回稳定 TOOL_TIMEOUT 错误而不是继续卡住。
def test_shell_tool_refuses_expired_runtime_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_AGENT_TOOL_DEADLINE_UNIX", str(time.time() - 1))
    monkeypatch.setenv("MY_AGENT_TOOL_DEADLINE_MARGIN_SECONDS", "0")
    tool = ShellTool(tmp_path, default_timeout=30)

    result = tool.execute({"command": "echo should-not-run", "timeout": 100})

    assert result.ok is False
    assert result.error_code == "TOOL_TIMEOUT"
    assert "TOOL_DEADLINE_EXCEEDED" in result.output
