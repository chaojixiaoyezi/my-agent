from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlResult,
    parse_conversation_control,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.control_operation_service import (
    GatewayControlOperationConflict,
    GatewayControlOperationReceipt,
    execute_gateway_control_operation,
    gateway_control_operation_status_payload,
    reconcile_gateway_control_operation,
)
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.http_handlers import _can_read_control_operation
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings import AgentConfig


def _command(text: str):
    command = parse_conversation_control(text, reject_unknown_slash=True)
    assert command is not None
    return command


def _scope(
    *,
    message_id: str = "msg-1",
    expected_turn_id: str = "",
    channel_chat_type: str = "",
    channel_chat_id: str = "",
):
    return GatewayControlScope(
        user_id="u-1",
        channel="feishu",
        conversation_id="c-1",
        metadata={
            "message_id": message_id,
            "expected_turn_id": expected_turn_id,
            "channel_chat_type": channel_chat_type,
            "channel_chat_id": channel_chat_id,
        },
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_control_operation_replays_completed_result_without_repeating_effect(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    paths = gateway_paths(agent)
    calls: list[str] = []

    def execute(_agent, _paths, command, _scope):
        calls.append(command.kind)
        return ConversationControlResult("verbose", True, "已开启。")

    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_operation_service."
        "execute_gateway_conversation_control",
        execute,
    )

    first = execute_gateway_control_operation(
        agent,
        paths,
        _command("/verbose on"),
        _scope(),
        command_text="/verbose on",
    )
    replay = execute_gateway_control_operation(
        agent,
        paths,
        _command("/verbose on"),
        _scope(),
        command_text="/verbose on",
    )

    assert calls == ["verbose"]
    assert replay.operation_id == first.operation_id
    assert replay.result == first.result
    assert gateway_control_operation_status_payload(replay)["message"] == "已开启。"


def test_live_control_operation_duplicate_and_status_return_executing_without_blocking(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    paths = gateway_paths(agent)
    entered = threading.Event()
    release = threading.Event()
    completed: list[GatewayControlOperationReceipt] = []
    calls = 0

    def execute(*_args):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=3)
        return ConversationControlResult("compact", True, "done")

    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_operation_service."
        "execute_gateway_conversation_control",
        execute,
    )

    def run_first() -> None:
        completed.append(
            execute_gateway_control_operation(
                agent,
                paths,
                _command("/compact"),
                _scope(),
                command_text="/compact",
            )
        )

    thread = threading.Thread(target=run_first)
    thread.start()
    assert entered.wait(timeout=2)

    started = time.monotonic()
    duplicate = execute_gateway_control_operation(
        agent,
        paths,
        _command("/compact"),
        _scope(),
        command_text="/compact",
    )
    status = reconcile_gateway_control_operation(agent, paths, duplicate.operation_id)
    elapsed = time.monotonic() - started

    assert duplicate.state == "executing"
    assert status is not None and status.state == "executing"
    assert elapsed < 0.5
    assert calls == 1
    release.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert completed[0].state == "completed"


def test_control_operation_same_message_id_different_command_conflicts_before_effect(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    paths = gateway_paths(agent)
    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_operation_service."
        "execute_gateway_conversation_control",
        lambda *_args: ConversationControlResult("verbose", True, "ok"),
    )
    execute_gateway_control_operation(
        agent,
        paths,
        _command("/verbose on"),
        _scope(),
        command_text="/verbose on",
    )

    with pytest.raises(GatewayControlOperationConflict):
        execute_gateway_control_operation(
            agent,
            paths,
            _command("/verbose off"),
            _scope(),
            command_text="/verbose off",
        )


def test_control_operation_same_message_cannot_change_group_owner_facts(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    paths = gateway_paths(agent)
    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_operation_service."
        "execute_gateway_conversation_control",
        lambda *_args: ConversationControlResult("verbose", True, "ok"),
    )
    execute_gateway_control_operation(
        agent,
        paths,
        _command("/verbose on"),
        _scope(channel_chat_type="group", channel_chat_id="group-a"),
        command_text="/verbose on",
    )

    with pytest.raises(GatewayControlOperationConflict):
        execute_gateway_control_operation(
            agent,
            paths,
            _command("/verbose on"),
            _scope(channel_chat_type="group", channel_chat_id="group-b"),
            command_text="/verbose on",
        )


