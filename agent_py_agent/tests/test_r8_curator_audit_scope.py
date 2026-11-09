"""普通审计窗口只核候选线程，未知不消费，已识别坏线程保留原隔离合同。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.memory_store import curator_inputs as inputs
from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig, MemoryCuratorState


@pytest.fixture
def audit_case(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'conversation')
    thread = store.threads.create()
    audit = tmp_path / 'audit'
    audit.mkdir()
    # 仅隔离另一分片负责的消息发现，审计扫描/线程规范点查/排除规则均真实。
    monkeypatch.setattr(inputs, '_collect_messages', lambda **_kwargs: ([], []))
    return store, thread, audit


def _write(audit, rows):
    (audit / '2026-11-09.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))


def _collect(case, *, excluded=frozenset()):
    store, _thread, audit = case
    return inputs.collect_curator_inputs(message_source=inputs.CuratorMessageSource(store, excluded),
        audit_dir=audit, state=MemoryCuratorState(), config=MemoryCuratorConfig())


def _no_full_listing(monkeypatch, store):
    monkeypatch.setattr(store.threads, 'list_report', lambda **_kwargs: pytest.fail('audit enumerated all threads'))


def test_empty_audit_does_not_enumerate_or_load_threads(audit_case, monkeypatch):
    store, _thread, _audit = audit_case
    _no_full_listing(monkeypatch, store)
    monkeypatch.setattr(store.threads, 'load_report', lambda *_args: pytest.fail('empty audit loaded thread'))
    batch = _collect(audit_case)
    assert not batch.audit_events and not batch.load_errors and not batch.skipped_audit_cursor


def test_only_distinct_candidate_ids_are_read_and_host_event_is_preserved(audit_case, monkeypatch):
    store, thread, audit = audit_case
    other = store.threads.create()
    _write(audit, [{'event_id': 'first', 'thread_id': thread.thread_id},
                   {'event_id': 'second', 'thread_id': thread.thread_id}, {'event_id': 'host-event'}])
    _no_full_listing(monkeypatch, store)
    original = store.threads.load_report
    seen = []

    def observed(tid):
        seen.append(tid)
        assert tid != other.thread_id
        return original(tid)

    monkeypatch.setattr(store.threads, 'load_report', observed)
    batch = _collect(audit_case)
    assert seen == [thread.thread_id]
    assert [row.event_id for row in batch.audit_events] == ['first', 'second', 'host-event']
    assert not batch.load_errors and not batch.skipped_audit_cursor


@pytest.mark.parametrize('metadata', [{'learning_excluded': True}, {'bot_mode': 'query_readonly'}])
def test_candidate_policy_exclusion_uses_exact_current_thread(audit_case, monkeypatch, metadata):
    store, thread, audit = audit_case
    store.threads.update_atomic(thread.thread_id, lambda row: replace(row, metadata=metadata))
    _write(audit, [{'event_id': 'excluded', 'thread_id': thread.thread_id}])
    _no_full_listing(monkeypatch, store)
    batch = _collect(audit_case)
    assert not batch.audit_events and batch.skipped_audit_cursor == 'excluded'
    assert not batch.load_errors
    store.threads.update_atomic(thread.thread_id, lambda row: replace(row, metadata={}))
    assert [row.event_id for row in _collect(audit_case).audit_events] == ['excluded']


def test_missing_thread_blocks_consumption_until_same_canonical_source_recovers(audit_case, monkeypatch):
    store, thread, audit = audit_case
    _write(audit, [{'event_id': 'host-before'}, {'event_id': 'missing', 'thread_id': thread.thread_id}])
    store.storage.thread_path(thread.thread_id).unlink()
    _no_full_listing(monkeypatch, store)
    batch = _collect(audit_case)
    assert not batch.audit_events and not batch.skipped_audit_cursor
    assert batch.load_errors[0]['error_code'] == 'AUDIT_THREAD_UNAVAILABLE'
    store.threads.write(thread)
    batch = _collect(audit_case)
    assert [row.event_id for row in batch.audit_events] == ['host-before', 'missing']
    assert not batch.load_errors


def test_identified_bad_thread_keeps_original_quarantine_semantics(audit_case, monkeypatch):
    store, thread, audit = audit_case
    store.storage.thread_path(thread.thread_id).write_bytes(b'{')
    _write(audit, [{'event_id': 'bad', 'thread_id': thread.thread_id}])
    _no_full_listing(monkeypatch, store)
    batch = _collect(audit_case)
    assert not batch.audit_events and batch.skipped_audit_cursor == 'bad' and not batch.load_errors


@pytest.mark.parametrize('failure', ['anonymous', 'wrong-error-id', 'wrong-thread'])
def test_unresolved_or_foreign_point_read_never_consumes_audit(audit_case, monkeypatch, failure):
    store, thread, audit = audit_case
    other = store.threads.create()
    _write(audit, [{'event_id': 'blocked', 'thread_id': thread.thread_id}])
    result = {'anonymous': (None, {}), 'wrong-error-id': (None, {'thread_id': other.thread_id}),
              'wrong-thread': (other, None)}[failure]
    monkeypatch.setattr(store.threads, 'load_report', lambda _tid: result)
    _no_full_listing(monkeypatch, store)
    batch = _collect(audit_case)
    assert not batch.audit_events and not batch.skipped_audit_cursor and batch.load_errors


def test_unreadable_exclusion_configuration_reads_no_audit(audit_case, monkeypatch):
    store, thread, audit = audit_case
    _write(audit, [{'event_id': 'blocked', 'thread_id': thread.thread_id}])
    _no_full_listing(monkeypatch, store)
    monkeypatch.setattr(inputs, '_collect_audit', lambda *_args, **_kwargs: pytest.fail('unreadable policy read audit'))
    batch = _collect(audit_case, excluded=None)
    assert not batch.audit_events and not batch.skipped_audit_cursor


def test_channel_exclusion_is_checked_only_for_candidate_binding(audit_case, monkeypatch):
    store, _thread, audit = audit_case
    thread = store.threads.get_or_create({'canonical_user_id': 'user', 'channel': 'internal',
        'channel_user_id': 'user', 'channel_conversation_id': 'excluded-room'})
    _write(audit, [{'event_id': 'excluded', 'thread_id': thread.thread_id}])
    _no_full_listing(monkeypatch, store)
    batch = _collect(audit_case, excluded=frozenset({'excluded-room'}))
    assert not batch.audit_events and batch.skipped_audit_cursor == 'excluded'
    assert not batch.load_errors


def test_scope_lookups_never_extend_beyond_selected_audit_page(audit_case, monkeypatch):
    store, thread, audit = audit_case
    _write(audit, [{'event_id': 'first', 'thread_id': thread.thread_id},
                   {'event_id': 'later', 'thread_id': 'unresolved-later-thread'}])
    _no_full_listing(monkeypatch, store)
    original = store.threads.load_report
    seen = []

    def observed(tid):
        seen.append(tid)
        assert tid == thread.thread_id
        return original(tid)

    monkeypatch.setattr(store.threads, 'load_report', observed)
    batch = inputs.collect_curator_inputs(message_source=inputs.CuratorMessageSource(store), audit_dir=audit,
        state=MemoryCuratorState(), config=MemoryCuratorConfig(batch_message_limit=1))
    assert seen == [thread.thread_id]
    assert [row.event_id for row in batch.audit_events] == ['first']
    assert not batch.load_errors
