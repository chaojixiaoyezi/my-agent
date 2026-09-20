from __future__ import annotations

import json
import threading
import time
import urllib.error
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
    request_gateway_control_status,
)
from agent_py_agent.cli.chat_parts.slash_command_types import SlashCommandContext
from agent_py_agent.cli.chat_parts.slash_commands import handle_common_slash_command


def test_slash_sessions_lists_only_current_owner_and_exact_resume_command(tmp_path) -> None:
    from agent_py_agent.agent.session.manager import SessionManager

    config = SimpleNamespace(
        session_workspace=str(tmp_path / "sessions"),
        user_id="owner-a",
    )
    manager = SessionManager(config)
    older = manager.create_session(user_id="owner-a", channel="chat")
    current = manager.create_session(user_id="owner-a", channel="tui")
    manager.create_session(user_id="owner-b", channel="chat")
    older.updated_at = 100.0
    current.updated_at = 200.0
    manager.save_session(older)
    manager.save_session(current)
    printed: list[str] = []

    handled = handle_common_slash_command(
        "/sessions",
        ctx=SlashCommandContext(
            agent=SimpleNamespace(config=config),
            memory_limit=5,
            runtime_inject=[],
            prompt_files=[],
            print_line=printed.append,
            conversation_id=current.session_id,
        ),
    )

    assert handled is True
    rendered = "\n".join(printed)
    assert current.session_id in rendered
    assert f"{current.session_id}（当前）" in rendered
    assert older.session_id in rendered
    assert "owner-b" not in rendered
    assert "my-agent resume <session_id>" in rendered


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


def test_slash_remember_does_not_call_unknown_delivery_a_failure() -> None:
    output: list[str] = []
    agent = SimpleNamespace(
        gateway_client_only=True,
        request_memory=lambda **_kwargs: {
            "ok": False,
            "outcome": "unknown",
            "error_code": "MEMORY_WRITE_RESULT_UNKNOWN",
        },
    )

    handled = handle_common_slash_command(
        "/remember 已确认内容",
        ctx=SlashCommandContext(
            agent=agent,
            memory_limit=5,
            runtime_inject=[],
            prompt_files=[],
            print_line=output.append,
            conversation_id="session-1",
        ),
    )

    assert handled is True
    assert output == [
        "记忆保存结果暂时无法确认；Gateway 可能已经写入，系统没有自动重复保存。"
        "可用 /memory <关键词> 查询。"
    ]


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
    pending = agent.conversation_store.guidance.pending("request", "chat-1")
    assert pending[0].message == "改为先写摘要"
    assert agent.conversation_store.guidance.pending("request", "chat-2") == []


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
    agent.conversation_store.guidance.append(
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
    assert agent.conversation_store.guidance.pending("request", "chat-stop") == []


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


def test_gateway_btw_carries_stable_identity_and_exact_turn(monkeypatch) -> None:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = (
        b'{"ok": true, "kind": "steer", "message": "pending", '
        b'"request_id": "turn-a", "operation_id": "gwctl-msg-abc", '
        b'"guidance_dedupe_key": "guidance-key", "delivery_status": "unknown"}'
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
        ChatControlState(
            True,
            0,
            "长任务",
            time.perf_counter(),
            "session-steer",
            request_id="turn-a",
        ),
    )

    result = execute_chat_control(execution, _command("/btw 请先核对证据"))

    request = urlopen.call_args.args[0]
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["metadata"]["expected_turn_id"] == "turn-a"
    assert str(payload["metadata"]["message_id"]).startswith("control-")
    assert result.delivery_status == "unknown"
    assert result.operation_id == "gwctl-msg-abc"
    assert result.guidance_dedupe_key == "guidance-key"


def test_scoped_gateway_tui_control_and_receipt_keep_owner_identity(
    monkeypatch,
    tmp_path,
) -> None:
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent

    captured: list[object] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "ok": True,
                    "kind": "stop",
                    "message": "stopped",
                    "operation_id": "gwctl-owner",
                    "control_state": "completed",
                }
            ).encode()

    def fake_urlopen(request, timeout):
        captured.append((request, timeout))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = GatewayChatClientAgent(
        SimpleNamespace(),
        SimpleNamespace(
            gateway_port=18420,
            gateway_service_command_timeout_seconds=4,
        ),
        tmp_path,
        [tmp_path],
        SimpleNamespace(),
        owner_identity=OwnerIdentity.provider_user("tui-test", "alice"),
    )
    execution = ChatControlExecution(
        client,
        True,
        ChatControlState(False, 0, "", 0.0, "same-session"),
    )

    submitted = execute_chat_control(
        execution,
        _command("/stop"),
        message_id="control-owner",
        target_control_message_id="compact-control-owner",
    )
    reconciled = request_gateway_control_status(execution, "gwctl-owner")

    post_request, post_timeout = captured[0]
    get_request, get_timeout = captured[1]
    post_headers = {str(key).casefold(): value for key, value in post_request.header_items()}
    get_headers = {str(key).casefold(): value for key, value in get_request.header_items()}
    payload = json.loads(post_request.data.decode("utf-8"))
    assert payload["user_id"] == "alice"
    assert payload["channel"] == "tui-test"
    assert payload["conversation_id"] == "same-session"
    assert payload["metadata"]["message_id"] == "control-owner"
    assert payload["metadata"]["target_control_message_id"] == "compact-control-owner"
    assert post_headers["x-user-id"] == get_headers["x-user-id"] == "alice"
    assert post_headers["x-channel"] == get_headers["x-channel"] == "tui-test"
    assert get_headers["x-conversation-id"] == "same-session"
    assert post_timeout == 10.0
    assert get_timeout == 2.0
    assert submitted.ok is True
    assert reconciled.operation_id == "gwctl-owner"


