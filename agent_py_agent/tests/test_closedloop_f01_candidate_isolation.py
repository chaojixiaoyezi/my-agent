"""坏候选只影响本条；原归档严格门、有限配额与自主策展入口保持真实。"""
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from agent_py_agent.agent.memory_store.candidate_ledger import CandidateRows, read_manifest
from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig
from agent_py_agent.tests.test_memory_candidates import candidate
from agent_py_agent.tests.test_memory_curator_v2 import (
    _conversation,
    _service,
    _StaticStructuredBackend,
)


def _corrupt_row(service, candidate_id):
    rows = CandidateRows(service.path, read_manifest(service.path))
    reference = rows.index.get(rows.root, 'row/' + candidate_id)['record']
    path = service.path.parent / (service.path.name + '.records') / reference['ref']
    path.write_bytes(b'broken row\n')
    return path


def test_indexed_row_isolation_advances_original_position(tmp_path):
    from agent_py_agent.agent.memory_store.candidates import CandidateService
    from agent_py_agent.tests.test_memory_candidate_daily_v2 import _user_observation

    service = CandidateService(tmp_path / 'candidates.jsonl')
    bad = service.observe(replace(_user_observation(message_id='bad'), subject_key='bad'))
    healthy = service.observe(replace(_user_observation(message_id='healthy'), subject_key='healthy'))
    bad_path = _corrupt_row(service, bad.candidate_id)
    first = service.readonly_window(max_records=1)
    assert len(first) == 4 and first.partial and not first[0]
    assert first.errors[0]['position'] == 0 and first[2]['position'] == 1
    second = service.readonly_window(first[2], max_records=1)
    assert [row.candidate_id for row in second[0]] == [healthy.candidate_id]
    assert second[3] and not second.partial
    before = service.path.read_bytes(), bad_path.read_bytes()
    with pytest.raises((ValueError, OSError)):
        service.compact_terminal_candidates()
    assert (service.path.read_bytes(), bad_path.read_bytes()) == before


@pytest.mark.parametrize("bad_bytes", [b"{bad json}\n", b"{}\n"])
def test_legacy_bad_record_is_partial_and_later_record_remains_readable(tmp_path, monkeypatch, bad_bytes):
    from agent_py_agent.agent.memory_store.candidates import CandidateService

    service = CandidateService(tmp_path / 'candidates.jsonl')
    healthy = candidate()
    service.path.write_bytes(bad_bytes + json.dumps(healthy.to_record()).encode() + b'\n')
    monkeypatch.setattr('agent_py_agent.agent.common.cache_freshness.cache_entry_trustworthy', lambda _stamp: True)
    first = service.readonly_window(max_records=1)
    assert first.partial and not first[0] and not first[3]
    second = service.readonly_window(first[2], max_records=1)
    assert [row.candidate_id for row in second[0]] == [healthy.candidate_id] and second[3]


