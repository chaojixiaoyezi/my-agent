from __future__ import annotations

import time

from agent_py_agent.agent.cards import CardStore, TaskStatus
from agent_py_agent.agent.messages import MessageStore, MessageTarget, MessageTool
from agent_py_agent.agent.runtime import TaskRuntime, WorkerPool


def test_worker_pool_limits_active_slots(tmp_path):
    cards = CardStore(tmp_path / "cards")
    pool = WorkerPool(cards, max_task_agent_slots=2)

    first = pool.acquire_task_agent_slot("worker-1")
    second = pool.acquire_task_agent_slot("worker-2")
    third = pool.acquire_task_agent_slot("worker-3")

    assert first is not None
    assert second is not None
    assert third is None

    pool.release_slot(first.lease_id)
    assert pool.acquire_task_agent_slot("worker-3") is not None


def test_runtime_creates_task_with_route_policy_and_completion_message(tmp_path):
    cards = CardStore(tmp_path / "cards")
    messages = MessageTool(MessageStore(tmp_path / "messages"))
    runtime = TaskRuntime(cards, messages)

    task = runtime.create_task(
        goal="build card message runtime",
        user_id="user-1",
        session_id="sess-1",
        complexity="large",
        progress_interval_seconds=600,
    )
    runtime.complete_task(task.task_id, artifact_refs=["artifact://report"])

    reloaded = cards.get_task(task.task_id)
    inbox = messages.read_inbox(MessageTarget(kind="session", identifier="sess-1"))

    assert reloaded.status == TaskStatus.COMPLETED
    assert reloaded.metadata["complexity"] == "large"
    assert reloaded.artifact_refs == ["artifact://report"]
    assert cards.get_progress_policy(task.task_id).interval_seconds == 600
    assert inbox[-1].message_type == "completion"
    assert inbox[-1].metadata["artifact_refs"] == ["artifact://report"]


def test_supervisor_requeues_task_when_worker_lease_expires(tmp_path):
    cards = CardStore(tmp_path / "cards")
    messages = MessageTool(MessageStore(tmp_path / "messages"))
    runtime = TaskRuntime(cards, messages)
    task = runtime.create_task(goal="recover me", user_id="user-1", session_id="sess-1")
    cards.update_task_status(task.task_id, TaskStatus.RUNNING)
    cards.acquire_lease("task", task.task_id, "worker-1", task_id=task.task_id, ttl_seconds=0.01)
    time.sleep(0.02)

    recovered = runtime.recover_expired_tasks()

    assert recovered == [task.task_id]
    assert cards.get_task(task.task_id).status == TaskStatus.QUEUED
    assert cards.list_events(task.task_id)[-1].event_type == "task.recovered"


def test_short_medium_large_tasks_share_same_contract(tmp_path):
    cards = CardStore(tmp_path / "cards")
    messages = MessageTool(MessageStore(tmp_path / "messages"))
    runtime = TaskRuntime(cards, messages)

    tasks = [
        runtime.create_task(goal="small edit", user_id="user-1", session_id="sess-1", complexity="short"),
        runtime.create_task(goal="medium refactor", user_id="user-1", session_id="sess-1", complexity="medium"),
        runtime.create_task(goal="large architecture change", user_id="user-1", session_id="sess-1", complexity="large"),
    ]

    queued = runtime.list_queued_tasks()

    assert [task.task_id for task in queued] == [task.task_id for task in tasks]
    assert [task.metadata["complexity"] for task in queued] == ["short", "medium", "large"]
    assert [task.metadata["worker_tier"] for task in queued] == ["weak_subagent", "weak_subagent", "task_agent"]
