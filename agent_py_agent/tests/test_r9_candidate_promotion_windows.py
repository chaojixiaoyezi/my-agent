"""只读窗口的规模、跨重启公平、旧JSONL续点与零提取续办。"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.learning_source import user_dialogue_projection
from agent_py_agent.agent.memory_store import candidate_ledger
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.curator_lease import release_curator_lease
from agent_py_agent.agent.memory_store.curator_state import CuratorLeaseRequest
from agent_py_agent.tests.test_memory_curator_v2 import _service, _StaticStructuredBackend


def seeded(root, count=257):
    service = _service(root, _StaticStructuredBackend({}), ConversationStore(root / 'c'))
    service.candidate_service.observe_many([CandidateObservation(candidate_type='long_term_fact',
        content=f'合成规范候选{i}', subject_key=f'synthetic.{i}', scope=MemoryScope('personal', 'personal'),
        origin='user_explicit', promotion_target='long_term', promotion_mode='auto_eligible',
        learning_source=user_dialogue_projection(),
        source_message_refs=({'thread_id': 'source-thread', 'message_id': f'm-{i}'},)) for i in range(count)])
    service.promotion_callback = lambda candidate_id: None
    return service


def select_under_lease(service, now=None):
    current = now or datetime.now(timezone.utc)
    acquired = service.committer.recover_and_acquire(CuratorLeaseRequest(
        'interval', service.config.revision(), 30, current, purpose='candidate_scan'))
    assert acquired is not None
    lease_id = acquired[1]['lease_id']
    try:
        return service._pending_promotable_candidate_ids(limit=32, lease_id=lease_id, scanned_at=current.isoformat())
    finally:
        release_curator_lease(service.state_store, lease_id)


@pytest.mark.parametrize('count', [85, 257])
def test_every_deferred_candidate_including_last_page_is_served_after_cold_restart(tmp_path, count):
    service = seeded(tmp_path, count)
    served = []
    for _ in range(20):
        identifiers = select_under_lease(service)
        assert len(identifiers) <= 32
        served.extend(identifiers)
        state = service.state_store.load()
        assert not state.pending_reasons
        assert len(state.candidate_scan_cursor.get('pending', [])) <= 128
        if not state.candidate_scan_cursor['active']:
            break
        service = _service(tmp_path, _StaticStructuredBackend({}), ConversationStore(tmp_path / 'c'))
        service.promotion_callback = lambda candidate_id: None
    else:
        pytest.fail('bounded candidate rotation did not finish')
    assert len(served) == len(set(served)) == count
    assert service.state_store.load().candidate_scan_cursor['scan']['wrapped'] is True


def test_indexed_first_window_never_loads_entire_status_group(tmp_path, monkeypatch):
    service = seeded(tmp_path, 513)
    loaded = []
    original = candidate_ledger._read_candidate_row
    def read(*args, **kwargs):
        loaded.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(candidate_ledger, '_read_candidate_row', read)
    monkeypatch.setattr(service.candidate_service, 'readonly_rows', lambda **kwargs: pytest.fail('full group scan'))
    assert len(service._pending_promotable_candidate_ids()) == 32
    assert len(loaded) == 128


def test_legacy_blank_lines_final_no_newline_and_multi_megabyte_window_resume(tmp_path):
    service = seeded(tmp_path, 12)
    rows = service.candidate_service.list()
    records = [row.to_record() for row in rows]
    path = service.candidate_service.path
    path.write_text('\n' + '\n\n'.join(json.dumps(row, ensure_ascii=False).replace('{', '{' + ' ' * 800000, 1) for row in records), encoding='utf-8')
    time.sleep(2.05)  # 真实mtime/ctime/ChangeTime越过原粗时钟窗口后才复用字节续点。
    first, refs, cursor, wrapped = service.candidate_service.readonly_window()
    assert first and not wrapped and 0 < cursor['offset'] < path.stat().st_size
    second, _, _, wrapped = service.candidate_service.readonly_window(cursor)
    assert wrapped and len(first) + len(second) == len(rows)
    assert len({row.candidate_id for row in (*first, *second)}) == len(rows)
    before = cursor
    path.write_text(path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
    restarted, _, after, _ = service.candidate_service.readonly_window(before)
    assert restarted[0].candidate_id == rows[0].candidate_id
    assert after['source'] != before['source']


def test_maintenance_continuation_has_no_extraction_no_trigger_requeue_and_obeys_cooldown(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.curator_inputs import CuratorInputBatch
    service = seeded(tmp_path, 85)
    now = datetime.now(timezone.utc).replace(hour=12)
    state = replace(service.state_store.load(), last_daily_finalize_date=now.date().isoformat())
    with service.state_store.path.open('w', encoding='utf-8') as handle:
        json.dump(state.to_dict(), handle)
    assert len(select_under_lease(service, now)) == 32
    monkeypatch.setattr(service, '_collect', lambda *args, **kwargs: CuratorInputBatch(messages=(), audit_events=()))
    monkeypatch.setattr(service, '_promote_eligible', lambda ids: (0, (), ()))
    assert service.run_if_due(now=now + timedelta(seconds=1)).status == 'not_due'
    assert service.run_if_due(now=now + timedelta(seconds=61)).status == 'succeeded'
    assert not service.state_store.load().pending_reasons
    assert service.run_if_due(now=now + timedelta(seconds=62)).status == 'not_due'
    assert service.run_if_due(now=now + timedelta(seconds=122)).status == 'succeeded'
    state = service.state_store.load()
    assert not state.candidate_scan_cursor['active'] and not state.pending_reasons
    assert state.last_daily_finalize_date == now.date().isoformat()
    assert service.backend.calls == 0
    assert service.run_if_due(now=now + timedelta(seconds=183)).status == 'not_due'


@pytest.mark.parametrize('manual,callback', [(True, True), (False, False)])
def test_disabled_promotion_never_creates_backlog_scan(tmp_path, monkeypatch, manual, callback):
    from agent_py_agent.agent.memory_store.curator_inputs import CuratorInputBatch
    service = seeded(tmp_path, 85)
    now = datetime.now(timezone.utc)
    state = replace(service.state_store.load(), last_daily_finalize_date=now.date().isoformat())
    service.state_store.path.write_text(json.dumps(state.to_dict()), encoding='utf-8')
    if manual:
        service.config = replace(service.config, auto_promotion_policy='manual_only')
    if not callback:
        service.promotion_callback = None
    monkeypatch.setattr(service, '_collect', lambda *args, **kwargs: CuratorInputBatch(messages=(), audit_events=()))
    monkeypatch.setattr(service.candidate_service, 'readonly_window', lambda *args, **kwargs: pytest.fail('disabled scan'))
    assert service.run_if_due(now=now).status == 'not_due'
    assert service.state_store.load().candidate_scan_cursor == {}
    assert service.backend.calls == 0


def test_initial_backlog_without_new_messages_is_discovered_and_foreground_still_defers(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends import request_scope
    from agent_py_agent.agent.memory_store.curator_due import candidate_scan_deadline
    service = seeded(tmp_path, 85)
    now = datetime.now(timezone.utc)
    state = replace(service.state_store.load(), last_daily_finalize_date=now.date().isoformat())
    service.state_store.path.write_text(json.dumps(state.to_dict()), encoding='utf-8')
    monkeypatch.setattr(service, '_collect', lambda *args, **kwargs: pytest.fail('continuation cannot collect'))
    monkeypatch.setattr(service, '_promote_eligible', lambda ids: (0, (), ()))
    monkeypatch.setattr(request_scope, 'foreground_model_active', lambda backend: True)
    assert service.run_if_due(now=now).status == 'busy'
    assert service.state_store.load().candidate_scan_cursor == {}
    monkeypatch.setattr(request_scope, 'foreground_model_active', lambda backend: False)
    assert service.run_if_due(now=now).status == 'succeeded'
    state = service.state_store.load()
    assert state.candidate_scan_cursor['active'] and not state.pending_reasons
    assert service.backend.calls == 0
    failed = replace(state, last_failure_code='CURATOR_CANDIDATE_READ_FAILED', last_failure_at=(now + timedelta(seconds=30)).isoformat())
    assert candidate_scan_deadline(failed, 60) == (now + timedelta(seconds=90)).timestamp()


def test_stale_lease_cannot_advance_a_slow_read_window(tmp_path, monkeypatch):
    import threading

    from agent_py_agent.agent.memory_store.curator_state import CuratorLeaseLostError
    service = seeded(tmp_path, 85)
    request = CuratorLeaseRequest('interval', service.config.revision(), 30, purpose='candidate_scan')
    acquired = service.committer.recover_and_acquire(request)
    old = acquired[1]['lease_id']
    entered, resume = threading.Event(), threading.Event()
    original = service.candidate_service.readonly_window
    errors = []
    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert resume.wait(5)
        return result
    monkeypatch.setattr(service.candidate_service, 'readonly_window', slow)
    def worker():
        try:
            service._pending_promotable_candidate_ids(lease_id=old)
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(5)
        release_curator_lease(service.state_store, old)
        fresh = service.committer.recover_and_acquire(request)[1]['lease_id']
    finally:
        resume.set()
        thread.join(5)
    assert not thread.is_alive() and len(errors) == 1 and isinstance(errors[0], CuratorLeaseLostError)
    assert service.state_store.load().active_lease['lease_id'] == fresh
    assert service.state_store.load().candidate_scan_cursor == {}
    release_curator_lease(service.state_store, fresh)


@pytest.mark.parametrize('prior_pages', [0, 2])
def test_promotion_failure_does_not_publish_first_or_final_page_cursor(tmp_path, monkeypatch, prior_pages):
    service = seeded(tmp_path, 85)
    now = datetime.now(timezone.utc)
    for _ in range(prior_pages):
        select_under_lease(service, now)
    before = service.state_store.load().candidate_scan_cursor
    attempted = []
    def interrupt(identifiers):
        attempted.append(tuple(identifiers))
        assert service.state_store.load().candidate_scan_cursor == before
        raise RuntimeError('exit before promotion returns')
    monkeypatch.setattr(service, '_promote_eligible', interrupt)
    assert service._resume_candidate_promotions(now).status == 'failed'
    assert service.state_store.load().candidate_scan_cursor == before
    # 冷实例沿同一原候选事实重试；失败没有提前签轮转完成，也不回放新的提取/日报。
    restarted = _service(tmp_path, _StaticStructuredBackend({}), ConversationStore(tmp_path / 'c'))
    restarted.promotion_callback = lambda candidate_id: None
    def finish(identifiers):
        assert tuple(identifiers) == attempted[0]
        assert restarted.state_store.load().candidate_scan_cursor == before
        return 0, (), ()
    monkeypatch.setattr(restarted, '_promote_eligible', finish)
    assert restarted._resume_candidate_promotions(now + timedelta(seconds=61)).status == 'succeeded'
    assert restarted.state_store.load().candidate_scan_cursor != before
    if prior_pages:
        assert restarted.state_store.load().candidate_scan_cursor['active'] is False
    assert restarted.backend.calls == 0
