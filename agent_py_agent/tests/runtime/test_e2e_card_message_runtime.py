from __future__ import annotations

from agent_py_agent.agent.cards import CardStore, SessionCard, TaskStatus
from agent_py_agent.agent.messages import MessageStore, MessageTarget, MessageTool
from agent_py_agent.agent.runtime import TaskRuntime, WorkerPool


def test_e2e_multi_session_task_runtime_and_large_output(tmp_path):
    cards = CardStore(tmp_path / "cards")
    message_store = MessageStore(tmp_path / "messages", max_inline_chars=64)
    messages = MessageTool(message_store)
    runtime = TaskRuntime(cards, messages)
    pool = WorkerPool(cards, max_task_agent_slots=1)

    cards.save_session(SessionCard(session_id="sess-a", user_id="user-1", channel="chat"))
    cards.save_session(SessionCard(session_id="sess-b", user_id="user-1", channel="chat"))
    short_task = runtime.create_task(goal="short task", user_id="user-1", session_id="sess-a", complexity="short")
    large_task = runtime.create_task(goal="large task", user_id="user-1", session_id="sess-b", complexity="large")
    cards.attach_task_to_session("sess-a", short_task.task_id)
    cards.attach_task_to_session("sess-b", large_task.task_id)

    first_slot = pool.acquire_task_agent_slot("worker-1")
    second_slot = pool.acquire_task_agent_slot("worker-2")

    assert first_slot is not None
    assert second_slot is None
    assert cards.get_session("sess-a").active_task_ids == [short_task.task_id]
    assert cards.get_session("sess-b").active_task_ids == [large_task.task_id]
    assert cards.get_task(short_task.task_id).metadata["worker_tier"] == "weak_subagent"
    assert cards.get_task(large_task.task_id).metadata["worker_tier"] == "task_agent"

    runtime.complete_task(short_task.task_id, artifact_refs=["artifact://short-result"])
    messages.send_message(
        sender=MessageTarget(kind="task", identifier=large_task.task_id),
        target=MessageTarget(kind="session", identifier="sess-b"),
        content="L" * 200,
        task_id=large_task.task_id,
        message_type="progress",
    )

    short_inbox = messages.read_inbox(MessageTarget(kind="session", identifier="sess-a"))
    large_inbox = messages.read_inbox(MessageTarget(kind="session", identifier="sess-b"))

    assert cards.get_task(short_task.task_id).status == TaskStatus.COMPLETED
    assert short_inbox[-1].message_type == "completion"
    assert large_inbox[-1].metadata["content_externalized"] is True
    assert message_store.read_message_body(large_inbox[-1]) == "L" * 200
