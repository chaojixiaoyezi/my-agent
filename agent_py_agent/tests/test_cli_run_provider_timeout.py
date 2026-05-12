from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent_py_agent.agent.backends.errors import ProviderTimeoutError
from agent_py_agent.cli.local_commands import cmd_run


# LLM: _NoopSpinner prevents CLI timeout tests from starting terminal animation threads.
# 类用途: 替代 ThinkingSpinner，保持测试输出稳定且不创建后台 UI 行为。
class _NoopSpinner:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


# LLM: cmd_run should close provider-timeout runs with a readable report and nonzero exit.
# 函数用途: 顶层 CLI 模型接口超时时，不打印 Python 堆栈，也不让用户以为还在运行。
def test_cmd_run_reports_provider_timeout(capsys) -> None:
    agent = SimpleNamespace(
        config=SimpleNamespace(request_timeout=23),
        run=MagicMock(side_effect=ProviderTimeoutError("模型接口请求超时: request_timeout=23s")),
    )
    args = SimpleNamespace(
        config="config.yaml",
        prompt="run a task",
        inject=[],
        prompt_file=[],
        save=False,
        show_prompt=False,
        resume_context=None,
    )

    with (
        patch("agent_py_agent.cli.local_commands.make_agent", return_value=agent),
        patch("agent_py_agent.cli.local_commands.ThinkingSpinner", return_value=_NoopSpinner()),
    ):
        code = cmd_run(args)

    output = capsys.readouterr().out
    assert code == 2
    assert "provider_timeout" in output
    assert "request_timeout=23s" in output
    assert "memory-resume" in output
