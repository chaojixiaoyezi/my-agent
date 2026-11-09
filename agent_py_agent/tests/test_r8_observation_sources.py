"""观察源工作量必须随请求页/窗口收费，不随终态历史或旧线程数增长。"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.json_io import (
    append_private_jsonl_records_unlocked,
    locked_json_path,
)
from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
from agent_py_agent.agent.conversation.business_observability import runtime_source_revision
from agent_py_agent.agent.conversation.store_usage import _thread_model_usage_event
from agent_py_agent.agent.conversation.usage_index import UsageIndex
from agent_py_agent.tests import test_mega_business_api_actions as business_actions
from agent_py_agent.tests.test_business_v12_f7_observation import fixture as observation_case
from agent_py_agent.tests.test_decision_audit_controls import _decision_calls
from agent_py_agent.tests.test_v13_w0_observation_readers import background_call, seeded, wire

background_http = business_actions.background_http


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))


def _attempt_history(case, count):
    with case.runtime._runtime_connection() as conn:
        template = dict(conn.execute('SELECT * FROM agent_attempts WHERE attempt_id=?', (case.root['attempt_id'],)).fetchone())
        columns = list(template)
        rows = []
        for number in range(count):
            row = dict(template, attempt_id=f'r8-attempt-{number:08}', attempt_generation=number + 10)
            rows.append(tuple(row[key] for key in columns))
        conn.executemany('INSERT INTO agent_attempts(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', rows)
        conn.commit()
        return conn.execute('SELECT max(rowid) FROM agent_attempts').fetchone()[0]


@contextmanager
def _sql_work(repo, monkeypatch):
    total = [0]
    original = repo._runtime_connection
    @contextmanager
    def measured():
        with original() as conn:
            def tick():
                total[0] += 1
                return 0
            conn.set_progress_handler(tick, 1)
            try:
                yield conn
            finally:
                conn.set_progress_handler(None, 0)
    with monkeypatch.context() as patch:
        patch.setattr(repo, '_runtime_connection', measured)
        yield total


@pytest.mark.parametrize('history', [100, 4000])
def test_execution_deep_page_vm_steps_are_bounded(tmp_path, monkeypatch, record_property, history):
    case = observation_case(tmp_path)
    last = _attempt_history(case, history)
    params = {'limit': 10}
    scope = case.service.scope_digest('executions.list', params, case.actor)
    cursor = case.service.cursor(scope, {'revision': runtime_source_revision(case.runtime.db_path), 'after': last - 3})
    with _sql_work(case.service.runtime, monkeypatch) as work:
        result = case.service.query('executions.list', dict(params, cursor=cursor), actor=case.actor)
    assert len(result['items']) == 3 and result['next_cursor'] is None
    assert work[0] < 2500
    record_property('history_attempts', history)
    record_property('deep_page_sqlite_vm_steps', work[0])


def test_sparse_authorization_does_not_fill_a_page_by_scanning_history(tmp_path, monkeypatch):
    case = observation_case(tmp_path)
    _attempt_history(case, 300)
    actor = BusinessActor(case.owner, 'sparse', case.service,
        lambda action, facts: not facts.get('attempt_id') or facts['attempt_id'] == 'r8-attempt-00000299')
    with _sql_work(case.service.runtime, monkeypatch) as work:
        result = case.service.query('executions.list', {'limit': 5}, actor=actor)
    assert result['items'] == [] and result['next_cursor']
    assert work[0] < 3000
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        case.service.query('executions.list', {'limit': 5, 'cursor': result['next_cursor'],
                                              'filter': {'run_id': 'root-run'}}, actor=actor)
    with case.runtime._runtime_connection() as conn:
        conn.execute("UPDATE agent_attempts SET status='done' WHERE attempt_id=?", (case.root['attempt_id'],))
        conn.commit()
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        case.service.query('executions.list', {'cursor': result['next_cursor']}, actor=actor)


def _legacy_usage(case, count, *, stamp=1000):
    store = case.store.model_usage
    rows = [_thread_model_usage_event({'event_id': f'r8-usage-{number}', 'thread_id': case.task.target_thread_id,
            'request_id': f'r8-request-{number}', 'run_id': 'root-run', 'task_id': case.task.task_id,
            'source': 'test', 'model_calls': _decision_calls(finished=1, timed_out=0, input_tokens=1),
            'now': stamp}) for number in range(count)]
    path = store._path(case.task.target_thread_id)
    with locked_json_path(path):
        append_private_jsonl_records_unlocked(path, [row.to_dict() for row in rows], sort_keys=True)
    return rows


def _finish_prepare(store, *, limit=4, maximum=1024):
    reports = []
    for _ in range(maximum):
        report = store.prepare_window_sources(limit=limit)
        assert report['sources_examined'] <= limit
        reports.append(report)
        if report['coverage'] == 'complete':
            return reports
    pytest.fail('usage source projection did not converge under bounded maintenance')


def test_usage_legacy_prepare_survives_store_recreation_and_limits_rows(tmp_path, monkeypatch):
    case = observation_case(tmp_path)
    _legacy_usage(case, 400)
    reads = []
    original = UsageIndex.prepare
    def bounded(index, conn, **kwargs):
        result = original(index, conn, **kwargs)
        reads.append(index.last_work['events_read'])
        assert index.last_work['events_read'] <= 128
        return result
    monkeypatch.setattr(UsageIndex, 'prepare', bounded)
    from agent_py_agent.agent.conversation.store_usage import ModelUsageStore
    for _ in range(1024):
        # 每片换对象，cookie必须来自SQLite meta而不是进程对象/64-owner LRU。
        current = ModelUsageStore(case.store.model_usage._directory, require_thread=case.store.threads.require,
                                 update_thread_atomic=case.store.threads.update_atomic)
        report = current.prepare_window_sources(limit=2)
        assert report['sources_examined'] <= 2
        if report['coverage'] == 'complete':
            break
    else:
        pytest.fail('persistent source directory cursor did not finish')
    assert reads and max(reads) <= 128
    plan = current.window_source_plan(since=999, until=1001)
    assert [identifier for identifier, _ in plan.rows()] == [case.task.target_thread_id]
    events, errors = current.events_window_report(case.task.target_thread_id, since=999, until=1001)
    assert not errors and len(list(events)) == 400


@pytest.mark.parametrize('history', [100, 4000])
def test_usage_time_window_reads_only_requested_canonical_rows(tmp_path, monkeypatch, record_property, history):
    case = observation_case(tmp_path)
    rows = _legacy_usage(case, history)
    path = case.store.model_usage._path(case.task.target_thread_id)
    window = [replace(rows[0], event_id=f'window-{number}', request_id=f'window-request-{number}', created_at=2000)
              for number in range(300)]
    with locked_json_path(path):
        append_private_jsonl_records_unlocked(path, [event.to_dict() for event in window], sort_keys=True)
    _finish_prepare(case.store.model_usage)
    count = [0]
    original = UsageIndex.event
    def read(index, conn, identifier):
        count[0] += 1
        return original(index, conn, identifier)
    monkeypatch.setattr(UsageIndex, 'event', read)
    events, errors = case.store.model_usage.events_window_report(case.task.target_thread_id, since=1999, until=2001)
    assert not errors and sum(1 for _ in events) == 300
    assert count[0] == 300
    record_property('older_usage_events', history)
    record_property('requested_window_events', 300)
    record_property('canonical_rows_read', count[0])


def test_usage_pending_append_and_stale_source_cas_remain_unknown(tmp_path, monkeypatch):
    case = observation_case(tmp_path)
    store = case.store.model_usage
    store.append_once({'event_id': 'initial', 'thread_id': case.task.target_thread_id, 'request_id': 'initial',
                       'model_calls': _decision_calls(finished=1, timed_out=0, input_tokens=1), 'now': 1000})
    _finish_prepare(store)
    stale = store._usage_source_snapshot(case.task.target_thread_id)
    arrived, release = threading.Event(), threading.Event()
    original = store._refresh_usage_source
    failures = []
    def paused(identifier, before, token):
        arrived.set()
        assert release.wait(10)
        return original(identifier, before, token)
    monkeypatch.setattr(store, '_refresh_usage_source', paused)
    def run():
        try:
            store.append_once({'event_id': 'next', 'thread_id': case.task.target_thread_id, 'request_id': 'next',
                               'model_calls': _decision_calls(finished=1, timed_out=0, input_tokens=1), 'now': 2000})
        except BaseException as error:
            failures.append(error)
    worker = threading.Thread(target=run)
    worker.start()
    assert arrived.wait(10)
    try:
        with pytest.raises(ValueError, match='incomplete'):
            store.window_source_plan(since=0, until=3000)
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and not failures
    _finish_prepare(store)
    sources = store._source_index()
    before = sources.stamps()
    token = sources.begin(case.task.target_thread_id)
    sources.update(case.task.target_thread_id, stale, before_stamp=before, token=token, store=store)
    with pytest.raises(ValueError, match='incomplete'):
        store.window_source_plan(since=0, until=3000)
    _finish_prepare(store)
    assert list(store.window_source_plan(since=1999, until=2001).rows())


def test_real_http_tool_source_pages_and_large_usage_window(background_http, monkeypatch):
    services, task, runtime, run, operations, _ = seeded(background_http)
    with runtime._runtime_connection() as conn:
        template = dict(conn.execute('SELECT * FROM tool_operations WHERE operation_id=?', (operations[0],)).fetchone())
        columns = list(template)
        rows = [tuple(dict(template, operation_id=f'r8-operation-{i:04}')[key] for key in columns) for i in range(70)]
        conn.executemany('INSERT INTO tool_operations(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', rows)
        conn.commit()
    wire(services, runtime)
    cursor, seen = None, []
    for _ in range(16):
        params = {'limit': 9}
        if cursor is not None:
            params['cursor'] = cursor
        status, envelope = background_call(background_http, 'tool_calls.list', params)
        assert status == 200, envelope
        data = envelope['data']
        seen.extend(row['operation_id'] for row in data['items'])
        cursor = data['next_cursor']
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 72
    store = services.host.conversation_store
    case = SimpleNamespace(store=store, task=task)
    _legacy_usage(case, 300, stamp=2000)
    _finish_prepare(store.model_usage)
    from agent_py_agent.agent.observability import concurrency_metrics
    metrics = concurrency_metrics._metrics()
    metrics.llm_inflight.set(0, labels={'owner_id': services.owner_id})
    wire(services, runtime)
    monkeypatch.setattr(store.threads, 'list_report', lambda **kwargs: pytest.fail('usage query scanned all owner threads'))
    filters = {'since': datetime.fromtimestamp(1999, timezone.utc).isoformat(),
               'until': datetime.fromtimestamp(2001, timezone.utc).isoformat()}
    try:
        status, envelope = background_call(background_http, 'usage.get', {'filter': filters})
        assert status == 200 and envelope['data']['actual']['input_tokens'] == 300, envelope
    finally:
        with metrics.llm_inflight._lock:
            metrics.llm_inflight._vals.pop((('owner_id', services.owner_id),), None)


def _append_in_child(root, thread_id, arrived, release):
    from agent_py_agent.agent.conversation.store import ConversationStore
    store = ConversationStore(Path(root))
    usage = store.model_usage
    original = usage._refresh_usage_source
    def paused(identifier, before, token):
        arrived.set()
        if not release.wait(10):
            raise RuntimeError('parent did not release usage writer')
        return original(identifier, before, token)
    usage._refresh_usage_source = paused
    usage.append_once({'event_id': 'child-event', 'thread_id': thread_id, 'request_id': 'child-request',
                       'model_calls': _decision_calls(finished=1, timed_out=0, input_tokens=1), 'now': 2000})


def test_usage_cross_process_pending_publication_is_not_an_empty_complete_scope(tmp_path):
    import multiprocessing
    case = observation_case(tmp_path)
    usage = case.store.model_usage
    usage.append_once({'event_id': 'initial', 'thread_id': case.task.target_thread_id, 'request_id': 'initial',
                       'model_calls': _decision_calls(finished=1, timed_out=0, input_tokens=1), 'now': 1000})
    _finish_prepare(usage)
    context = multiprocessing.get_context('spawn')
    arrived, release = context.Event(), context.Event()
    child = context.Process(target=_append_in_child, args=(str(case.store.storage.root), case.task.target_thread_id, arrived, release))
    child.start()
    try:
        assert arrived.wait(10)
        with pytest.raises(ValueError, match='incomplete'):
            usage.window_source_plan(since=1999, until=2001)
    finally:
        release.set()
        child.join(15)
    assert child.exitcode == 0
    _finish_prepare(usage)
    rows, errors = usage.events_window_report(case.task.target_thread_id, since=1999, until=2001)
    assert not errors and [row.event_id for row in rows] == ['child-event']


def test_usage_source_cookie_progresses_across_more_than_64_owners(tmp_path):
    from agent_py_agent.agent.conversation.store import ConversationStore
    sources = []
    for number in range(66):
        store = ConversationStore(tmp_path / str(number) / 'conversations')
        thread = store.threads.get_or_create({'canonical_user_id': 'isolated', 'channel': 'internal'})
        event = _thread_model_usage_event({'event_id': 'legacy', 'thread_id': thread.thread_id, 'request_id': 'legacy',
            'model_calls': _decision_calls(finished=1, timed_out=0, input_tokens=1), 'now': 1000})
        path = store.model_usage._path(thread.thread_id)
        with locked_json_path(path):
            append_private_jsonl_records_unlocked(path, [event.to_dict()], sort_keys=True)
        sources.append(store)
    remaining = set(range(len(sources)))
    for _ in range(80):
        for number in tuple(remaining):
            report = sources[number].model_usage.prepare_window_sources(limit=1)
            assert report['sources_examined'] <= 1
            if report['coverage'] == 'complete':
                remaining.remove(number)
        if not remaining:
            break
    assert not remaining, 'owner rotation must not repeatedly lose cold inventory progress'


def test_usage_source_busy_does_not_block_canonical_append_or_claim_complete(tmp_path):
    import time
    case = observation_case(tmp_path)
    usage = case.store.model_usage
    usage.append_once({'event_id': 'initial', 'thread_id': case.task.target_thread_id, 'request_id': 'initial',
                       'model_calls': _decision_calls(finished=1, timed_out=0, input_tokens=1), 'now': 1000})
    _finish_prepare(usage)
    index = usage._source_index()
    with index.write_connection():
        started = time.monotonic()
        saved = usage.append_once({'event_id': 'while-busy', 'thread_id': case.task.target_thread_id, 'request_id': 'while-busy',
                                   'model_calls': _decision_calls(finished=1, timed_out=0, input_tokens=1), 'now': 2000})
        assert saved.event_id == 'while-busy' and time.monotonic() - started < 1
        assert usage.prepare_window_sources(limit=1)['cursor']['busy']
    with pytest.raises(ValueError, match='incomplete'):
        usage.window_source_plan(since=1999, until=2001)
    _finish_prepare(usage)
    assert list(usage.window_source_plan(since=1999, until=2001).rows())


@pytest.mark.parametrize('history', [100, 4000])
def test_tool_deep_page_vm_steps_are_bounded(tmp_path, monkeypatch, record_property, history):
    from agent_py_agent.agent.gateway_parts.business_api_v13_readers import (
        default_observation_readers,
    )
    case = observation_case(tmp_path)
    initial = case.runtime.create_tool_operation(agent_run_id=case.root['agent_run_id'],
        attempt_id=case.root['attempt_id'], operation_type='read_file')
    with case.runtime._runtime_connection() as conn:
        template = dict(conn.execute('SELECT * FROM tool_operations WHERE operation_id=?', (initial['operation_id'],)).fetchone())
        columns = list(template)
        rows = [tuple(dict(template, operation_id=f'operation-{number:08}')[key] for key in columns) for number in range(history)]
        conn.executemany('INSERT INTO tool_operations(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', rows)
        conn.commit()
        last = conn.execute('SELECT max(rowid) FROM tool_operations').fetchone()[0]
    case.service.readers = default_observation_readers(tmp_path, case.owner, case.store, case.runtime)
    source = case.service.readers['tool_calls'].__self__
    params = {'limit': 10}
    scope = case.service.scope_digest('tool_calls.list', params, case.actor)
    cursor = case.service.cursor(scope, {'revision': runtime_source_revision(case.runtime.db_path), 'after': last - 3})
    with _sql_work(source.repo, monkeypatch) as work:
        result = case.service.query('tool_calls.list', dict(params, cursor=cursor), actor=case.actor)
    assert len(result['items']) == 3 and result['next_cursor'] is None
    assert work[0] < 3000
    record_property('historical_operations', history)
    record_property('deep_tool_page_sqlite_vm_steps', work[0])


def test_usage_unpublished_temp_cleanup_is_bounded_and_does_not_block_scope_forever(tmp_path):
    import os
    import time
    case = observation_case(tmp_path)
    usage = case.store.model_usage
    usage.append_once({'event_id': 'initial', 'thread_id': case.task.target_thread_id, 'request_id': 'initial',
                       'model_calls': _decision_calls(finished=1, timed_out=0, input_tokens=1), 'now': 1000})
    _finish_prepare(usage)
    directory = usage._source_index().pending_directory
    for number in range(130):
        path = directory / ('.' + f'{number:032x}' + '.json.' + 'a' * 32 + '.tmp')
        path.write_bytes(b'unpublished')
        os.utime(path, (time.time() - 600, time.time() - 600))
    with pytest.raises(ValueError):
        usage.window_source_plan(since=0, until=3000)
    reports = _finish_prepare(usage, limit=4)
    assert all(report['sources_examined'] <= 4 for report in reports)
    assert not tuple(directory.iterdir())
    assert list(usage.window_source_plan(since=0, until=3000).rows())


@pytest.mark.parametrize('relative', ['conversations', 'sessions', 'sessions/conversations',
    'workspace/runtime/workspaces/main/conversations'])
def test_usage_projection_and_intents_are_host_readonly(tmp_path, relative):
    from agent_py_agent.agent.path_access_policy import host_state_for_path
    base = tmp_path / 'home'
    owner = base / 'owners/providers/test/users/u'
    path = owner / relative / '.model_usage_sources/pending/uncreated.json'
    assert host_state_for_path(path, base) is not None


def test_partial_execution_window_never_claims_a_missing_tree_root_is_absent(tmp_path):
    case = observation_case(tmp_path)
    _attempt_history(case, 300)
    result = case.service.query('agent_tree.get', {'space_id': 'space-a'}, actor=case.actor)
    assert result['coverage'] == 'partial' and result['has_more_descendants'] is True
    with pytest.raises(BusinessError, match='HOST_NOT_READY'):
        case.service.query('agent_tree.get', {'space_id': 'space-a', 'root_agent_id': 'not-in-window'}, actor=case.actor)


def test_readonly_runtime_recovery_intent_is_unknown_not_empty_complete(tmp_path):
    from agent_py_agent.agent.runtime_db.recovery_guard import request_runtime_db_recovery
    case = observation_case(tmp_path)
    request_runtime_db_recovery(case.runtime.db_path)
    result = case.service.query('executions.list', {}, actor=case.actor)
    assert result['items'] == [] and result['coverage'] == 'unknown'


@pytest.mark.parametrize('kind', ['events', 'sources'])
def test_usage_factory_close_failure_keeps_primary_and_unclosed_proof(tmp_path, monkeypatch, kind):
    import sqlite3

    from agent_py_agent.agent.conversation import usage_index, usage_sources
    primary = sqlite3.DatabaseError('factory schema failure')
    cleanup = OSError('close not proved')
    original = sqlite3.connect
    opened = []
    class Connection(sqlite3.Connection):
        def execute(self, *_args, **_kwargs):
            raise primary
        def close(self):
            raise cleanup
    def connect(*_args, **_kwargs):
        connection = original(':memory:', factory=Connection)
        opened.append(connection)
        return connection
    module = usage_index if kind == 'events' else usage_sources
    instance = (usage_index.UsageIndex(tmp_path / 'usage/thread.jsonl', 'thread') if kind == 'events'
                else usage_sources.UsageSourceIndex(tmp_path / 'model_usage'))
    instance.path.parent.mkdir(parents=True, exist_ok=True)
    instance.path.touch()
    monkeypatch.setattr(module.sqlite3, 'connect', connect)
    try:
        with pytest.raises(sqlite3.DatabaseError) as caught:
            instance._open()
        assert caught.value is primary and primary.sqlite_cleanup_error is cleanup
        assert primary.sqlite_unclosed_factory_connection is opened[0]
    finally:
        for connection in opened:
            sqlite3.Connection.close(connection)


def test_usage_projection_rebuild_proof_requires_existing_canonical_directory(tmp_path):
    import os

    from agent_py_agent.agent.conversation.usage_sources import UsageSourceIndex
    directory = tmp_path / 'model_usage'
    index = UsageSourceIndex(directory)
    assert index._projection_rebuild_proof() is None
    directory.mkdir()
    before = index._projection_rebuild_proof()
    assert before['directory'] and before['archive'] is None
    (directory / 'canonical.jsonl').write_text('record')
    # 同一 Linux 文件系统时间刻度内 mkdir/write 可能保留相同目录时间；
    # 此证明只准许从现存 canonical 重建空投影，随后仍须 prepare；
    # 它不保证辨认每次叶内容更新。这里显式推进测试中的结构化来源快照。
    os.utime(directory, ns=(directory.stat().st_atime_ns, before['directory'][2] + 2_000_000_000))
    assert index._projection_rebuild_proof() != before


def test_usage_projection_rebuild_proof_rejects_replaced_directory_with_same_mtime(tmp_path):
    import os

    from agent_py_agent.agent.conversation.usage_sources import UsageSourceIndex
    directory = tmp_path / 'model_usage'
    directory.mkdir()
    index = UsageSourceIndex(directory)
    before = index._projection_rebuild_proof()
    # 保留旧目录的 inode，排除文件系统立即复用 inode 对本断言的干扰。
    directory.rename(tmp_path / 'removed_model_usage')
    assert index._projection_rebuild_proof() is None
    directory.mkdir()
    os.utime(directory, ns=(directory.stat().st_atime_ns, before['directory'][2]))
    after = index._projection_rebuild_proof()
    assert after['directory'][2] == before['directory'][2]
    assert after['directory'][:2] != before['directory'][:2]
    assert after != before


def test_usage_rebuild_proof_retains_cold_manifest_when_active_leaf_is_deleted(tmp_path):
    from agent_py_agent.agent.conversation.thread_ledger_segments import archive_dir_for
    active = tmp_path / 'model_usage/thread.jsonl'
    index = UsageIndex(active, 'thread')
    assert index._projection_rebuild_proof() is None
    archive = archive_dir_for(active)
    archive.mkdir(parents=True)
    (archive / 'manifest.json').write_text('{}')
    proof = index._projection_rebuild_proof()
    assert proof['active'] is None and proof['archive'][1] is not None
