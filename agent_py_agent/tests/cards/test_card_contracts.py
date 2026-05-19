from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.cards import (
    CardStore,
    CheckpointCard,
    NotificationRouteCard,
    ProgressPolicyCard,
    SessionCard,
    SubagentRunCard,
    TaskStatus,
    WorkerRunCard,
)


def test_task_status_transition_writes_event(tmp_path):
    store = CardStore(tmp_path)
    task = store.create_task(
        goal="write a report",
        user_id="user-1",
        session_id="sess-1",
        acceptance=["report exists"],
    )

    updated = store.update_task_status(task.task_id, TaskStatus.RUNNING, reason="worker claimed")

    assert updated.status == TaskStatus.RUNNING
    events = store.list_events(task.task_id)
    assert [event.event_type for event in events] == ["task.created", "task.status_changed"]
    assert events[-1].payload["from_status"] == TaskStatus.QUEUED
    assert events[-1].payload["to_status"] == TaskStatus.RUNNING


def test_invalid_task_status_transition_is_rejected(tmp_path):
    store = CardStore(tmp_path)
    task = store.create_task(goal="ship", user_id="user-1", session_id="sess-1")
    store.update_task_status(task.task_id, TaskStatus.COMPLETED)

    with pytest.raises(ValueError, match="invalid task status transition"):
        store.update_task_status(task.task_id, TaskStatus.RUNNING)


def test_lease_prevents_duplicate_active_worker(tmp_path):
    store = CardStore(tmp_path)
    first = store.acquire_lease(
        resource_type="task",
        resource_id="task-1",
        owner_id="worker-a",
        task_id="task-1",
        ttl_seconds=60,
    )
    second = store.acquire_lease(
        resource_type="task",
        resource_id="task-1",
        owner_id="worker-b",
        task_id="task-1",
        ttl_seconds=60,
    )

    assert first is not None
    assert second is None
    assert store.get_active_lease("task", "task-1").owner_id == "worker-a"


def test_expired_lease_can_be_reclaimed(tmp_path):
    store = CardStore(tmp_path)
    lease = store.acquire_lease(
        resource_type="task",
        resource_id="task-1",
        owner_id="worker-a",
        task_id="task-1",
        ttl_seconds=0.01,
    )
    assert lease is not None
    time.sleep(0.02)

    reclaimed = store.acquire_lease(
        resource_type="task",
        resource_id="task-1",
        owner_id="worker-b",
        task_id="task-1",
        ttl_seconds=60,
    )

    assert reclaimed is not None
    assert reclaimed.owner_id == "worker-b"


def test_checkpoint_and_routes_are_recoverable(tmp_path):
    store = CardStore(tmp_path)
    task = store.create_task(goal="long task", user_id="user-1", session_id="sess-1")
    checkpoint = CheckpointCard(
        checkpoint_id="cp-1",
        task_id=task.task_id,
        step="phase-2",
        payload={"files": ["a.txt"]},
    )
    route = NotificationRouteCard(
        route_id="route-1",
        task_id=task.task_id,
        user_id="user-1",
        channel="internal",
        target="session:sess-1",
    )
    policy = ProgressPolicyCard(
        policy_id="progress-1",
        task_id=task.task_id,
        mode="interval",
        interval_seconds=600,
    )

    store.save_checkpoint(checkpoint)
    store.save_notification_route(route)
    store.save_progress_policy(policy)

    assert store.latest_checkpoint(task.task_id).step == "phase-2"
    assert store.get_notification_route(task.task_id).target == "session:sess-1"
    assert store.get_progress_policy(task.task_id).interval_seconds == 600


def test_integrity_reports_orphan_child_reference(tmp_path):
    store = CardStore(tmp_path)
    parent = store.create_task(goal="parent", user_id="user-1", session_id="sess-1")
    parent.child_task_ids.append("missing-child")
    store.save_task(parent)

    findings = store.check_integrity()

    assert findings == [
        {
            "code": "missing_child_task",
            "task_id": parent.task_id,
            "child_task_id": "missing-child",
        }
    ]


def test_session_card_persists_active_task_links(tmp_path):
    store = CardStore(tmp_path)
    session = SessionCard(session_id="sess-1", user_id="user-1", channel="chat")

    store.save_session(session)
    task = store.create_task(goal="long task", user_id="user-1", session_id="sess-1")
    store.attach_task_to_session("sess-1", task.task_id)

    reloaded = store.get_session("sess-1")
    assert reloaded.user_id == "user-1"
    assert reloaded.active_task_ids == [task.task_id]


def test_terminal_task_is_removed_from_session_active_list(tmp_path):
    store = CardStore(tmp_path)
    store.save_session(SessionCard(session_id="sess-1", user_id="user-1", channel="chat"))
    task = store.create_task(goal="long task", user_id="user-1", session_id="sess-1")
    store.attach_task_to_session("sess-1", task.task_id)

    store.update_task_status(task.task_id, TaskStatus.COMPLETED)

    assert store.get_session("sess-1").active_task_ids == []
    assert store.list_events(task.task_id)[-2].event_type == "session.task_detached"


def test_concurrent_session_attach_keeps_all_task_links(tmp_path):
    store = CardStore(tmp_path)
    store.save_session(SessionCard(session_id="sess-1", user_id="user-1", channel="chat"))
    tasks = [
        store.create_task(goal=f"task {index}", user_id="user-1", session_id="sess-1")
        for index in range(12)
    ]

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda task: store.attach_task_to_session("sess-1", task.task_id), tasks))

    assert set(store.get_session("sess-1").active_task_ids) == {task.task_id for task in tasks}


def test_worker_run_card_persists_execution_attempt(tmp_path):
    store = CardStore(tmp_path)
    task = store.create_task(goal="background work", user_id="user-1", session_id="sess-1")
    worker_run = store.create_worker_run(
        task_id=task.task_id,
        worker_id="worker-1",
        worker_type="task_agent",
        lease_id="lease-1",
    )

    reloaded = store.get_worker_run(worker_run.worker_run_id)

    assert isinstance(reloaded, WorkerRunCard)
    assert reloaded.task_id == task.task_id
    assert reloaded.worker_id == "worker-1"
    assert store.list_worker_runs(task.task_id)[0].lease_id == "lease-1"


def test_subagent_run_record_tracks_child_session_and_pending_delivery(tmp_path):
    store = CardStore(tmp_path)
    run = store.create_subagent_run(
        requester_session_id="sess-parent",
        child_session_id="sess-child",
        task_id="task-1",
        goal="research and write",
        controller_session_id="sess-parent",
        label="researcher",
        mode="session",
        context_mode="fork",
        expects_completion_message=True,
        metadata={"spawn_depth": 1},
    )

    store.mark_subagent_started(run.run_id)
    ended = store.mark_subagent_completed(
        run.run_id,
        outcome="ok",
        artifact_refs=["artifact://report"],
        pending_final_delivery=True,
    )

    reloaded = store.get_subagent_run(run.run_id)
    assert isinstance(reloaded, SubagentRunCard)
    assert reloaded.child_session_id == "sess-child"
    assert reloaded.requester_session_id == "sess-parent"
    assert reloaded.status == "completed"
    assert reloaded.outcome == "ok"
    assert reloaded.artifact_refs == ["artifact://report"]
    assert ended.pending_final_delivery is True
    assert store.list_subagent_runs(requester_session_id="sess-parent") == [reloaded]
    assert store.list_pending_final_delivery()[0].run_id == run.run_id
