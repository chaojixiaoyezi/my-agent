"""W3：真实候选写盘计数、批次责任守恒、向量批量与终态冷归档。"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from agent_py_agent.agent.memory_store import candidates as ledger
from agent_py_agent.tests.test_memory_promotion_v2 import _explicit, _runtime


def _rows(tmp_path, count):
    service, store, thread = _runtime(tmp_path)
    rows = [_explicit(service, store, thread, content=f"独立批次事实 {i}", subject_key=f"w3.fact.{i}",
                      promotion_target="") for i in range(count)]
    return service, rows


def _observe_writes(monkeypatch, path):
    original = ledger.write_private_text_file_atomic_unlocked
    writes = []

    def write(target, text, **kwargs):
        result = original(target, text, **kwargs)
        if target == path:
            writes.append(text)
        return result

    monkeypatch.setattr(ledger, "write_private_text_file_atomic_unlocked", write)
    return writes


@pytest.mark.parametrize("count", [1, 8])
def test_batch_uses_two_real_candidate_flushes(tmp_path, monkeypatch, count):
    import json

    service, rows = _rows(tmp_path, count)
    writes = _observe_writes(monkeypatch, service.candidates.path)
    original = service._commit_target

    def commit(row, context):
        durable = [json.loads(line) for line in service.candidates.path.read_text().splitlines()]
        assert len(writes) == 1
        assert all(item["status"] == "approved" and item["commit_obligation"] for item in durable)
        return original(row, context)

    monkeypatch.setattr(service, "_commit_target", commit)
    results = service.promote_batch(rows, reviewer="w3", automatic=True)
    assert [item.candidate_id for item in results] == [row.candidate_id for row in rows]
    assert all(item.promoted and item.formal_memory_changed for item in results)
    assert len(writes) == 2
    assert len(service.long_term.all()) == count
    assert all(row.status == "promoted" and not row.commit_obligation for row in service.candidates.list())


def test_final_flush_failure_preserves_obligations_and_retry_is_idempotent(tmp_path, monkeypatch):
    import json

    service, rows = _rows(tmp_path, 8)
    original = ledger.write_private_text_file_atomic_unlocked
    failed = []

    def write(path, text, **kwargs):
        records = [json.loads(line) for line in text.splitlines()] if path == service.candidates.path else []
        if records and any(row["status"] == "promoted" for row in records) and not failed:
            failed.append(True)
            raise OSError("injected terminal receipt failure")
        return original(path, text, **kwargs)

    monkeypatch.setattr(ledger, "write_private_text_file_atomic_unlocked", write)
    results = service.promote_batch(rows, reviewer="w3", automatic=True)
    assert failed and all(not result.promoted for result in results)
    assert all(result.reason_code == "PROMOTION_IO_BLOCKED" for result in results)
    assert len(service.long_term.all()) == 8
    pending = service.candidates.list()
    assert all(row.status == "approved" and row.commit_obligation for row in pending)
    before = service.long_term.path.read_bytes()
    retried = service.promote_batch(pending, reviewer="w3", automatic=False)
    assert all(item.promoted and not item.formal_memory_changed for item in retried)
    assert service.long_term.path.read_bytes() == before
    assert len({row.entry_id for row in service.long_term.all()}) == 8
    assert all(row.status == "promoted" and not row.commit_obligation for row in service.candidates.list())


def test_known_item_failure_does_not_erase_other_successes(tmp_path, monkeypatch):
    service, rows = _rows(tmp_path, 8)
    original = service._commit_target

    def commit(row, context):
        if row.candidate_id == rows[3].candidate_id:
            raise OSError("one target unavailable")
        return original(row, context)

    monkeypatch.setattr(service, "_commit_target", commit)
    results = service.promote_batch(rows, reviewer="w3", automatic=True)
    assert [result.promoted for result in results] == [True, True, True, False, True, True, True, True]
    pending = service.candidates.get(rows[3].candidate_id)
    assert pending.status == "approved" and pending.commit_obligation and pending.obligation_retry_after
    assert len(service.long_term.all()) == 7


def test_batch_snapshot_conflict_is_item_local(tmp_path):
    service, rows = _rows(tmp_path, 2)
    service.candidates.transition(rows[0], "rejected", reviewer="owner")
    results = service.promote_batch(rows, reviewer="w3", automatic=True)
    assert not results[0].promoted and results[0].reason_code == "CANDIDATE_CHANGED_AFTER_VERIFICATION"
    assert results[1].promoted
    assert service.candidates.get(rows[0].candidate_id).status == "rejected"


def test_large_ledger_is_bounded_and_preserves_retry_cooldown(tmp_path, monkeypatch):
    service, rows = _rows(tmp_path, 1)
    seeds = [replace(rows[0], candidate_id=f"memory-candidate-seed-{i}",
                     subject_key=f"w3.seed.{i}") for i in range(2000)]
    # 每条都经正式序列化校验，不使用坏行或修改状态规则来制造大文件。
    ledger._write_candidates_unlocked(service.candidates.path, [*rows, *seeds], admission=ledger._NullAdmission())
    with pytest.raises(ValueError):
        service.promote_batch(seeds[:33], reviewer="w3", automatic=True)
    deferred = service.candidates.defer_commit(rows[0], "PROMOTION_IO_BLOCKED")
    writes = _observe_writes(monkeypatch, service.candidates.path)
    [result] = service.promote_batch([deferred], reviewer="w3", automatic=True)
    assert result.reason_code == "COMMIT_OBLIGATION_BACKOFF"
    assert not writes and not service.long_term.all()
    assert service.candidates.stats()["total"] == 2001
    untouched = service.candidates.get(seeds[-1].candidate_id)
    selected = [service.candidates.get(row.candidate_id) for row in seeds[:8]]
    results = service.promote_batch(selected, reviewer="w3", automatic=True)
    assert all(result.promoted for result in results)
    assert len(writes) == 2 and len(service.long_term.all()) == 8
    assert service.candidates.get(seeds[-1].candidate_id) == untouched
    assert service.candidates.get(deferred.candidate_id) == deferred


def test_batch_never_acquires_quota_under_candidate_lock(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.candidate_commit_lock import _OWNED

    service, rows = _rows(tmp_path, 8)
    enforcer = service.persona.quota_enforcer
    original = enforcer.admission
    entered = []

    @contextmanager
    def admission():
        assert not getattr(_OWNED, "paths", ())
        entered.append(True)
        with original() as admitted:
            yield admitted

    monkeypatch.setattr(enforcer, "admission", admission)
    assert all(item.promoted for item in service.promote_batch(rows, reviewer="w3", automatic=True))
    assert entered


def test_batch_vector_snapshot_is_written_once(tmp_path, monkeypatch):
    from agent_py_agent.agent.retrieval.vector_store import VectorStore
    from agent_py_agent.tests._hashing_embedder import LocalHashingEmbedder

    service, rows = _rows(tmp_path, 8)
    service.long_term._embedder = LocalHashingEmbedder(dim=8)
    original = VectorStore._write_snapshot
    snapshots = []

    def write(store, items, identity, dim):
        snapshots.append(set(items))
        return original(store, items, identity, dim)

    monkeypatch.setattr(VectorStore, "_write_snapshot", write)
    assert all(item.promoted for item in service.promote_batch(rows, reviewer="w3", automatic=True))
    assert len(snapshots) == 1 and len(snapshots[0]) == 8


@pytest.mark.parametrize("status", ["rejected", "withdrawn", "expired", "superseded"])
def test_curator_tick_archives_old_terminal_details_only(tmp_path, status):
    import json
    from types import SimpleNamespace

    from agent_py_agent.tests.test_memory_curator_v2 import _service

    service, rows = _rows(tmp_path, 3)
    approved = service.candidates.transition(rows[1], "approved", reviewer="owner")
    rejected = service.candidates.transition(rows[2], status, reviewer="owner")
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=40)).isoformat()
    rejected = replace(rejected, updated_at=old)
    ledger._write_candidates_unlocked(service.candidates.path, [rows[0], approved, rejected], admission=ledger._NullAdmission())
    curator = _service(tmp_path / "curator", SimpleNamespace(), service.message_verifier.conversation_store)
    curator.candidate_service = service.candidates
    before = {row.candidate_id: row for row in service.candidates.list()}
    curator.run_if_due(now=now)
    hot = {row["candidate_id"]: row for line in service.candidates.path.read_text().splitlines() if (row := json.loads(line))}
    # candidate_models的现行schema只允许三类终态持cold_detail_ref，不能越过白名单去放松该合同。
    assert bool(hot[rejected.candidate_id].get("cold_detail_ref")) is (status in {"rejected", "superseded"})
    assert not hot[rows[0].candidate_id].get("cold_detail_ref")
    assert not hot[approved.candidate_id].get("cold_detail_ref")
    assert service.candidates.get(rejected.candidate_id).source_message_refs == rejected.source_message_refs
    assert service.candidates.get(rows[0].candidate_id) == before[rows[0].candidate_id]
    assert service.candidates.get(approved.candidate_id) == before[approved.candidate_id]


def test_failed_evidence_block_never_reports_unpersisted_state_or_adds_obligation(tmp_path, monkeypatch):
    import json

    from agent_py_agent.tests.test_opt_memory_obligations import _pending_automatic_candidate

    service, candidate = _pending_automatic_candidate(tmp_path)
    assert not candidate.commit_obligation
    original = ledger.write_private_text_file_atomic_unlocked
    failed = []

    def write(path, text, **kwargs):
        if (path == service.candidates.path and not failed
                and any(json.loads(line)["status"] == "blocked_missing_evidence" for line in text.splitlines())):
            failed.append(True)
            raise OSError("evidence rejection receipt write failed")
        return original(path, text, **kwargs)

    monkeypatch.setattr(service, "_verify_evidence", lambda row: "USER_MESSAGE_EVIDENCE_INVALID")
    monkeypatch.setattr(ledger, "write_private_text_file_atomic_unlocked", write)
    [result] = service.promote_batch([candidate], reviewer="w3", automatic=True)
    saved = service.candidates.get(candidate.candidate_id)
    assert failed and result.status == saved.status == "pending_review"
    assert result.reason_code == "PROMOTION_IO_BLOCKED" and not result.promoted
    assert not saved.commit_obligation and not service.long_term.all()


def test_remove_cleanup_failure_is_item_local_and_keeps_real_commit_fact(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store._jsonl_indexing import RemovalCleanupError

    service, store, thread = _runtime(tmp_path)
    initial = _explicit(service, store, thread, content="待删除的独立事实", subject_key="remove.target")
    assert service.promote(initial.candidate_id, automatic=True).promoted
    [target] = service.long_term.all()
    removal = _explicit(service, store, thread, content=target.content, subject_key="remove.target",
        proposed_action="remove", target_entry_id=target.entry_id)
    addition = _explicit(service, store, thread, content="不能因删除清理失败被覆盖的成功事实", subject_key="keep.target")
    original = service.long_term._after_commit_indexes

    def after(records, **kwargs):
        if any(record.action == "remove" for record in records):
            raise RemovalCleanupError("REMOVE_CLEANUP_PENDING", formal_memory_changed=True)
        return original(records, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(service.long_term, "_after_commit_indexes", after)
        results = service.promote_batch([addition, removal], reviewer="w3", automatic=True)
    assert results[0].promoted and results[0].formal_memory_changed
    assert not results[1].promoted and results[1].reason_code == "REMOVE_CLEANUP_PENDING"
    assert results[1].formal_memory_changed
    assert service.candidates.get(addition.candidate_id).status == "promoted"
    pending = service.candidates.get(removal.candidate_id)
    assert pending.status == "approved" and pending.commit_obligation
    before = service.long_term.path.read_bytes()
    [recovered] = service.promote_batch([pending], reviewer="w3", automatic=True)
    assert recovered.promoted and recovered.reason_code == "REMOVE_CLEANUP_RECOVERED"
    assert not recovered.formal_memory_changed
    assert service.long_term.path.read_bytes() == before


def test_conflict_receipt_io_failure_does_not_erase_other_success(tmp_path, monkeypatch):
    import json

    service, store, thread = _runtime(tmp_path)
    initial = _explicit(service, store, thread, content="主题的原始事实", subject_key="conflict.target")
    assert service.promote(initial.candidate_id, automatic=True).promoted
    conflict = _explicit(service, store, thread, content="同主题的冲突事实", subject_key="conflict.target")
    good = _explicit(service, store, thread, content="独立可提交事实", subject_key="good.target")
    original = ledger.write_private_text_file_atomic_unlocked
    failed = []

    def write(path, text, **kwargs):
        if (path == service.candidates.path and not failed
                and any(json.loads(line)["status"] == "blocked_conflict" for line in text.splitlines())):
            failed.append(True)
            raise OSError("conflict receipt unavailable once")
        return original(path, text, **kwargs)

    monkeypatch.setattr(ledger, "write_private_text_file_atomic_unlocked", write)
    results = service.promote_batch([good, conflict], reviewer="w3", automatic=True)
    assert failed and results[0].promoted
    assert not results[1].promoted and results[1].reason_code == "PROMOTION_IO_BLOCKED"
    assert service.candidates.get(good.candidate_id).status == "promoted"
    pending = service.candidates.get(conflict.candidate_id)
    assert pending.status == "approved" and pending.commit_obligation and pending.obligation_retry_after


@pytest.mark.parametrize("kind", ["missing", "transport", "exit"])
def test_uncertain_batch_keeps_original_error_and_approved_obligations(tmp_path, monkeypatch, kind):
    from agent_py_agent.tests.test_curator_uncertain_outcome import _unknown

    service, rows = _rows(tmp_path, 2)
    original, error, condenses = service._commit_target, _unknown(kind), []

    def commit(row, context):
        if row.candidate_id == rows[1].candidate_id:
            raise error
        return original(row, context)

    monkeypatch.setattr(service, "_commit_target", commit)
    monkeypatch.setattr(service.long_term, "condense", lambda: condenses.append(True))
    with pytest.raises(type(error)) as caught:
        service.promote_batch(rows, reviewer="w3", automatic=True)
    assert caught.value is error and not condenses
    assert len(service.long_term.all()) == 1
    assert all(row.status == "approved" and row.commit_obligation and not row.obligation_retry_after
               for row in service.candidates.list())
