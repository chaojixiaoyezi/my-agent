from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.concurrency.interrupt import is_interrupted, register_interruptible
from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlResult,
    parse_conversation_control,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.chat_parts.control_runtime import (
    ChatControlExecution,
    ChatControlState,
    _command_text,
    execute_chat_control,
)
from agent_py_agent.cli.chat_parts.slash_command_types import SlashCommandContext
from agent_py_agent.cli.chat_parts.slash_commands import handle_common_slash_command


def _command(text: str):
    command = parse_conversation_control(text)
    assert command is not None
    return command


def test_slash_btw_uses_control_executor_without_persistent_inject() -> None:
    output: list[str] = []
    runtime_inject = ["existing startup inject"]
    seen: list[str] = []

    def execute(command):
        seen.append(command.value)
        return ConversationControlResult("steer", True, "已补充到当前任务。")

    handled = handle_common_slash_command(
        "/btw 先核对事实",
        ctx=SlashCommandContext(
            agent=MagicMock(),
            memory_limit=5,
            runtime_inject=runtime_inject,
            prompt_files=[],
            print_line=output.append,
            control_executor=execute,
        ),
    )

    assert handled is True
    assert seen == ["先核对事实"]
    assert output == ["已补充到当前任务。"]
    assert runtime_inject == ["existing startup inject"]


def test_slash_btw_clear_only_returns_usage() -> None:
    output: list[str] = []
    executor = MagicMock()
    handled = handle_common_slash_command(
        "/btw-clear",
        ctx=SlashCommandContext(
            agent=MagicMock(),
            memory_limit=5,
            runtime_inject=[],
            prompt_files=[],
            print_line=output.append,
            control_executor=executor,
        ),
    )

    assert handled is True
    assert output == ["用法：/btw 你的补充要求"]
    executor.assert_not_called()


def test_slash_stop_executes_without_printing_a_chat_reply() -> None:
    output: list[str] = []
    executor = MagicMock(
        return_value=ConversationControlResult(
            "stop",
            True,
            "internal result is not user chat",
            request_id="req-1",
        )
    )

    handled = handle_common_slash_command(
        "/stop",
        ctx=SlashCommandContext(
            agent=MagicMock(),
            memory_limit=5,
            runtime_inject=[],
            prompt_files=[],
            print_line=output.append,
            control_executor=executor,
        ),
    )

    assert handled is True
    executor.assert_called_once()
    assert output == []


