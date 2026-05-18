from __future__ import annotations

from agent_py_agent.agent.adapter.protocol import IncomingMessage
from agent_py_agent.agent.cards import TaskStatus
from agent_py_agent.agent.messages import MessageTarget
from agent_py_agent.agent.notification.models import Notification
from agent_py_agent.agent.runtime.bridges import (
    gateway_response_to_message,
    incoming_to_message,
    notification_to_message,
    task_registry_record_to_card,
)


def test_incoming_adapter_message_maps_to_internal_message(tmp_path):
    incoming = IncomingMessage(
        channel="feishu",
        user_id="user-1",
        content="start task",
        message_id="m-1",
        metadata={"chat_id": "chat-a", "session_id": "sess-1"},
    )

    message = incoming_to_message(incoming, messages_root=tmp_path)

    assert message.sender == "channel:feishu:user-1"
    assert message.target == "session:sess-1"
    assert message.content == "start task"
    assert message.metadata["external_message_id"] == "m-1"


def test_notification_maps_to_completion_message_with_idempotency(tmp_path):
    notification = Notification(
        notification_id="notif-1",
        task_id="task-1",
        user_id="user-1",
        session_id="sess-1",
        channel="chat",
        message="task done",
    )

    first = notification_to_message(notification, messages_root=tmp_path)
    second = notification_to_message(notification, messages_root=tmp_path)

    assert first.message_id == second.message_id
    assert first.target == "session:sess-1"
    assert first.message_type == "notification"
    assert first.metadata["notification_id"] == "notif-1"


def test_gateway_response_maps_to_task_message(tmp_path):
    response = {
        "id": "gw-1",
        "ok": True,
        "status": "done",
        "response": "finished",
        "metadata": {"task_id": "task-1", "session_id": "sess-1"},
    }

    message = gateway_response_to_message(response, messages_root=tmp_path)

    assert message.target == "session:sess-1"
    assert message.task_id == "task-1"
    assert message.message_type == "gateway_result"
    assert message.content == "finished"


def test_task_registry_record_maps_to_task_card():
    card = task_registry_record_to_card(
        {
            "task_id": "task-1",
            "session_id": "sess-1",
            "user_id": "user-1",
            "status": "running",
            "goal": "build runtime",
            "created_at": 1.0,
            "updated_at": 2.0,
        }
    )

    assert card.task_id == "task-1"
    assert card.status == TaskStatus.RUNNING
    assert card.goal == "build runtime"
    assert card.metadata["source"] == "task_registry"


def test_message_target_parse_round_trip():
    target = MessageTarget.parse("session:sess-1")
    assert target.kind == "session"
    assert target.identifier == "sess-1"
    assert target.key == "session:sess-1"