def test_control_operation_status_restores_group_owner_scope(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    paths = gateway_paths(agent)
    seen_scope: list[GatewayControlScope] = []
    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_operation_service."
        "execute_gateway_conversation_control",
        lambda *_args: ConversationControlResult(
            "steer",
            True,
            "waiting",
            request_id="turn-group",
            delivery_status="unknown",
            guidance_dedupe_key="guidance-group",
        ),
    )
    receipt = execute_gateway_control_operation(
        agent,
        paths,
        _command("/btw 继续检查"),
        _scope(
            expected_turn_id="turn-group",
            channel_chat_type="group",
            channel_chat_id="group-a",
        ),
        command_text="/btw 继续检查",
    )

    def reconcile(_agent, _paths, _command, scope):
        seen_scope.append(scope)
        return None

    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_operation_service."
        "reconcile_gateway_steer_delivery",
        reconcile,
    )

    reconcile_gateway_control_operation(agent, paths, receipt.operation_id)

    assert seen_scope[0].metadata["channel_chat_type"] == "group"
    assert seen_scope[0].metadata["channel_chat_id"] == "group-a"


def test_prepared_control_retry_uses_persisted_owner_after_config_change(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_per_user_owner_scoping=True,
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    scope = _scope(
        message_id="msg-owner-freeze",
        channel_chat_type="group",
        channel_chat_id="group-a",
    )
    import agent_py_agent.agent.gateway_parts.control_operation_service as service

    original_write = service.write_json_file_atomic
    writes = 0

    def crash_before_executing(path, payload):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("crash before executing marker")
        original_write(path, payload)

    monkeypatch.setattr(service, "write_json_file_atomic", crash_before_executing)
    with pytest.raises(OSError, match="executing marker"):
        execute_gateway_control_operation(
            agent,
            paths,
            _command("/verbose on"),
            scope,
            command_text="/verbose on",
        )

    agent.config = replace(agent.config, gateway_per_user_owner_scoping=False)
    seen_owner = []

    def execute(_agent, _paths, _command, frozen_scope):
        seen_owner.append(frozen_scope.resolved_owner)
        return ConversationControlResult("verbose", True, "ok")

    monkeypatch.setattr(service, "write_json_file_atomic", original_write)
    monkeypatch.setattr(service, "execute_gateway_conversation_control", execute)
    receipt = execute_gateway_control_operation(
        agent,
        paths,
        _command("/verbose on"),
        scope,
        command_text="/verbose on",
    )

    assert receipt.owner_provider == "feishu"
    assert receipt.owner_kind == "group"
    assert receipt.owner_id == "group-a"
    assert len(seen_owner) == 1
    assert seen_owner[0].provider == "feishu"
    assert seen_owner[0].owner_kind == "group"
    assert seen_owner[0].owner_id == "group-a"


def test_control_receipt_rejects_changed_owner_digest(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    paths = gateway_paths(agent)
    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_operation_service."
        "execute_gateway_conversation_control",
        lambda *_args: ConversationControlResult("verbose", True, "ok"),
    )
    receipt = execute_gateway_control_operation(
        agent,
        paths,
        _command("/verbose on"),
        _scope(message_id="msg-owner-digest"),
        command_text="/verbose on",
    )
    payload = receipt.to_dict()
    payload["owner_ref_digest"] = "0" * 64

    with pytest.raises(DataCorruptionError, match="owner digest"):
        GatewayControlOperationReceipt.from_dict(payload)


def test_executing_control_projection_is_processing_not_terminal_unknown(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    paths = gateway_paths(agent)
    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_operation_service."
        "execute_gateway_conversation_control",
        lambda *_args: ConversationControlResult("verbose", True, "ok"),
    )
    receipt = execute_gateway_control_operation(
        agent,
        paths,
        _command("/verbose on"),
        _scope(message_id="msg-live-projection"),
        command_text="/verbose on",
    )

    payload = gateway_control_operation_status_payload(
        replace(receipt, state="executing", result={}, error={})
    )

    assert payload["ok"] is True
    assert payload["control_state"] == "executing"
    assert payload["delivery_status"] == "unknown"
    assert "error_code" not in payload


def test_control_status_projection_cannot_be_overridden_by_result_fields(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    paths = gateway_paths(agent)
    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_operation_service."
        "execute_gateway_conversation_control",
        lambda *_args: ConversationControlResult("verbose", True, "ok"),
    )
    receipt = execute_gateway_control_operation(
        agent,
        paths,
        _command("/verbose on"),
        _scope(),
        command_text="/verbose on",
    )
    forged = replace(
        receipt,
        result={
            **receipt.result,
            "status": "ordinary_request",
            "disposition": "queued",
            "operation_id": "attacker-controlled",
        },
    )

    payload = gateway_control_operation_status_payload(forged)

    assert payload["status"] == "control"
    assert payload["disposition"] == "system_command"
    assert payload["operation_id"] == receipt.operation_id


def test_control_receipt_state_matrix_rejects_terminal_fields_on_prepared(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    paths = gateway_paths(agent)
    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_operation_service."
        "execute_gateway_conversation_control",
        lambda *_args: ConversationControlResult("verbose", True, "ok"),
    )
    receipt = execute_gateway_control_operation(
        agent,
        paths,
        _command("/verbose on"),
        _scope(),
        command_text="/verbose on",
    )
    payload = receipt.to_dict()
    payload["state"] = "prepared"

    with pytest.raises(DataCorruptionError, match="terminal fields"):
        GatewayControlOperationReceipt.from_dict(payload)


def test_control_status_auth_matches_channel_user_and_group_owner(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    paths = gateway_paths(agent)
    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_operation_service."
        "execute_gateway_conversation_control",
        lambda *_args: ConversationControlResult("verbose", True, "ok"),
    )
    receipt = execute_gateway_control_operation(
        agent,
        paths,
        _command("/verbose on"),
        _scope(channel_chat_type="group", channel_chat_id="group-a"),
        command_text="/verbose on",
    )
    permission = SimpleNamespace(can_access_all_users=False)
    middleware = MagicMock()
    middleware.extract_identity.return_value = ("u-1", "feishu")
    middleware.get_permission.return_value = permission
    headers = {
        "X-Conversation-Id": "c-1",
        "X-Channel-Chat-Type": "group",
        "X-Channel-Chat-Id": "group-a",
    }
    handler = SimpleNamespace(
        _auth_middleware=middleware,
        headers=headers,
        client_address=("127.0.0.1", 1),
    )

    assert _can_read_control_operation(handler, receipt) is True
    middleware.extract_identity.return_value = ("u-1", "qq")
    assert _can_read_control_operation(handler, receipt) is False
    middleware.extract_identity.return_value = ("u-1", "feishu")
    handler.headers = {**headers, "X-Channel-Chat-Id": "group-b"}
    assert _can_read_control_operation(handler, receipt) is False


def test_control_operation_interrupted_after_executing_marker_is_not_repeated(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    paths = gateway_paths(agent)
    calls = 0

    def crash_after_marker(*_args):
        nonlocal calls
        calls += 1
        raise SystemExit("simulated process loss")

    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_operation_service."
        "execute_gateway_conversation_control",
        crash_after_marker,
    )
    with pytest.raises(SystemExit):
        execute_gateway_control_operation(
            agent,
            paths,
            _command("/compact"),
            _scope(),
            command_text="/compact",
        )

    receipt = execute_gateway_control_operation(
        agent,
        paths,
        _command("/compact"),
        _scope(),
        command_text="/compact",
    )

    assert calls == 1
    assert receipt.state == "terminal_unknown"
    assert gateway_control_operation_status_payload(receipt)["delivery_status"] == "unknown"


def test_control_operation_status_only_reconciles_existing_btw_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    paths = gateway_paths(agent)
    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_operation_service."
        "execute_gateway_conversation_control",
        lambda *_args: ConversationControlResult(
            "steer",
            True,
            "等待确认",
            request_id="turn-a",
            delivery_status="unknown",
            guidance_dedupe_key="guidance-key",
        ),
    )
    receipt = execute_gateway_control_operation(
        agent,
        paths,
        _command("/btw 先核对证据"),
        _scope(expected_turn_id="turn-a"),
        command_text="/btw 先核对证据",
    )
    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_operation_service."
        "reconcile_gateway_steer_delivery",
        lambda *_args: ConversationControlResult(
            "steer",
            True,
            "已接收",
            request_id="turn-a",
            delivery_status="accepted",
            guidance_dedupe_key="guidance-key",
        ),
    )

    reconciled = reconcile_gateway_control_operation(agent, paths, receipt.operation_id)

    assert reconciled is not None
    assert reconciled.result["delivery_status"] == "accepted"
    assert reconciled.result["guidance_dedupe_key"] == "guidance-key"


