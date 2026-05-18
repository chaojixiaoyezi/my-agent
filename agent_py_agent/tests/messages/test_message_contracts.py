from __future__ import annotations

from agent_py_agent.agent.messages import MessageStore, MessageTarget, MessageTool


def test_send_message_places_record_in_target_inbox(tmp_path):
    tool = MessageTool(MessageStore(tmp_path))

    message = tool.send_message(
        sender=MessageTarget(kind="session", identifier="sess-a"),
        target=MessageTarget(kind="session", identifier="sess-b"),
        content="hello",
        task_id="task-1",
    )

    inbox = tool.read_inbox(MessageTarget(kind="session", identifier="sess-b"))
    assert [item.message_id for item in inbox] == [message.message_id]
    assert inbox[0].content == "hello"
    assert inbox[0].task_id == "task-1"


def test_delivery_idempotency_prevents_duplicate_inbox_rows(tmp_path):
    tool = MessageTool(MessageStore(tmp_path))
    target = MessageTarget(kind="session", identifier="sess-b")

    first = tool.send_message(
        sender=MessageTarget(kind="task", identifier="task-1"),
        target=target,
        content="done",
        idempotency_key="task-1:complete:route-1",
    )
    second = tool.send_message(
        sender=MessageTarget(kind="task", identifier="task-1"),
        target=target,
        content="done again",
        idempotency_key="task-1:complete:route-1",
    )

    assert second.message_id == first.message_id
    inbox = tool.read_inbox(target)
    assert len(inbox) == 1
    assert inbox[0].content == "done"


def test_ack_message_marks_delivery_read(tmp_path):
    tool = MessageTool(MessageStore(tmp_path))
    target = MessageTarget(kind="user", identifier="user-1")
    message = tool.send_message(
        sender=MessageTarget(kind="system", identifier="runtime"),
        target=target,
        content="stored result",
    )

    assert tool.ack_message(target, message.message_id) is True
    assert tool.read_inbox(target, unread_only=True) == []
    assert [item.message_id for item in tool.read_inbox(target, unread_only=False)] == [message.message_id]


def test_progress_and_need_user_input_use_structured_targets(tmp_path):
    tool = MessageTool(MessageStore(tmp_path))

    progress = tool.broadcast_progress(
        task_id="task-1",
        route_target=MessageTarget(kind="session", identifier="sess-1"),
        content="phase 1 complete",
    )
    question = tool.send_need_user_input(
        task_id="task-1",
        session_id="sess-1",
        prompt="Which branch should I use?",
    )

    inbox = tool.read_inbox(MessageTarget(kind="session", identifier="sess-1"))
    assert [item.message_type for item in inbox] == ["progress", "need_user_input"]
    assert progress.metadata["task_id"] == "task-1"
    assert question.metadata["requires_frontstage_agent"] is True


def test_large_message_content_is_externalized(tmp_path):
    store = MessageStore(tmp_path, max_inline_chars=32)
    tool = MessageTool(store)
    long_content = "x" * 120

    message = tool.send_message(
        sender=MessageTarget(kind="task", identifier="task-1"),
        target=MessageTarget(kind="session", identifier="sess-1"),
        content=long_content,
    )

    assert message.content == "x" * 32
    assert message.metadata["content_externalized"] is True
    assert message.metadata["content_chars"] == 120
    assert store.read_message_body(message) == long_content
