from types import SimpleNamespace

from agent_py_agent.agent.conversation import ConversationStore, runtime


def test_local_canonical_pending_does_not_fail_scheduler_run(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.create(owner_id="owner")
    wake = store.wakes.raise_signal({"thread_id": thread.thread_id, "reason": "scheduler_run", "root_task_id": "task"})
    store.wakes.cache_delivery(wake.wake_signal_id, {
        "schema_version": "wake-owner-delivery.v2", "reason": wake.reason, "task_id": wake.root_task_id,
        "content": "business succeeded", "external_sent": False, "ownership": "local", "canonical_pending": True})
    released, finished = [], []
    scheduler = SimpleNamespace(store=store, scheduler_service=SimpleNamespace(
        release=lambda *a, **k: released.append(a), finish=lambda *a, **k: finished.append(a)))
    runtime._handle_nonquota_wake_error(scheduler, wake, lifecycle_reason="scheduler_run", claim="claim", error=OSError("EIO"))
    assert released == [("claim",)]
    assert finished == []
    assert store.wakes.pending_one(wake.wake_signal_id) is not None