def test_local_btw_is_scoped_to_current_request(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    execution = ChatControlExecution(
        agent,
        False,
        ChatControlState(
            True,
            0,
            "长任务",
            time.perf_counter(),
            "session-1",
            request_id="chat-1",
        ),
    )

    result = execute_chat_control(execution, _command("/btw 改为先写摘要"))

    assert result.ok is True
    pending = agent.conversation_store.pending_guidance("request", "chat-1")
    assert pending[0].message == "改为先写摘要"
    assert agent.conversation_store.pending_guidance("request", "chat-2") == []


def test_local_stop_signals_current_run(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    execution = ChatControlExecution(
        agent,
        False,
        ChatControlState(
            True,
            0,
            "长任务",
            time.perf_counter(),
            "session-1",
            request_id="chat-stop",
        ),
    )
    ready = threading.Event()
    stopped = threading.Event()

    def worker() -> None:
        with register_interruptible("conversation-request:chat-stop"):
            ready.set()
            while not is_interrupted():
                time.sleep(0.01)
            stopped.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(timeout=2)
    agent.conversation_store.append_guidance(
        {
            "target_type": "request",
            "target_id": "chat-stop",
            "message": "尚未消费的旧引导",
            "sender": "local-cli",
            "delivery": "current_request",
        }
    )

    result = execute_chat_control(execution, _command("/stop"))
    thread.join(timeout=2)

    assert result.ok is True
    assert stopped.is_set()
    assert agent.conversation_store.pending_guidance("request", "chat-stop") == []


def test_local_stop_uses_shared_request_id_instead_of_thread_local_params(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    agent._current_run_params = SimpleNamespace(request_id="wrong-thread-local-id")
    execution = ChatControlExecution(
        agent,
        False,
        ChatControlState(
            True,
            0,
            "长任务",
            time.perf_counter(),
            "session-1",
            request_id="chat-shared-state",
        ),
    )
    ready = threading.Event()
    stopped = threading.Event()

    def worker() -> None:
        with register_interruptible("conversation-request:chat-shared-state"):
            ready.set()
            while not is_interrupted():
                time.sleep(0.01)
            stopped.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(timeout=2)

    result = execute_chat_control(execution, _command("/stop"))
    thread.join(timeout=2)

    assert result.ok is True
    assert stopped.is_set()


def test_local_stop_fails_when_running_snapshot_has_no_interruptible_request(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    execution = ChatControlExecution(
        agent,
        False,
        ChatControlState(True, 0, "启动中", time.perf_counter(), "session-1"),
    )

    result = execute_chat_control(execution, _command("/stop"))

    assert result.ok is False
    assert "没有运行中的内容" in result.message


def test_local_verbose_is_system_control_without_model_call(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    agent.run = MagicMock(side_effect=AssertionError("must not call model"))
    execution = ChatControlExecution(
        agent,
        False,
        ChatControlState(False, 0, "", 0.0, "session-verbose"),
    )

    result = execute_chat_control(execution, _command("/verbose full"))

    assert result.ok is True
    assert "完整过程" in result.message
    agent.run.assert_not_called()


def test_gateway_control_uses_configured_service_command_timeout(monkeypatch) -> None:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = (
        b'{"ok": true, "message": "status", "request_id": ""}'
    )
    urlopen = MagicMock(return_value=response)
    monkeypatch.setattr(
        "agent_py_agent.cli.chat_parts.control_runtime.urllib.request.urlopen",
        urlopen,
    )
    execution = ChatControlExecution(
        SimpleNamespace(
            config=SimpleNamespace(
                gateway_port=8420,
                gateway_service_command_timeout_seconds=37,
            )
        ),
        True,
        ChatControlState(False, 0, "", 0.0, "session-status"),
    )

    result = execute_chat_control(execution, _command("/status"))

    assert result.ok is True
    assert urlopen.call_args.kwargs["timeout"] == 37.0


def test_local_status_does_not_show_guidance_history(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", model_name="MiniMax-M2.7"), tmp_path)
    execution = ChatControlExecution(
        agent,
        False,
        ChatControlState(True, 2, "整理资料", time.perf_counter() - 65, "session-1"),
    )

    result = execute_chat_control(execution, _command("/status"))

    assert "状态：运行中" in result.message
    assert "等待中的消息：2" in result.message
    assert "MiniMax-M2.7" in result.message
    assert "引导" not in result.message


def test_gateway_goal_command_serialization_preserves_operation_and_value() -> None:
    assert _command_text(_command("/goal")) == "/goal"
    assert _command_text(_command("/goal 连续检查发布健康")) == "/goal 连续检查发布健康"
    assert _command_text(_command("/goal edit 改为每日检查")) == "/goal edit 改为每日检查"
    assert _command_text(_command("/goal pause")) == "/goal pause"
    assert (
        _command_text(_command("/goal 7d 周报整理 整理本周资料"))
        == "/goal 7d 周报整理 整理本周资料"
    )
    assert _command_text(_command("/goal 周报整理 clear")) == "/goal 周报整理 clear"
    assert (
        _command_text(_command("/audit 5m CLI三源 持续检查三路来源"))
        == "/audit 5m CLI三源 持续检查三路来源"
    )
    assert _command_text(_command("/audit help")) == "/audit help"
    assert _command_text(_command("/audit 安全巡检 clear")) == "/audit 安全巡检 clear"


def test_direct_chat_goal_fails_explicitly_instead_of_stopping_current_run(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    agent._current_run_params = SimpleNamespace(request_id="chat-goal")
    execution = ChatControlExecution(
        agent,
        False,
        ChatControlState(True, 0, "普通直连任务", time.perf_counter(), "session-1"),
    )

    result = execute_chat_control(execution, _command("/goal 持续检查"))

    assert result.kind == "goal"
    assert result.ok is False
    assert "Gateway" in result.message

    audit_result = execute_chat_control(
        execution,
        _command("/audit 安全巡检 clear"),
    )
    assert audit_result.kind == "audit"
    assert audit_result.ok is False
    assert "Gateway" in audit_result.message