def test_interrupted_btw_without_guidance_rejects_only_after_exact_turn_terminal(
    tmp_path,
    monkeypatch,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    paths = gateway_paths(agent)
    turn_id = "turn-no-guidance"

    def crash_before_guidance(*_args):
        raise RuntimeError("crash before append_guidance_once")

    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_operation_service."
        "execute_gateway_conversation_control",
        crash_before_guidance,
    )
    receipt = execute_gateway_control_operation(
        agent,
        paths,
        _command("/btw 继续核对证据"),
        _scope(message_id="msg-no-guidance", expected_turn_id=turn_id),
        command_text="/btw 继续核对证据",
    )
    assert receipt.state == "terminal_unknown"

    write_json_file_atomic(
        paths.terminal / f"{turn_id}.json",
        {
            "schema": "gateway_terminal_request.v1",
            "id": turn_id,
            "request_id": turn_id,
            "user_id": "u-1",
            "conversation": {
                "channel": "feishu",
                "channel_conversation_id": "c-1",
                "channel_user_id": "u-1",
                "canonical_user_id": "u-1",
            },
        },
    )

    reconciled = reconcile_gateway_control_operation(agent, paths, receipt.operation_id)

    assert reconciled is not None
    assert reconciled.state == "completed"
    assert reconciled.result["delivery_status"] == "rejected"
    assert reconciled.result["request_id"] == turn_id


