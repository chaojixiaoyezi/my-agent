from __future__ import annotations

import pytest

from agent_py_agent.agent.cards import (
    CardStore,
    CheckpointCard,
    SessionCard,
    TaskStatus,
    new_card_id,
)
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


# LLM: card runtime must model foreground chat plus multiple task agents and a weak subagent.
# 函数用途: 覆盖用户会话不断线、多个 main/task-agent worker 干活、subagent 干短活以及约定卡片落盘。
def test_e2e_foreground_session_multiple_task_agents_and_subagent_cards(tmp_path):
    cards = CardStore(tmp_path / "cards")
    message_store = MessageStore(tmp_path / "messages", max_inline_chars=96)
    messages = MessageTool(message_store)
    runtime = TaskRuntime(cards, messages)
    pool = WorkerPool(cards, max_task_agent_slots=2)

    cards.save_session(SessionCard(session_id="sess-admin", user_id="admin-1", channel="terminal"))
    report_task = runtime.create_task(
        goal="复杂报告任务",
        user_id="admin-1",
        session_id="sess-admin",
        complexity="complex",
        progress_interval_seconds=60,
        acceptance=["xlsx exists", "recommendations are Chinese"],
    )
    site_task = runtime.create_task(
        goal="购物网站任务",
        user_id="admin-1",
        session_id="sess-admin",
        complexity="large",
        progress_interval_seconds=120,
        acceptance=["register flow works", "cart flow works"],
    )
    small_task = runtime.create_task(
        goal="短检查任务",
        user_id="admin-1",
        session_id="sess-admin",
        complexity="short",
        acceptance=["summary message sent"],
    )
    for task in (report_task, site_task, small_task):
        cards.attach_task_to_session("sess-admin", task.task_id)

    report_slot = pool.acquire_task_agent_slot("main-agent-worker-1", task_id=report_task.task_id)
    site_slot = pool.acquire_task_agent_slot("main-agent-worker-2", task_id=site_task.task_id)
    extra_slot = pool.acquire_task_agent_slot("main-agent-worker-3", task_id=small_task.task_id)
    subagent_run = cards.create_worker_run(
        task_id=small_task.task_id,
        worker_id="weak-subagent-1",
        worker_type="weak_subagent",
        metadata={"tier": "weak_subagent"},
    )
    cards.save_checkpoint(
        CheckpointCard(
            checkpoint_id=new_card_id("checkpoint"),
            task_id=report_task.task_id,
            step="research_started",
            payload={"processed_batches": 1},
        )
    )
    messages.send_message(
        sender=MessageTarget(kind="session", identifier="sess-admin"),
        target=MessageTarget(kind="task", identifier=report_task.task_id),
        content="用户补充：报告里要有中文推荐理由。",
        task_id=report_task.task_id,
        message_type="user_followup",
    )

    assert report_slot is not None
    assert site_slot is not None
    assert extra_slot is None
    assert cards.get_task(report_task.task_id).metadata["worker_tier"] == "task_agent"
    assert cards.get_task(site_task.task_id).metadata["worker_tier"] == "task_agent"
    assert cards.get_task(small_task.task_id).metadata["worker_tier"] == "weak_subagent"
    assert cards.get_progress_policy(report_task.task_id).interval_seconds == 60
    assert cards.get_notification_route(site_task.task_id).target == "session:sess-admin"
    assert cards.latest_checkpoint(report_task.task_id).payload["processed_batches"] == 1
    assert {run.worker_type for run in cards.list_worker_runs()} == {"task_agent", "weak_subagent"}
    assert subagent_run.worker_id == "weak-subagent-1"
    assert len(cards.get_session("sess-admin").active_task_ids) == 3
    assert messages.read_inbox(MessageTarget(kind="task", identifier=report_task.task_id))[-1].message_type == "user_followup"


