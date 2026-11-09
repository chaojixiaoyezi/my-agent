"""线程待处理观察按活动集读取，不被普通近期事件挤掉。"""
from __future__ import annotations

from agent_py_agent.agent.conversation import ConversationStore


def _store(tmp_path):
    store = ConversationStore(tmp_path / 'conversations')
    first = store.threads.create(canonical_user_id='first')
    second = store.threads.create(canonical_user_id='second')
    return store, first, second


def _append(store, thread, number, *, needs_main=True):
    return store.observations.append({
        'thread_id': thread.thread_id, '_observation_id': f'event-{thread.thread_id}-{number}',
        'event_type': 'subagent_runner_finished' if needs_main else 'progress',
        'requires_main_agent': needs_main, 'now': float(number + 1),
        'root_task_id': 'old-root', 'parent_agent_id': 'old-root', 'source_agent_id': f'child-{number}',
        'metadata': {'task_id': f'child-{number}', 'completion_schema_version': 'subagent-completion.v1',
                     'status': 'DONE', 'completion_message': '已完成'},
    })


def _ready(store):
    for _ in range(32):
        report = store.observations._index.maintenance()
        if report['coverage'] == 'complete':
            return
    raise AssertionError(report)


def test_completion_survives_ordinary_tail_and_ack_removes_only_exact_thread_event(tmp_path):
    store, first, second = _store(tmp_path)
    completion = _append(store, first, 0)
    other = _append(store, second, 0)
    for number in range(1, 140):
        _append(store, first, number, needs_main=False)
    _ready(store)
    cold = ConversationStore(store.storage.root, initialize=False)
    tail, errors, _coverage = cold.observations.recent_window_report(first.thread_id)
    assert not errors and completion.observation_id not in {event.observation_id for event in tail}
    events, errors = cold.observations.unhandled_requiring_main_report(first.thread_id)
    assert not errors and events == [completion]
    assert cold.observations._index.last_work['candidate_rows'] == 1
    assert cold.observations._index.last_work['json_decodes'] == 1
    cold.observations.mark_handled([completion.observation_id], now=1000)
    assert cold.observations.unhandled_requiring_main_report(first.thread_id) == ([], [])
    assert cold.observations.unhandled_requiring_main_report(second.thread_id) == ([other], [])


def test_thread_filter_is_applied_before_limit_and_zero_keeps_all_active(tmp_path):
    store, first, second = _store(tmp_path)
    _append(store, second, 0)
    pending = [_append(store, first, number) for number in range(1, 151)]
    _ready(store)
    events, errors = store.observations.unhandled_requiring_main_report(first.thread_id, limit=1)
    assert not errors and events == pending[:1]
    events, errors = store.observations.unhandled_requiring_main_report(first.thread_id)
    assert not errors and events == pending
    assert store.observations._index.last_work['candidate_rows'] == 150
    assert store.observations._index.last_work['json_decodes'] == 150
    # 旧global/wake读取语义保持，thread只增加可选交集。
    assert len(store.observations._index.pending(0)) == 151
    assert len(store.observations._index.pending(0, wake='')) == 151
    assert store.observations._index.pending(0, wake='', thread_id=first.thread_id) == pending


def test_cold_partial_never_claims_complete_empty_and_recovery_continues(tmp_path):
    store, first, _second = _store(tmp_path)
    for number in range(160):
        _append(store, first, number, needs_main=False)
    completion = _append(store, first, 161)
    cold = ConversationStore(store.storage.root, initialize=False)
    events, errors = cold.observations.unhandled_requiring_main_report(first.thread_id)
    assert events == [] and errors
    assert errors[0]['error_type'] == 'ObservationIndexIncomplete'
    assert errors[0]['coverage'] == 'partial'
    _ready(cold)
    assert cold.observations.unhandled_requiring_main_report(first.thread_id) == ([completion], [])


def test_bad_legacy_confirmation_stays_typed_unknown_not_empty(tmp_path):
    store, first, _second = _store(tmp_path)
    _append(store, first, 1)
    store.storage.observation_handled_path.write_bytes(b'{broken')
    events, errors = store.observations.unhandled_requiring_main_report(first.thread_id)
    assert events == [] and errors
    assert errors[0]['error_type'] == 'ObservationIndexError'
    assert errors[0]['coverage'] == 'partial'
    assert store.storage.observation_handled_path.read_bytes() == b'{broken'


def test_thread_projection_never_returns_foreign_canonical_event(tmp_path):
    store, first, second = _store(tmp_path)
    foreign = _append(store, second, 1)
    _ready(store)
    index = store.observations._index
    with index._connection() as conn:
        conn.execute('UPDATE pending SET thread=? WHERE id=?', (first.thread_id, foreign.observation_id))
    events, errors = store.observations.unhandled_requiring_main_report(first.thread_id)
    assert events == [] and errors[0]['error_type'] == 'ObservationIndexIncomplete'
    assert errors[0]['coverage'] == 'partial'
    _ready(store)
    assert store.observations.unhandled_requiring_main_report(first.thread_id) == ([], [])
    assert store.observations.unhandled_requiring_main_report(second.thread_id) == ([foreign], [])
