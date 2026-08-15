from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent_py_agent.agent.agent_core.cli_run_conversation import (
    CliRunConversationPersistenceError,
)
from agent_py_agent.agent.backends.errors import (
    ProviderRecoverableError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderTransientError,
    is_provider_recoverable_error,
)
from agent_py_agent.cli.local_commands import cmd_run


class _NoopSpinner:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


def _mock_agent(tmp_path, run_side_effect):
    """构造 cmd_run 所需的 mock agent（含真实 ConversationStore 供会话绑定）。"""
    from agent_py_agent.agent.conversation.store import ConversationStore

    return SimpleNamespace(
        config=SimpleNamespace(request_timeout=23),
        conversation_store=ConversationStore(tmp_path / "conv"),
        run=MagicMock(side_effect=run_side_effect),
        root=str(tmp_path),
        runtime_guard_policy=None,
        home_paths=SimpleNamespace(
            owner_id="local/main",
            owner_home_dir=str(tmp_path),
        ),
    )


def test_cmd_run_reports_provider_timeout(capsys, tmp_path) -> None:
    agent = _mock_agent(
        tmp_path,
        ProviderTimeoutError("模型接口请求超时: request_timeout=23s"),
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


def test_cmd_run_reports_provider_transient(capsys, tmp_path) -> None:
    agent = _mock_agent(tmp_path, ProviderTransientError("HTTP 429: plan limited"))
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
    assert "provider_transient" in output
    assert "HTTP 429" in output
    assert "稍后重试" in output


def test_cmd_run_reports_provider_response_error(capsys, tmp_path) -> None:
    agent = _mock_agent(tmp_path, ProviderResponseError("missing choices"))
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
    assert "provider_response_error" in output
    assert "missing choices" in output


def test_provider_timeout_and_transient_share_recoverable_base() -> None:
    assert issubclass(ProviderTimeoutError, ProviderRecoverableError)
    assert issubclass(ProviderTransientError, ProviderRecoverableError)
    assert issubclass(ProviderResponseError, ProviderRecoverableError)
    assert is_provider_recoverable_error(ProviderTimeoutError("timeout"))
    assert is_provider_recoverable_error(ProviderTransientError("429"))
    assert is_provider_recoverable_error(ProviderResponseError("bad payload"))


def test_cmd_run_reports_conversation_persistence_failure_without_traceback(capsys) -> None:
    agent = SimpleNamespace(
        config=SimpleNamespace(request_timeout=23),
        run=MagicMock(
            side_effect=CliRunConversationPersistenceError(
                "CLI run 用户消息无法可靠写入 ConversationStore，已在模型执行前停止。"
            )
        ),
    )
    args = SimpleNamespace(
        config="config.yaml",
        prompt="请记住一条事实",
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
    assert "cli_run_conversation_persistence" in output
    assert "模型和工具均未执行" in output
    assert "Traceback" not in output