def test_oversized_legacy_row_discards_in_bounded_slices(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.candidates import CandidateService

    service = CandidateService(tmp_path / 'candidates.jsonl')
    healthy = candidate()
    service.path.write_bytes(b'x' * (8 * 1024 * 1024 + 100) + b'\n' + json.dumps(healthy.to_record()).encode() + b'\n')
    monkeypatch.setattr('agent_py_agent.agent.common.cache_freshness.cache_entry_trustworthy', lambda _stamp: True)
    first = service.readonly_window()
    assert first.partial and first[2]['discard_row'] and not first[0]
    second = service.readonly_window(first[2])
    assert [row.candidate_id for row in second[0]] == [healthy.candidate_id]


def test_public_autonomous_promotion_survives_bad_sibling_with_finite_quota(tmp_path):
    from agent_py_agent.agent.memory_store.curator_formal import FormalMemorySource
    from agent_py_agent.agent.user_space.owner_quota import (
        OwnerQuotaEnforcer,
        reconcile_owner_quota_slice,
    )
    from agent_py_agent.tests.test_memory_promotion_v2 import _explicit, _runtime

    promotion, store, thread = _runtime(tmp_path)
    bad = _explicit(promotion, store, thread, content='坏候选记录', subject_key='broken')
    healthy = _explicit(promotion, store, thread, content='健康用户事实', subject_key='healthy')
    bad_path = _corrupt_row(promotion.candidates, bad.candidate_id)
    quota = OwnerQuotaEnforcer(promotion.persona.owner_home, max_bytes=1024**3)
    for repository in (promotion.candidates, promotion.long_term, promotion.persona):
        repository.quota_enforcer = quota
    for _ in range(256):
        if reconcile_owner_quota_slice(promotion.persona.owner_home, max_entries=32, max_ms=1000)['complete']:
            break
    else:
        pytest.fail('finite quota reconciliation failed to finish within 256 slices')
    curator = _service(tmp_path / 'curator-data', _StaticStructuredBackend({}), store,
        config=MemoryCuratorConfig(interval_seconds=60, daily_finalize_hour=23))
    curator.candidate_service = promotion.candidates
    curator.promotion_callback = lambda cid: promotion.promote(cid, automatic=True)
    curator.formal_memory_source = FormalMemorySource(promotion.long_term, promotion.lessons, promotion.hot)
    original = bad_path.read_bytes()
    result = curator.run_if_due(now=datetime(2026, 11, 16, 12, tzinfo=timezone.utc))
    assert result.status == 'succeeded' and result.formal_commits == 1
    assert any('CURATOR_CANDIDATE_ARCHIVE_FAILED' in item for item in result.warnings)
    assert any('CURATOR_CANDIDATE_READ_PARTIAL' in item for item in result.warnings)
    assert promotion.candidates.get(healthy.candidate_id).status == 'promoted'
    assert [row.content for row in promotion.long_term.all()] == [healthy.content]
    assert bad_path.read_bytes() == original
    cursor = curator.state_store.load().candidate_scan_cursor
    assert cursor['scan']['wrapped'] and cursor['partial'] and cursor['active']


@pytest.mark.parametrize('kind', ['missing', 'transport', 'exit'])
def test_archive_unknown_remains_original_exception(tmp_path, monkeypatch, kind):
    from agent_py_agent.tests.test_curator_uncertain_outcome import _unknown

    store, _, _ = _conversation(tmp_path)
    curator = _service(tmp_path, _StaticStructuredBackend({}), store)
    error = _unknown(kind)
    def fail(**_kwargs):
        raise error
    monkeypatch.setattr(curator.candidate_service, 'compact_terminal_candidates', fail)
    with pytest.raises(type(error)) as caught:
        curator.run_if_due()
    assert caught.value is error and not hasattr(curator, '_terminal_compaction_at')


def test_continuous_precompact_and_failed_daily_take_turns_without_clearing_pending(tmp_path, monkeypatch):
    store, _, _ = _conversation(tmp_path)
    curator = _service(tmp_path, _StaticStructuredBackend({}), store)
    curator.timezone_name = 'UTC'
    curator.request('pre_compact')
    calls = []
    def daily(*, reason, **_kwargs):
        calls.append(reason)
        return curator._result(status='failed', reason=reason)
    def pending(_state, reason, _current, **_kwargs):
        calls.append(reason)
        return curator._result(status='succeeded', reason=reason)
    monkeypatch.setattr(curator, 'run', daily)
    monkeypatch.setattr(curator, '_run_pending_when_due', pending)
    now = datetime(2026, 11, 16, 23, 30, tzinfo=timezone.utc)
    from agent_py_agent.agent.backends.request_scope import foreground_model_scope
    curator.backend.api_base = 'http://model.test'
    with foreground_model_scope(curator.backend):
        curator.run_if_due(now=now)
    assert calls == ['pre_compact']
    calls.clear()
    for _ in range(4):
        curator.run_if_due(now=now)
    assert calls == ['daily_finalize', 'pre_compact', 'daily_finalize', 'pre_compact']
    assert 'pre_compact' in curator.state_store.load().pending_reasons


def test_exclusion_unknown_only_parks_affected_candidate_without_persisting_mark(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_memory_promotion_v2 import _explicit, _runtime

    promotion, store, thread = _runtime(tmp_path)
    healthy = _explicit(promotion, store, thread, content='健康用户事实')
    unknown = replace(healthy, candidate_id='unknown-candidate', source_message_refs=[{'thread_id': 'missing-thread'}])
    curator = _service(tmp_path / 'curator', _StaticStructuredBackend({}), store)
    curator.learning_exclusions = lambda: (frozenset({'excluded-chat'}), False)
    warnings = []
    assert curator._drop_excluded_promotable([unknown, healthy], warnings_out=warnings) == [healthy]
    assert warnings == ['THREAD_SOURCE_MISSING']
    monkeypatch.setattr(curator.candidate_service, 'get', lambda _cid: unknown)
    monkeypatch.setattr(curator, '_sync_learning_exclusions', lambda _ids: pytest.fail('unknown source persisted exclusion'))
    skipped, warnings = curator._recheck_exclusion_before_promotion(unknown.candidate_id)
    assert skipped and warnings == ['THREAD_SOURCE_MISSING']
    assert not unknown.promotion_exclusion and not healthy.promotion_exclusion
