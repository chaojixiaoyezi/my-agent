"""学习排除只核候选准确来源；未知来源不授权，独立健康候选继续。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.store_layout import ConversationStorage
from agent_py_agent.agent.conversation.store_threads import ThreadStore
from agent_py_agent.agent.memory_store.learning_exclusions import (
    LEARNING_SOURCE_EXCLUDED,
    candidate_exclusion_report,
    excluded_candidate_ids,
    sync_learning_exclusion_marks,
)


def row(name, *threads):
    return SimpleNamespace(candidate_id=name, status='pending_review', promotion_exclusion='',
                           source_message_refs=tuple({'thread_id': thread} for thread in threads))


def real_store(tmp_path):
    threads = ThreadStore(ConversationStorage(tmp_path / 'conversations'))
    threads._create({'thread_id': 'ignored', 'channel': 'feishu', 'channel_conversation_id': 'excluded',
                     'canonical_user_id': 'user', 'channel_user_id': 'user'})
    excluded = threads.list(limit=1)[0]
    healthy = ConversationThread(thread_id='thread-healthy', canonical_user_id='user')
    threads.write(healthy)
    return SimpleNamespace(threads=threads), excluded, healthy


def test_exact_sources_share_batch_reads_without_history_enumeration(tmp_path, monkeypatch):
    store, excluded, healthy = real_store(tmp_path)
    import json
    for value in range(300):
        historical = ConversationThread(thread_id=f'thread-history-{value}', canonical_user_id='user')
        store.threads.storage.thread_path(historical.thread_id).write_text(json.dumps(historical.to_dict()))
    store.threads.storage.thread_path('thread-unrelated-broken').write_text('{invalid')
    monkeypatch.setattr(store.threads, 'list_report', lambda **kw: pytest.fail('historical scan'))
    original, calls = store.threads.load_report, []
    def read(thread_id):
        calls.append(thread_id)
        return original(thread_id)
    monkeypatch.setattr(store.threads, 'load_report', read)
    result = candidate_exclusion_report(conversation_store=store,
        rows=[row('one', healthy.thread_id), row('two', excluded.thread_id), row('three', healthy.thread_id)],
        excluded_conversation_ids=frozenset({'excluded'}))
    assert result['excluded_candidate_ids'] == {'two'}
    assert not result['unknown_candidate_ids']
    assert calls == [healthy.thread_id, excluded.thread_id]
    assert result['checked_threads'] == 2


def test_missing_and_corrupt_sources_only_block_affected_candidates(tmp_path):
    store, excluded, healthy = real_store(tmp_path)
    store.threads.storage.thread_path(excluded.thread_id).write_text('{bad')
    result = candidate_exclusion_report(conversation_store=store,
        rows=[row('missing', 'thread-missing'), row('broken', excluded.thread_id), row('healthy', healthy.thread_id)],
        excluded_conversation_ids=frozenset({'excluded'}))
    assert result['unknown_candidate_ids'] == {'missing', 'broken'}
    assert not result['excluded_candidate_ids']
    assert {error['code'] for error in result['errors']} == {'THREAD_SOURCE_MISSING', 'THREAD_SOURCE_UNREADABLE'}


def test_submission_rechecks_canonical_instead_of_reusing_stale_scope(tmp_path):
    store, excluded, healthy = real_store(tmp_path)
    candidate = row('candidate', healthy.thread_id)
    args = dict(conversation_store=store, rows=[candidate], excluded_conversation_ids=frozenset({'excluded'}))
    assert not excluded_candidate_ids(**args)
    store.threads.write(replace(healthy, channel_bindings=excluded.channel_bindings))
    assert excluded_candidate_ids(**args) == {'candidate'}


def test_budget_unknown_never_allows_unread_sources_and_keeps_known_sibling():
    calls = []
    def read(thread_id):
        calls.append(thread_id)
        return SimpleNamespace(thread_id=thread_id, channel_bindings=()), None
    result = candidate_exclusion_report(conversation_store=SimpleNamespace(threads=SimpleNamespace(load_report=read)),
        rows=[row('known', 'one'), row('unknown', 'two'), row('same', 'one')],
        excluded_conversation_ids=frozenset({'excluded'}), max_thread_reads=1)
    assert result['unknown_candidate_ids'] == {'unknown'}
    assert calls == ['one']
    assert result['errors'][0]['code'] == 'THREAD_SOURCE_READ_BUDGET_EXHAUSTED'


def test_cancellation_propagates_and_missing_reader_is_not_empty_history():
    def read(_):
        raise InterruptedError('cancelled')
    args = dict(rows=[row('candidate', 'one')], excluded_conversation_ids=frozenset({'excluded'}))
    with pytest.raises(InterruptedError):
        candidate_exclusion_report(conversation_store=SimpleNamespace(threads=SimpleNamespace(load_report=read)), **args)
    report = candidate_exclusion_report(conversation_store=SimpleNamespace(threads=SimpleNamespace()), **args)
    assert report['unknown_candidate_ids'] == {'candidate'}
    assert report['errors'][0]['code'] == 'THREAD_SOURCE_READ_API_UNAVAILABLE'


def test_sync_uses_fair_window_and_does_not_persist_unknown(tmp_path, monkeypatch):
    store, excluded, healthy = real_store(tmp_path)
    candidates = [row('excluded', excluded.thread_id), row('missing', 'thread-missing'), row('healthy', healthy.thread_id)]
    marked, cursors = [], []
    def window(cursor, *, max_records):
        assert max_records == 128
        offset = cursor.get('position', 0)
        cursors.append(offset)
        return candidates[offset:offset + 1], (), {'position': (offset + 1) % 3}, offset == 2
    def mark(ids, *, reason):
        assert reason == LEARNING_SOURCE_EXCLUDED
        marked.extend(ids)
        return len(ids)
    service = SimpleNamespace(readonly_window=window, mark_promotion_excluded=mark)
    monkeypatch.setattr(store.threads, 'list_report', lambda **kw: pytest.fail('historical scan'))
    results = [sync_learning_exclusion_marks(conversation_store=store, candidate_service=service,
               excluded_conversation_ids=frozenset({'excluded'})) for _ in range(3)]
    assert cursors == [0, 1, 2]
    assert marked == ['excluded']
    assert results[1]['unknown_candidate_ids'] == ('missing',)
    assert results[2]['checked'] == 1


def test_unknown_only_sync_never_calls_durable_exclusion_writer():
    item = row('unknown', 'missing')
    service = SimpleNamespace(readonly_window=lambda cursor, **kw: ([item], (), {'position': 0}, True),
                              mark_promotion_excluded=lambda *a, **kw: pytest.fail('unknown persisted as excluded'))
    store = SimpleNamespace(threads=SimpleNamespace(load_report=lambda _: (None, None)))
    report = sync_learning_exclusion_marks(conversation_store=store, candidate_service=service,
                                           excluded_conversation_ids=frozenset({'excluded'}))
    assert report['marked'] == 0
    assert report['unknown_candidate_ids'] == ('unknown',)
    assert report['coverage'] == 'partial'