def test_gateway_control_transport_retry_reuses_one_message_id(monkeypatch) -> None:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = (
        b'{"ok": true, "kind": "compact", "message": "done", '
        b'"operation_id": "gwctl-stable", "control_state": "completed"}'
    )
    urlopen = MagicMock(
        side_effect=[urllib.error.URLError("response lost"), response]
    )
    monkeypatch.setattr(
        "agent_py_agent.cli.chat_parts.control_runtime.urllib.request.urlopen",
        urlopen,
    )
    execution = ChatControlExecution(
        SimpleNamespace(
            config=SimpleNamespace(
                gateway_port=8420,
                gateway_service_command_timeout_seconds=2,
                request_timeout=2,
            )
        ),
        True,
        ChatControlState(False, 0, "", 0.0, "session-retry"),
    )

    result = execute_chat_control(
        execution,
        _command("/compact 保留未完成事项"),
        message_id="control-stable",
    )

    payloads = [
        json.loads(call.args[0].data.decode("utf-8"))
        for call in urlopen.call_args_list
    ]
    assert [item["metadata"]["message_id"] for item in payloads] == [
        "control-stable",
        "control-stable",
    ]
    assert result.operation_id == "gwctl-stable"
    assert result.control_state == "completed"


def test_gateway_control_status_is_read_only_and_preserves_delivery(monkeypatch) -> None:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = (
        b'{"ok": true, "kind": "steer", "message": "accepted", '
        b'"request_id": "turn-a", "operation_id": "gwctl-one", '
        b'"control_state": "completed", "delivery_status": "accepted"}'
    )
    urlopen = MagicMock(return_value=response)
    monkeypatch.setattr(
        "agent_py_agent.cli.chat_parts.control_runtime.urllib.request.urlopen",
        urlopen,
    )
    execution = ChatControlExecution(
        SimpleNamespace(config=SimpleNamespace(gateway_port=8420)),
        True,
        ChatControlState(False, 0, "", 0.0, "session-status"),
    )

    result = request_gateway_control_status(execution, "gwctl-one")

    request = urlopen.call_args.args[0]
    assert request.full_url.endswith("/control-status/gwctl-one")
    assert request.data is None
    assert result.operation_id == "gwctl-one"
    assert result.request_id == "turn-a"
    assert result.delivery_status == "accepted"
    assert result.control_state == "completed"


def test_gateway_compact_result_restores_typed_generation_without_parsing_message(
    monkeypatch,
) -> None:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = (
        b'{"ok": true, "kind": "compact", "message": "localized display only", '
        b'"operation_id": "gwctl-compact", "control_state": "completed", '
        b'"task_status": {"state": "idle", "compact_generation": 4}}'
    )
    monkeypatch.setattr(
        "agent_py_agent.cli.chat_parts.control_runtime.urllib.request.urlopen",
        MagicMock(return_value=response),
    )
    execution = ChatControlExecution(
        SimpleNamespace(config=SimpleNamespace(gateway_port=8420)),
        True,
        ChatControlState(False, 0, "", 0.0, "session-compact-status"),
    )

    result = request_gateway_control_status(execution, "gwctl-compact")

    assert result.status is not None
    assert result.status.compact_generation == 4
    assert result.message == "localized display only"


def test_gateway_manual_compact_timeout_covers_one_provider_call(monkeypatch) -> None:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = (
        b'{"ok": true, "message": "compacted", "request_id": ""}'
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
                request_timeout=240,
            )
        ),
        True,
        ChatControlState(False, 0, "", 0.0, "session-compact"),
    )

    result = execute_chat_control(execution, _command("/compact 保留未完成事项"))

    assert result.ok is True
    assert urlopen.call_args.kwargs["timeout"] == 270.0


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
    assert _command_text(_command("/context")) == "/context"
    assert _command_text(_command("/compact 保留决策")) == "/compact 保留决策"
    assert _command_text(_command("/effort high")) == "/effort high"
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


def test_gateway_goal_control_transmits_scoped_client_workspace(tmp_path) -> None:
    workspace = {"cwd": str(tmp_path), "roots": [str(tmp_path)]}
    captured = []
    agent = SimpleNamespace(
        config=SimpleNamespace(gateway_port=18420),
        gateway_request_workspace=lambda: workspace,
        post_gateway_json=lambda _path, payload, **_kwargs: (
            captured.append(payload) or 200,
            {"kind": "goal", "ok": True, "message": "created"},
        ),
    )
    execution = ChatControlExecution(agent, True, ChatControlState(False, 0, "", 0.0, "fresh"))
    assert execute_chat_control(execution, _command("/goal 记录当前目录")).ok
    assert captured[0]["workspace"] == workspace
    agent.gateway_request_workspace = lambda: {}
    assert execute_chat_control(execution, _command("/goal 记录当前目录")).ok
    assert "workspace" not in captured[1]