@pytest.mark.parametrize("lifecycle", ["active", "recoverable", "corrupt", "absent"])
def test_interrupted_btw_without_guidance_keeps_unknown_without_terminal_proof(
    tmp_path,
    monkeypatch,
    lifecycle: str,
) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    paths = gateway_paths(agent)
    turn_id = f"turn-no-guidance-{lifecycle}"
    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_operation_service."
        "execute_gateway_conversation_control",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("crash before guidance")),
    )
    receipt = execute_gateway_control_operation(
        agent,
        paths,
        _command("/btw 继续核对证据"),
        _scope(message_id=f"msg-{lifecycle}", expected_turn_id=turn_id),
        command_text="/btw 继续核对证据",
    )
    request_payload = {
        "id": turn_id,
        "request_id": turn_id,
        "status": "processing" if lifecycle == "active" else "pending",
        "turn_phase": "open",
        "user_id": "u-1",
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
            "canonical_user_id": "u-1",
        },
    }
    if lifecycle == "active":
        write_json_file_atomic(paths.processing / f"{turn_id}.json", request_payload)
    elif lifecycle == "recoverable":
        write_json_file_atomic(paths.inbox / f"{turn_id}.json", request_payload)
    elif lifecycle == "corrupt":
        paths.terminal.mkdir(parents=True, exist_ok=True)
        (paths.terminal / f"{turn_id}.json").write_text("{", encoding="utf-8")

    reconciled = reconcile_gateway_control_operation(agent, paths, receipt.operation_id)

    assert reconciled is not None
    assert reconciled.state == "terminal_unknown"
    assert reconciled.result == {}


def test_http_control_requires_stable_id_and_exposes_pollable_receipt(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    port = _free_port()
    server = GatewayHTTPServer(port, paths, params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        missing_id = urllib.request.Request(
            f"http://127.0.0.1:{port}/control",
            data=json.dumps(
                {
                    "command": "/verbose on",
                    "user_id": "u-1",
                    "channel": "feishu",
                    "conversation_id": "c-1",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(missing_id, timeout=5)
        assert error.value.code == 400

        body = {
            "command": "/verbose on",
            "user_id": "u-1",
            "channel": "feishu",
            "conversation_id": "c-1",
            "metadata": {"message_id": "provider-msg-1"},
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/control",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            submitted = json.loads(response.read().decode("utf-8"))
        replay_request = urllib.request.Request(
            f"http://127.0.0.1:{port}/control",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(replay_request, timeout=5) as response:
            replayed = json.loads(response.read().decode("utf-8"))
        conflicting = dict(body)
        conflicting["command"] = "/verbose off"
        conflict_request = urllib.request.Request(
            f"http://127.0.0.1:{port}/control",
            data=json.dumps(conflicting).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as conflict_error:
            urllib.request.urlopen(conflict_request, timeout=5)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/control-status/{submitted['operation_id']}",
            timeout=5,
        ) as response:
            polled = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()

    assert submitted["control_state"] == "completed"
    assert replayed["operation_id"] == submitted["operation_id"]
    assert conflict_error.value.code == 409
    assert polled["operation_id"] == submitted["operation_id"]
    assert polled["message"] == submitted["message"]