def test_sessionized_subagent_spawn_and_steer_uses_message_contract(tmp_path):
    cards = CardStore(tmp_path / "cards")
    messages = MessageTool(MessageStore(tmp_path / "messages"))
    runtime = TaskRuntime(cards, messages)
    cards.save_session(SessionCard(session_id="sess-parent", user_id="user-1", channel="terminal"))
    parent_task = runtime.create_task(
        goal="coordinate research",
        user_id="user-1",
        session_id="sess-parent",
        complexity="complex",
    )

    child = runtime.spawn_subagent_session(
        requester_session_id="sess-parent",
        parent_task_id=parent_task.task_id,
        goal="collect source data",
        label="researcher",
        context_mode="fork",
        mode="session",
    )
    runtime.send_to_subagent(
        run_id=child.run_id,
        message="focus on verified URLs only",
        message_type="steer",
    )

    child_session = cards.get_session(child.child_session_id)
    child_task = cards.get_task(child.task_id)
    child_inbox = messages.read_inbox(MessageTarget(kind="session", identifier=child.child_session_id))

    assert child_session.user_id == "user-1"
    assert child_session.metadata["parent_session_id"] == "sess-parent"
    assert child_task.parent_task_id == parent_task.task_id
    assert cards.get_task(parent_task.task_id).child_task_ids == [child.task_id]
    assert cards.get_subagent_run(child.run_id).context_mode == "fork"
    assert [message.message_type for message in child_inbox] == ["task_assignment", "steer"]
    assert child_inbox[-1].metadata["run_id"] == child.run_id


def test_subagent_completion_requires_final_delivery_to_parent_session(tmp_path):
    cards = CardStore(tmp_path / "cards")
    messages = MessageTool(MessageStore(tmp_path / "messages"))
    runtime = TaskRuntime(cards, messages)
    cards.save_session(SessionCard(session_id="sess-parent", user_id="user-1", channel="terminal"))
    parent_task = runtime.create_task(
        goal="coordinate artifacts",
        user_id="user-1",
        session_id="sess-parent",
        complexity="complex",
    )
    child = runtime.spawn_subagent_session(
        requester_session_id="sess-parent",
        parent_task_id=parent_task.task_id,
        goal="produce artifact",
        label="writer",
    )

    delivered = runtime.complete_subagent_run(
        child.run_id,
        outcome="ok",
        artifact_refs=["artifact://child-result"],
        summary="child result ready",
    )

    parent_inbox = messages.read_inbox(MessageTarget(kind="session", identifier="sess-parent"))
    assert delivered.pending_final_delivery is False
    assert cards.list_pending_final_delivery() == []
    assert parent_inbox[-1].message_type == "subagent_completion"
    assert parent_inbox[-1].metadata["run_id"] == child.run_id
    assert parent_inbox[-1].metadata["artifact_refs"] == ["artifact://child-result"]


def test_subagent_spawn_depth_is_enforced_by_card_metadata(tmp_path):
    cards = CardStore(tmp_path / "cards")
    messages = MessageTool(MessageStore(tmp_path / "messages"))
    runtime = TaskRuntime(cards, messages)
    cards.save_session(SessionCard(session_id="sess-parent", user_id="user-1", channel="terminal"))
    parent_task = runtime.create_task(
        goal="root",
        user_id="user-1",
        session_id="sess-parent",
        complexity="complex",
    )
    child = runtime.spawn_subagent_session(
        requester_session_id="sess-parent",
        parent_task_id=parent_task.task_id,
        goal="level one",
        max_spawn_depth=1,
    )

    child_session = cards.get_session(child.child_session_id)
    assert child_session.metadata["spawn_depth"] == 1
    assert child_session.metadata["subagent_role"] == "leaf"
    assert child_session.metadata["subagent_control_scope"] == "none"

    with pytest.raises(ValueError, match="subagent spawn depth exceeded"):
        runtime.spawn_subagent_session(
            requester_session_id=child.child_session_id,
            parent_task_id=child.task_id,
            goal="level two",
            max_spawn_depth=1,
        )


def test_parent_can_kill_sessionized_subagent_by_run_id(tmp_path):
    cards = CardStore(tmp_path / "cards")
    messages = MessageTool(MessageStore(tmp_path / "messages"))
    runtime = TaskRuntime(cards, messages)
    cards.save_session(SessionCard(session_id="sess-parent", user_id="user-1", channel="terminal"))
    parent_task = runtime.create_task(
        goal="coordinate",
        user_id="user-1",
        session_id="sess-parent",
        complexity="complex",
    )
    child = runtime.spawn_subagent_session(
        requester_session_id="sess-parent",
        parent_task_id=parent_task.task_id,
        goal="risky branch",
    )

    killed = runtime.kill_subagent_run(child.run_id, reason="parent redirected task")

    child_inbox = messages.read_inbox(MessageTarget(kind="session", identifier=child.child_session_id))
    assert killed.status == "killed"
    assert killed.outcome == "killed"
    assert child_inbox[-1].message_type == "kill"
    assert child_inbox[-1].metadata["run_id"] == child.run_id
