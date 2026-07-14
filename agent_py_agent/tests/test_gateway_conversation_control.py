from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request

from agent_py_agent.agent.concurrency.interrupt import (
    is_interrupted,
    register_interruptible,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    execute_gateway_conversation_control,
)
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.io import write_json_file
from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path, gateway_paths
from agent_py_agent.agent.gateway_parts.request_execution import _handle_gateway_request
from agent_py_agent.agent.settings import AgentConfig


def _request(request_id: str, *, user: str = "u-1", conversation_id: str = "c-1") -> dict:
    return {
        "id": request_id,
        "request_id": request_id,
        "kind": "ask",
        "goal": "整理今天的资料",
        "created_at": time.time() - 30,
        "lease_started_at": time.time() - 20,
        "status": "processing",
        "user_id": user,
        "metadata": {"user_id": user, "channel": "feishu"},
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": conversation_id,
            "channel_user_id": user,
            "canonical_user_id": user,
        },
    }


def _scope(user: str = "u-1", conversation_id: str = "c-1") -> GatewayControlScope:
    return GatewayControlScope(user, "feishu", conversation_id)


def _command(text: str):
    command = parse_conversation_control(text)
    assert command is not None
    return command


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_btw_targets_only_current_request(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    write_json_file(paths.processing / "req-2.json", _request("req-2", user="u-2"))

    result = execute_gateway_conversation_control(agent, paths, _command("/btw 先核对来源"), _scope())

    assert result.ok is True
    assert result.request_id == "req-1"
    assert agent.conversation_store.pending_guidance("request", "req-1")[0].message == "先核对来源"
    assert agent.conversation_store.pending_guidance("request", "req-2") == []


def test_status_uses_typed_facts_without_guidance_history(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            model_name="MiniMax-M2.7",
            gateway_per_user_owner_scoping=False,
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    chunk_path = gateway_chunk_path(paths, "req-1")
    chunk_path.write_text(
        '{"kind":"tool_progress","progress":{"tool":"run_command","status":"完成","detail":"secret"}}\n',
        encoding="utf-8",
    )
    agent.conversation_store.get_or_create_thread(
        {
            "canonical_user_id": "u-1",
            "channel": "feishu",
            "channel_conversation_id": "c-1",
            "channel_user_id": "u-1",
        }
    )

    result = execute_gateway_conversation_control(agent, paths, _command("/status"), _scope())

    assert result.ok is True
    assert "状态：运行中" in result.message
    assert "最近进展：刚完成一个执行步骤" in result.message
    assert "MiniMax-M2.7" in result.message
    assert "run_command" not in result.message
    assert "secret" not in result.message
    assert "引导" not in result.message


def test_stop_persists_and_signals_only_matching_request(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "req-1.json"
    write_json_file(request_path, _request("req-1"))
    ready = threading.Event()
    observed = threading.Event()

    def worker() -> None:
        with register_interruptible("conversation-request:req-1"):
            ready.set()
            while not is_interrupted():
                time.sleep(0.01)
            observed.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(timeout=2)

    result = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())
    thread.join(timeout=2)

    assert result.ok is True
    assert observed.is_set()
    payload = request_path.read_text(encoding="utf-8")
    assert '"cancel_requested": true' in payload
    assert '"control_status": "stopping"' in payload


def test_status_and_stop_follow_typed_request_lineage_to_subagents(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    child = agent.subagents.create_run(
        goal="整理子目录",
        thought="先检查",
        plan=["读取", "整理"],
        attributes={CONVERSATION_REQUEST_ID_ATTR: "req-1"},
    )

    status = execute_gateway_conversation_control(agent, paths, _command("/status"), _scope())
    stopped = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and agent.subagents.load(child.id).status != "CANCELLED":
        time.sleep(0.01)

    assert status.status is not None
    assert status.status.subagent_total == 1
    assert status.status.subagent_running == 1
    assert stopped.ok is True
    assert agent.subagents.load(child.id).status == "CANCELLED"


def test_stop_does_not_recreate_request_that_finished_during_control(tmp_path, monkeypatch) -> None:
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic as real_update

    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "req-finished.json"
    write_json_file(request_path, _request("req-finished"))

    def finish_then_update(path, updater, **kwargs):
        path.unlink()
        return real_update(path, updater, **kwargs)

    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.control_service.update_json_file_atomic",
        finish_then_update,
    )

    result = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope())

    assert result.ok is False
    assert "刚刚结束" in result.message
    assert not request_path.exists()


def test_cancelled_request_finishes_without_model_execution(tmp_path, monkeypatch) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_path = paths.processing / "req-cancelled.json"
    payload = _request("req-cancelled")
    payload["cancel_requested"] = True
    write_json_file(request_path, payload)
    monkeypatch.setattr(agent, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")))

    response = _handle_gateway_request(agent, request_path)

    assert response["ok"] is True
    assert response["status"] == "cancelled"
    assert response["error_code"] == "CANCELLED"
    assert response["response"] == "当前任务已停止。"


def test_control_fails_closed_for_other_user(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-2.json", _request("req-2", user="u-2"))

    result = execute_gateway_conversation_control(agent, paths, _command("/stop"), _scope(user="u-1"))

    assert result.ok is False
    assert result.request_id == ""


def test_http_control_endpoint_returns_immediate_conversation_status(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False),
        tmp_path,
    )
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.processing / "req-1.json", _request("req-1"))
    port = _free_port()
    server = GatewayHTTPServer(
        port,
        paths,
        params=GatewayHTTPServerParams(agent=agent),
    )
    server.start()
    try:
        body = json.dumps(
            {
                "command": "/status",
                "user_id": "u-1",
                "channel": "feishu",
                "conversation_id": "c-1",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/control",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()

    assert payload["ok"] is True
    assert payload["kind"] == "status"
    assert payload["request_id"] == "req-1"
    assert "状态：运行中" in payload["message"]
