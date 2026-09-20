from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.scheduler.repository import (
    SchedulerJobCreateRequest,
    SchedulerRepository,
)
from agent_py_agent.agent.scheduler.service import SchedulerService


def _setup(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "open-1",
            "now": 900.0,
        }
    )
    repository = SchedulerRepository(
        tmp_path / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id="user-1",
    )
    return store, thread, repository


def _create(repository, thread_id: str, *, name: str, skill_refs=None):
    return repository.create_job(
        SchedulerJobCreateRequest(
            name=name,
            prompt=f"do {name}",
            thread_id=thread_id,
            source_task_id="",
            schedule={
                "kind": "every",
                "every_seconds": 600,
                "anchor_at": 1_000,
                "timezone": "UTC",
            },
            misfire_grace_seconds=60,
            skill_refs=skill_refs or [],
            source_request_id=name,
            now=900,
        )
    )[0]


def test_service_enqueues_claims_and_finishes_in_the_same_thread(tmp_path) -> None:
    store, thread, repository = _setup(tmp_path)
    first = _create(repository, thread.thread_id, name="first")
    second = _create(repository, thread.thread_id, name="second")
    service = SchedulerService(repository, conversation_store=store)

    wake_ids = service.enqueue_ready_runs(now=1_000)
    signals = store.wakes.pending()
    assert len(wake_ids) == 2
    assert len(signals) == 2
    assert {signal.thread_id for signal in signals} == {thread.thread_id}
    assert {signal.metadata["scheduler_job_id"] for signal in signals} == {
        first["job_id"],
        second["job_id"],
    }

    result = service.claim_wake(signals[0], lease_seconds=60, now=1_001)
    assert result.status == "claimed"
    assert result.claim is not None
    terminal = service.finish(
        result.claim,
        status="done",
        response="complete",
        delivery_status="sent",
        now=1_002,
    )
    assert terminal is not None
    history, errors = repository.history(job_id=str(terminal["job_id"]))
    assert errors == []
    assert history[0]["response"] == "complete"


def test_service_reuses_deduped_wake_after_restart(tmp_path) -> None:
    store, thread, repository = _setup(tmp_path)
    job = _create(repository, thread.thread_id, name="restart")
    repository.reserve_due_runs(now=1_000)

    restarted_repository = SchedulerRepository(
        repository.root,
        owner_provider="feishu",
        owner_kind="user",
        owner_id="user-1",
    )
    restarted = SchedulerService(restarted_repository, conversation_store=store)
    first_ids = restarted.enqueue_ready_runs(now=1_001)
    second_ids = restarted.enqueue_ready_runs(now=1_002)
    assert first_ids == second_ids
    assert len(store.wakes.pending()) == 1
    assert store.wakes.pending()[0].metadata["scheduler_job_id"] == job["job_id"]


def test_stale_skill_snapshot_fails_before_model_execution(tmp_path) -> None:
    store, thread, repository = _setup(tmp_path)
    job = _create(
        repository,
        thread.thread_id,
        name="skill",
        skill_refs=[{"stable_id": "shared:skill", "content_sha256": "a" * 64}],
    )
    snapshot = SimpleNamespace(resolve=lambda _reference: SimpleNamespace(content_sha256="b" * 64))
    service = SchedulerService(
        repository,
        conversation_store=store,
        skill_snapshot_provider=lambda: snapshot,
    )
    assert service.enqueue_ready_runs(now=1_000) == []
    assert store.wakes.pending() == []
    history, _errors = repository.history(job_id=str(job["job_id"]))
    assert history[0]["status"] == "failed"
    assert history[0]["error_code"] == "SCHEDULER_SKILL_SNAPSHOT_STALE"
