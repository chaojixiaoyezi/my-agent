from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.tests.test_compact_scoped_checkpoint import (  # noqa: F401
    _commit_transcript,
    compact_case,
)
from agent_py_agent.tests.test_curator_observation_identity import _service
from agent_py_agent.tests.test_d2_curator_breaker_shrink_quarantine import _ShapeBackend


def test_checkpoint_claim_does_not_load_or_rewrite_owner_json(compact_case, monkeypatch):  # noqa: F811
    case = compact_case
    thread, _, _ = _commit_transcript(
        case, scope=THREAD_COMPACT_SCOPE, summary="summary", content="evidence", publish=True,
    )
    store = ConversationStore(case.store.storage.root, compact_root=case.root / "compact")
    service = _service(case.root, _ShapeBackend(thread.thread_id, lambda _: False), store)
    repository = RuntimeRepository(case.root / "runtime.db")
    service.state_store.runtime_repository = repository

    def unexpected_json_access(*args, **kwargs):
        raise AssertionError("foreground claim accessed owner Curator JSON")

    with monkeypatch.context() as patch:
        patch.setattr(service.state_store, "_load_unlocked", unexpected_json_access)
        patch.setattr(service.state_store, "_write_unlocked", unexpected_json_access)
        service.claim_compact_checkpoint(thread, max_rows=1)
        service.claim_compact_checkpoint(thread, max_rows=1)

    restarted = _service(case.root, service.backend, store)
    restarted.state_store.runtime_repository = repository
    state = restarted.state_store.load()
    assert state.pending_reasons == ["pre_compact"]
    assert state.pending_reason_generations["pre_compact"] == 1


def test_legacy_cursor_migrates_in_slices_and_wake_is_acknowledged_once(compact_case, monkeypatch):  # noqa: F811
    import json
    from dataclasses import replace

    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorState

    case = compact_case
    thread, message, _ = _commit_transcript(
        case, scope=THREAD_COMPACT_SCOPE, summary="summary", content="evidence", publish=True,
    )
    store = ConversationStore(case.store.storage.root, compact_root=case.root / "compact")
    backend = _ShapeBackend(thread.thread_id, lambda _: False)
    service = _service(case.root, backend, store)
    legacy = {f"legacy-{index:05d}": {"through": 1, "last_id": "a" * 64} for index in range(10000)}
    state = replace(MemoryCuratorState(), compact_consumption_cursors=legacy)
    path = service.state_store.path
    path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
    repository = RuntimeRepository(case.root / "runtime.db")
    service.state_store.bind_compact_repository(repository)
    original = path.read_bytes()
    service.state_store.migrate_compact_cursors(service.state_store.load(), limit=100)
    assert repository.curator_compact_migration_position() == 100
    assert path.read_bytes() == original

    def unexpected_json_access(*args, **kwargs):
        raise AssertionError("checkpoint claim accessed historical JSON")

    with monkeypatch.context() as patch:
        patch.setattr(service.state_store, "_load_unlocked", unexpected_json_access)
        patch.setattr(service.state_store, "_write_unlocked", unexpected_json_access)
        service.claim_compact_checkpoint(thread)
        service.claim_compact_checkpoint(thread)
    assert path.read_bytes() == original

    reopened = _service(case.root, backend, store)
    reopened.state_store.bind_compact_repository(RuntimeRepository(repository.db_path))
    assert reopened.state_store.load().pending_reason_generations == {"pre_compact": 1}
    outcome = reopened.run(reason="pre_compact")
    assert (outcome.status, outcome.processed_messages) == ("succeeded", 1)
    assert backend.seen == [[message.message_id]]

    last = _service(case.root, backend, store)
    last.state_store.bind_compact_repository(RuntimeRepository(repository.db_path))
    last.claim_compact_checkpoint(thread)
    latest = last.state_store.load()
    assert latest.pending_reasons == []
    assert latest.compact_wake_generation == 1
    assert last.state_store.compact_cursor("legacy-09999") == legacy["legacy-09999"]
