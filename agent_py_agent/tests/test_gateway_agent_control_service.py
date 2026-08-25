from __future__ import annotations

import time

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.agent_control_service import (
    GatewayAgentControlError,
    read_gateway_agent_view,
    send_gateway_agent_guidance,
    stop_gateway_agent,
)
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent


def _bound_agent_tree(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            gateway_per_user_owner_scoping=False,
        ),
        tmp_path / "workspace",
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "local-agent",
            "channel": "chat",
            "channel_conversation_id": "session-a",
            "channel_user_id": "local-agent",
        }
    )
    root_id = "task-root"
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": root_id,
            "goal": "主任务",
            "status": "active",
            "task_path": str(tmp_path / "workspace" / "task-root"),
            "now": time.time(),
        }
    )
    store.select_workspace_task(
        {
            "thread_id": thread.thread_id,
            "task_id": root_id,
            "now": time.time(),
        }
    )
    child = agent.subagents.create_run(
        goal="完成子任务",
        root_id=root_id,
        parent_id=root_id,
    )
    scope = GatewayControlScope("local-agent", "chat", "session-a")
    return agent, scope, child


def test_owner_can_view_steer_stop_and_reopen_terminal_child(tmp_path) -> None:
    agent, scope, child = _bound_agent_tree(tmp_path)

    view = read_gateway_agent_view(agent, scope=scope, run_id=child.id)
    assert view["ok"] is True
    assert view["agent"]["run_id"] == child.id
    assert view["terminal"] is False

    accepted = send_gateway_agent_guidance(
        agent,
        scope=scope,
        run_id=child.id,
        message="请先运行完整测试",
        message_id="agent-steer-1",
    )
    replay = send_gateway_agent_guidance(
        agent,
        scope=scope,
        run_id=child.id,
        message="请先运行完整测试",
        message_id="agent-steer-1",
    )
    assert accepted["guidance_id"] == replay["guidance_id"]
    entries = agent.conversation_store.recent_guidance("agent_run", child.id)
    assert len(entries) == 1
    entry = entries[0]
    expected_turn_id = str(entry.metadata["expected_turn_id"])
    assert expected_turn_id.startswith("attempt-")
    assert agent.conversation_store.claim_guidance_once_for_turn(
        entry,
        expected_turn_id=expected_turn_id,
        attempt_id=expected_turn_id,
    ) is True
    assert agent.conversation_store.mark_guidance_entries_submitted(
        expected_turn_id,
        [entry],
        attempt_id=expected_turn_id,
    ) == (entry.guidance_id,)

    stopped = stop_gateway_agent(
        agent,
        scope=scope,
        run_id=child.id,
        operation_id="agent-stop-1",
    )
    assert stopped["status"] == "cancelled"
    terminal_view = read_gateway_agent_view(agent, scope=scope, run_id=child.id)
    assert terminal_view["terminal"] is True
    replayed_stop = stop_gateway_agent(
        agent,
        scope=scope,
        run_id=child.id,
        operation_id="agent-stop-1",
    )
    assert replayed_stop["status"] == "already_terminal"
    with pytest.raises(GatewayAgentControlError) as exc_info:
        send_gateway_agent_guidance(
            agent,
            scope=scope,
            run_id=child.id,
            message="继续",
            message_id="agent-steer-2",
        )
    assert exc_info.value.status == 409
    assert exc_info.value.error_code == "AGENT_ALREADY_TERMINAL"


def test_agent_guidance_rejects_nonterminal_task_without_active_attempt(tmp_path) -> None:
    agent, scope, child = _bound_agent_tree(tmp_path)
    repo = agent.subagents.runtime_db
    agent_run = repo.agent_run_for_run_id(child.id)
    assert agent_run is not None
    attempt = repo.create_attempt(
        str(agent_run["agent_run_id"]),
        reuse_pending=True,
        reject_running=True,
    )
    settled = repo.settle_agent_attempt(
        agent_run_id=str(agent_run["agent_run_id"]),
        attempt_id=str(attempt["attempt_id"]),
    )
    assert settled["settled"] is True

    with pytest.raises(GatewayAgentControlError) as exc_info:
        send_gateway_agent_guidance(
            agent,
            scope=scope,
            run_id=child.id,
            message="继续检查",
            message_id="agent-steer-no-turn",
        )

    assert exc_info.value.error_code == "AGENT_NOT_RUNNING"
    assert agent.conversation_store.recent_guidance("agent_run", child.id) == []


def test_agent_control_rejects_run_outside_conversation_root(tmp_path) -> None:
    agent, scope, _child = _bound_agent_tree(tmp_path)
    outside = agent.subagents.create_run(
        goal="其它任务",
        root_id="task-other",
        parent_id="task-other",
    )

    with pytest.raises(GatewayAgentControlError) as exc_info:
        read_gateway_agent_view(agent, scope=scope, run_id=outside.id)
    assert exc_info.value.status == 403
    assert exc_info.value.error_code == "AGENT_OUTSIDE_CONVERSATION"


def test_thin_client_agent_requests_keep_local_conversation_identity() -> None:
    client = object.__new__(GatewayChatClientAgent)
    calls: list[tuple[str, dict[str, object], float]] = []

    def post(path: str, payload: dict[str, object], *, timeout: float):
        calls.append((path, payload, timeout))
        return 200, {"ok": True}

    client._post_gateway_json = post
    client.request_agent_view("session-a", run_id="child-a")
    client.request_agent_guidance(
        "session-a",
        run_id="child-a",
        message="继续",
        message_id="agent-steer-1",
    )
    client.request_agent_stop(
        "session-a",
        run_id="child-a",
        operation_id="agent-stop-1",
    )

    assert [path for path, _payload, _timeout in calls] == [
        "/client/agent-view",
        "/client/agent-guidance",
        "/client/agent-stop",
    ]
    assert all(payload["user_id"] == "local-agent" for _path, payload, _ in calls)
    assert all(payload["channel"] == "chat" for _path, payload, _ in calls)
    assert all(payload["conversation_id"] == "session-a" for _path, payload, _ in calls)
