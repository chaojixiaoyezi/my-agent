"""W1 调用记录的状态、锁、owner、持久化与有界读取行为回归。"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from itertools import combinations
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.business_spaces import gap158_w1_llm_records as records
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallAdmissionClosure,
    ModelCallFailureParams,
    ModelCallFinishParams,
    ModelCallLedger,
    ModelCallLedgerOptions,
    ModelCallProviderAttemptParams,
    ModelCallRecord,
    ModelCallStartedParams,
    ModelCallTimeoutParams,
    close_model_call_admission,
)
from agent_py_agent.agent.gateway_parts.business_api import bind_business_context
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.runtime_db.schema import runtime_db_path


def _host(tmp_path, owner='alice', *, enabled=True, max_rows=50000):
    return SimpleNamespace(
        home_paths=SimpleNamespace(owner_id=owner, owner_home_dir=tmp_path / owner),
        config=SimpleNamespace(business_spaces_enabled=True, llm_call_records_enabled=enabled,
                               llm_call_records_max_rows=max_rows),
        _current_task_attributes={'task_id': 'task-live', 'role': 'worker', 'thread_id': 'thread-live'},
    )


def _call(host, action='list', params=None, *, authorize=None, clock=lambda: 1700000000.):
    context = bind_business_context(host, authorize=authorize or (lambda _a, _f: True))
    return V13Call('llm_records.' + action, params or {}, 'req', host.home_paths.owner_id,
                   context.subject_id, context, SimpleNamespace(owner_id=host.home_paths.owner_id),
                   host.home_paths.owner_home_dir, clock=clock)


def _record(call_id='call-1', **kwargs):
    values = dict(call_id=call_id, backend='fake', model='model-a', input_tokens=12,
                  accounted_input_tokens=10, output_tokens=5, cached_input_tokens=3,
                  status='finished', started_at=100., finished_at=102., total_latency_seconds=2.,
                  metadata={'purpose': 'openai_compat', 'profile_id': 'profile-a'})
    values.update(kwargs)
    return ModelCallRecord(**values)


def _seed(conn, owner, size, *, task='task-a', start=0):
    conn.executemany(
        'INSERT INTO llm_call_records (record_id,call_id,owner_id,task_id,model,status,'
        'input_tokens,output_tokens,cached_tokens,latency_ms,started_at,finished_at,created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        ((f'{owner}-{i:07}', f'call-{owner}-{i:07}', owner, task, 'model-a', 'completed',
          10, 5, 3, 2000, float(i), float(i + 2), float(i // 10))
         for i in range(start, start + size)),
    )


@pytest.mark.parametrize('terminal', ['finished', 'failed', 'timed_out'])
def test_sink_once_per_first_terminal_and_late_provider_event(terminal):
    seen = []
    ledger = ModelCallLedger(ModelCallLedgerOptions(record_sink=seen.append))
    ledger.started(ModelCallStartedParams('one', 'fake', 'm', 4))
    if terminal == 'finished':
        ledger.finished(ModelCallFinishParams('one', output_tokens=2))
    elif terminal == 'failed':
        ledger.failed(ModelCallFailureParams('one', 'ToolCancelled'))
    else:
        ledger.timeout(ModelCallTimeoutParams('one', 2, 'wall_clock'))
    ledger.finished(ModelCallFinishParams('one', output_tokens=100))
    ledger.failed(ModelCallFailureParams('one', 'late'))
    ledger.timeout(ModelCallTimeoutParams('one', 3, 'wall_clock'))
    ledger.provider_attempt(ModelCallProviderAttemptParams('one', 'late-http', 'finished'))
    assert [r.status for r in seen] == [terminal]
    assert ledger.records()[0].status == terminal


def test_sink_error_is_logged_without_secret_and_does_not_fail_call(caplog):
    def broken(_record):
        raise RuntimeError('secret-body-must-not-be-logged')

    ledger = ModelCallLedger(ModelCallLedgerOptions(record_sink=broken))
    ledger.started(ModelCallStartedParams('one', 'fake', 'm', 4))
    assert ledger.finished(ModelCallFinishParams('one', output_tokens=2)).status == 'finished'
    assert 'record sink failed (RuntimeError)' in caplog.text
    assert 'secret-body-must-not-be-logged' not in caplog.text


@pytest.mark.parametrize('shutdown', [False, True])
def test_sink_is_outside_all_ledger_locks_and_can_reenter(shutdown):
    observed = threading.Event()
    threads = []
    calls = []

    def sink(record):
        # 另一个线程能读账本才真正证明锁已释放，RLock 同线程重入不足以证明。
        worker = threading.Thread(target=lambda: (ledger.records(), observed.set()), daemon=True)
        threads.append(worker)
        worker.start()
        calls.append((record.call_id, observed.wait(2)))
        ledger.failed(ModelCallFailureParams(record.call_id, 'reentrant-late'))

    ledger = ModelCallLedger(ModelCallLedgerOptions(record_sink=sink))
    ledger.started(ModelCallStartedParams('one', 'fake', 'm', 4))
    if shutdown:
        close_model_call_admission(ModelCallAdmissionClosure('Stopped', 'HOST_STOPPED'))
    else:
        ledger.finished(ModelCallFinishParams('one'))
    for worker in threads:
        worker.join(2)
        assert not worker.is_alive()
    assert calls == [('one', True)]


def test_terminal_race_delivers_once():
    barrier = threading.Barrier(3)
    seen = []
    ledger = ModelCallLedger(ModelCallLedgerOptions(record_sink=seen.append))
    ledger.started(ModelCallStartedParams('one', 'fake', 'm', 4))

    def finish():
        barrier.wait()
        ledger.finished(ModelCallFinishParams('one'))

    def fail():
        barrier.wait()
        ledger.failed(ModelCallFailureParams('one', 'failure'))

    workers = [threading.Thread(target=finish), threading.Thread(target=fail)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(2)
        assert not worker.is_alive()
    assert len(seen) == 1
    assert seen[0].status == ledger.records()[0].status


def test_sink_redacts_nested_metadata_bounds_utf8_and_converts_monotonic(tmp_path, monkeypatch):
    host = _host(tmp_path)
    monkeypatch.setattr(records.time, 'time', lambda: 1700000100.)
    monkeypatch.setattr(records.time, 'monotonic', lambda: 100.)
    metadata = {'purpose': 'openai_compat', 'task_id': 'stale-task',
                'nested': {'api_key': 'secret-key', 'url': 'https://u:secret-password@example.test'},
                'headers': {'Authorization': 'Bearer secret-token'},
                'prompt': 'prompt-never-save', 'output': 'output-never-save',
                'child': [{'content': 'nested-body', 'text': 'nested-text'}]}
    sink = records.make_record_sink(host)
    row = _record(metadata=metadata)
    sink(row)
    sink(replace(row, output_tokens=999))
    body = records.execute(_call(host))
    assert len(body['items']) == 1
    value = body['items'][0]
    assert value['task_id'] == 'task-live'
    assert value['role'] == 'worker' and value['thread_id'] == 'thread-live'
    assert value['started_at'] == 1700000100.
    assert value['finished_at'] == 1700000102.
    assert value['input_tokens'] == 10 and value['output_tokens'] == 5
    serialized = json.dumps(value)
    for secret in ('secret-key', 'secret-password', 'secret-token', 'prompt-never-save',
                   'output-never-save', 'nested-body', 'nested-text'):
        assert secret not in serialized
    assert metadata['nested']['api_key'] == 'secret-key'
    huge = records._metadata({'safe': 'ok', 'huge': '中' * 10000})
    assert len(huge.encode()) <= records._limits.LLM_RECORDS_METADATA_MAX_BYTES
    assert json.loads(huge)['safe'] == 'ok'
    assert list(host.home_paths.owner_home_dir.iterdir()) == [runtime_db_path(host.home_paths.owner_home_dir)]


@pytest.mark.parametrize(('status', 'error_type', 'expected'), [
    ('finished', '', 'completed'), ('failed', '', 'failed'), ('timed_out', '', 'failed'),
    ('failed', 'ToolCancelled', 'cancelled'), ('failed', 'CancelledError', 'cancelled'),
    ('cancelled', '', 'cancelled'),
])
def test_public_terminal_mapping(status, error_type, expected):
    assert records._status(_record(status=status, error_type=error_type)) == expected


def test_owner_isolation_foreign_ids_and_live_authorization(tmp_path):
    alice, bob = _host(tmp_path), _host(tmp_path, 'bob')
    records.make_record_sink(bob)(_record())
    foreign_id = records.execute(_call(bob))['items'][0]['record_id']
    assert records.execute(_call(alice))['items'] == []
    with pytest.raises(HostAPIError, match='RESOURCE_NOT_FOUND'):
        records.execute(_call(alice, 'get', {'record_id': foreign_id}))
    assert records.execute(_call(alice, 'delete', {'record_id': foreign_id})) == {
        'deleted': False, 'record_id': foreign_id}
    # 同库脏行也必须以 owner SQL 条件隔离，不能只依赖各 owner 独立目录。
    with records._database(alice.home_paths.owner_home_dir) as conn:
        _seed(conn, 'bob', 1)
    assert records.execute(_call(alice))['items'] == []
    with pytest.raises(HostAPIError, match='RESOURCE_NOT_FOUND'):
        records.execute(replace(_call(alice), owner_id='bob'))
    denied = _call(bob, authorize=lambda _a, _f: False)
    with pytest.raises(HostAPIError, match='CAPABILITY_DENIED'):
        records.execute(denied)
    assert records.execute(_call(bob, 'get', {'record_id': foreign_id}))['record']['call_id'] == 'call-1'
    assert records.execute(_call(bob, 'delete', {'record_id': foreign_id}))['deleted'] is True
    assert records.execute(_call(bob, 'delete', {'record_id': foreign_id}))['deleted'] is False


def test_disabled_has_no_sink_io_and_list_reports_disabled(tmp_path):
    host = _host(tmp_path, enabled=False)
    records.make_record_sink(host)(_record())
    assert records.execute(_call(host)) == {
        'enabled': False, 'items': [], 'next_cursor': '', 'coverage': 'complete'}
    assert not host.home_paths.owner_home_dir.exists()


@pytest.mark.parametrize('action', ['list', 'by_task'])
def test_keyset_pagination_thousand_rows_and_signed_owner_binding(tmp_path, monkeypatch, action):
    host = _host(tmp_path)
    with records._database(host.home_paths.owner_home_dir) as conn:
        _seed(conn, 'alice', 1000)
    params = {'limit': 37, **({'task_id': 'task-a'} if action == 'by_task' else {})}
    first = records.execute(_call(host, action, params))
    assert first['coverage'] == 'partial' and first['next_cursor']
    foreign = _host(tmp_path, 'bob')
    with pytest.raises(HostAPIError, match='CURSOR_EXPIRED'):
        records.execute(_call(foreign, action, {**params, 'cursor': first['next_cursor']}))
    with pytest.raises(HostAPIError, match='CURSOR_EXPIRED'):
        records.execute(_call(host, action, {**params, 'cursor': first['next_cursor'][:-5] + 'AAAAA'}))
    changed = {**params, 'task_id': 'another-task', 'cursor': first['next_cursor']}
    with pytest.raises(HostAPIError, match='CURSOR_EXPIRED'):
        records.execute(_call(host, action, changed))
    seen, body = [], first
    while True:
        seen.extend(row['record_id'] for row in body['items'])
        if action == 'by_task':
            assert body['totals'] == {'calls': 1000, 'failed': 0, 'input_tokens': 10000,
                                      'output_tokens': 5000, 'cached_tokens': 3000}
        if not body['next_cursor']:
            break
        body = records.execute(_call(host, action, {**params, 'cursor': body['next_cursor']}))
    assert body['coverage'] == 'complete'
    assert seen == [f'alice-{i:07}' for i in reversed(range(1000))]
    monkeypatch.setattr(records, '_cursor_key', b'new-process-key')
    with pytest.raises(HostAPIError, match='CURSOR_EXPIRED'):
        records.execute(_call(host, action, {**params, 'cursor': first['next_cursor']}))


def test_hundred_thousand_rows_page_uses_indexed_seek_without_temp_sort(tmp_path):
    host = _host(tmp_path)
    with records._database(host.home_paths.owner_home_dir) as conn:
        _seed(conn, 'alice', 100000)
        conn.execute('ANALYZE llm_call_records')
        for extra, args, index in (
            ('', (), 'idx_llm_records_owner_created'),
            (' AND task_id=?', ('task-a',), 'idx_llm_records_task_keyset'),
            (' AND model=?', ('model-a',), 'idx_llm_records_model_keyset'),
        ):
            sql = ('SELECT * FROM llm_call_records WHERE owner_id=?' + extra +
                   ' AND (created_at,record_id)<(?,?) ORDER BY created_at DESC,record_id DESC LIMIT ?')
            parameters = ('alice', *args, 3., 'alice-0000030', 51)
            plan = ' '.join(str(row['detail']) for row in conn.execute('EXPLAIN QUERY PLAN ' + sql, parameters))
            assert index in plan and 'SEARCH' in plan
            assert 'SCAN ' not in plan and 'TEMP B-TREE' not in plan
            ticks = []
            conn.set_progress_handler(lambda: ticks.append(1) or 0, 100)
            page = conn.execute(sql, parameters).fetchall()
            conn.set_progress_handler(None, 0)
            assert len(page) == 30
            assert len(ticks) < 100
        assert records._list(conn, _call(host, params={'limit': 50}))['items'][0]['record_id'] == 'alice-0099999'


def test_prune_batched_keeps_newest_owner_rows_and_zero_disables_cap(tmp_path, monkeypatch):
    host = _host(tmp_path)
    monkeypatch.setattr(records, '_limits', SimpleNamespace(**{
        **vars(records._limits), 'LLM_RECORDS_PRUNE_BATCH_ROWS': 3}))
    with records._database(host.home_paths.owner_home_dir) as conn:
        _seed(conn, 'alice', 20)
        _seed(conn, 'bob', 10)
        statements = []
        conn.set_trace_callback(statements.append)
        records._prune(conn, 'alice', 7)
        conn.set_trace_callback(None)
        kept = [row[0] for row in conn.execute('SELECT record_id FROM llm_call_records WHERE owner_id="alice" ORDER BY record_id')]
        assert kept == [f'alice-{i:07}' for i in range(13, 20)]
        assert conn.execute('SELECT COUNT(*) FROM llm_call_records WHERE owner_id="bob"').fetchone()[0] == 10
        deletes = [s for s in statements if s.startswith('DELETE')]
        assert len(deletes) == 5 and all('LIMIT 3' in s for s in deletes)
        records._prune(conn, 'alice', 0)
        assert conn.execute('SELECT COUNT(*) FROM llm_call_records WHERE owner_id="alice"').fetchone()[0] == 7
    host.config.llm_call_records_max_rows = 2
    sink = records.make_record_sink(host)
    sink(_record('fresh-1'))
    sink(_record('fresh-2'))
    assert {r['call_id'] for r in records.execute(_call(host))['items']} == {'fresh-1', 'fresh-2'}


def test_by_model_clamps_window_default_and_group_cap(tmp_path, monkeypatch):
    host = _host(tmp_path)
    monkeypatch.setattr(records, '_limits', SimpleNamespace(**{
        **vars(records._limits), 'LLM_RECORDS_AGGREGATE_DEFAULT_WINDOW_SECONDS': 10,
        'LLM_RECORDS_AGGREGATE_MAX_WINDOW_SECONDS': 20, 'LLM_RECORDS_AGGREGATE_MAX_MODELS': 2}))
    with records._database(host.home_paths.owner_home_dir) as conn:
        _seed(conn, 'alice', 4)
        conn.execute('UPDATE llm_call_records SET created_at=95, model=record_id')
        conn.execute('UPDATE llm_call_records SET created_at=79 WHERE record_id="alice-0000000"')
        conn.execute('UPDATE llm_call_records SET status="failed" WHERE record_id="alice-0000001"')
    call = _call(host, 'by_model', {'since': 0., 'until': 100.})
    body = records.execute(call)
    assert body['window'] == {'since': 80., 'until': 100.}
    assert body['coverage'] == 'partial'
    assert [r['model'] for r in body['items']] == ['alice-0000001', 'alice-0000002']
    assert body['items'][0] == {'model': 'alice-0000001', 'calls': 1, 'failed': 1,
                               'input_tokens': 10, 'output_tokens': 5, 'avg_latency_ms': 2000}
    default = records.execute(_call(host, 'by_model', clock=lambda: 100.))
    assert default['window'] == {'since': 90., 'until': 100.}
    with pytest.raises(HostAPIError, match='INVALID_SCHEMA'):
        records.execute(_call(host, 'by_model', {'since': 101., 'until': 100.}))


def test_filters_and_dto_exact_shape(tmp_path):
    host = _host(tmp_path)
    records.make_record_sink(host)(_record())
    params = {'task_id': 'task-live', 'role': 'worker', 'model': 'model-a', 'status': 'completed',
              'since': 0., 'until': records.time.time() + 10}
    value = records.execute(_call(host, params=params))['items'][0]
    assert set(value) == {'record_id', 'call_id', 'request_id', 'run_id', 'task_id', 'role', 'thread_id',
                         'model', 'backend', 'profile_id', 'purpose', 'status', 'input_tokens', 'output_tokens',
                         'cached_tokens', 'latency_ms', 'error_code', 'metadata', 'started_at', 'finished_at'}
    assert records.execute(_call(host, params={**params, 'status': 'failed'}))['items'] == []
    assert records.execute(_call(host, 'get', {'record_id': value['record_id']})) == {'record': value}


def test_ledger_sink_to_sqlite_to_authorized_read_end_to_end(tmp_path):
    host = _host(tmp_path)
    ledger = ModelCallLedger(ModelCallLedgerOptions(record_sink=records.make_record_sink(host)))
    ledger.started(ModelCallStartedParams('physical', 'fake', 'model-a', 12, request_id='request-a'))
    ledger.finished(ModelCallFinishParams('physical', output_tokens=8, input_tokens=11, cached_input_tokens=4))
    ledger.failed(ModelCallFailureParams('physical', 'late'))
    body = records.execute(_call(host))
    assert len(body['items']) == 1
    assert body['items'][0]['status'] == 'completed'
    assert body['items'][0]['input_tokens'] == 11
    assert body['items'][0]['cached_tokens'] == 4
    assert body['items'][0]['request_id'] == 'request-a'


def test_current_thread_task_context_outranks_transient_agent_attributes(tmp_path):
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )

    host = _host(tmp_path)
    previous = set_current_subagent_context(host, run_id='run-current', task_attributes={
        'task_id': 'task-current', 'role': 'reviewer', 'thread_id': 'thread-current'})
    try:
        records.make_record_sink(host)(_record())
    finally:
        restore_current_subagent_context(host, previous)
    value = records.execute(_call(host))['items'][0]
    assert (value['task_id'], value['role'], value['thread_id']) == ('task-current', 'reviewer', 'thread-current')


@pytest.mark.parametrize('enabled', [False, True])
def test_core_sink_tracks_live_switch_without_replacing_ledger(tmp_path, monkeypatch, enabled):
    from agent_py_agent.agent import core
    from agent_py_agent.agent.settings import live_switches as live
    from agent_py_agent.agent.settings.config import load_config

    monkeypatch.setattr(core, 'home_paths', lambda _root: SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(core, 'runtime_guard_policy', lambda **_kw: None)
    monkeypatch.setattr(core, '_export_model_endpoint_env', lambda _c: None)
    path = tmp_path / 'agent.yaml'
    path.write_text(f'llm_call_records_enabled: {str(enabled).lower()}\n', encoding='utf-8')
    config = replace(load_config(path), my_agent_home=str(tmp_path))
    agent = _host(tmp_path)
    core._initialize_agent_construction(agent, config, tmp_path, None, None)
    live.register_live_config(agent.config)
    ledger = agent._model_call_ledger
    assert ledger.options.record_sink is not None
    database = runtime_db_path(agent.home_paths.owner_home_dir)
    assert not database.exists()
    expected = []
    for index, active in enumerate((enabled, not enabled, enabled)):
        path.write_text(f'llm_call_records_enabled: {str(active).lower()}\n', encoding='utf-8')
        live.refresh_live_switches(agent.config, force=True)
        assert agent.config.llm_call_records_enabled is active
        call_id = f'toggle-{index}'
        ledger.started(ModelCallStartedParams(call_id, 'fake', 'model-a', 4))
        ledger.finished(ModelCallFinishParams(call_id, output_tokens=2))
        assert agent._model_call_ledger is ledger
        if active:
            expected.append(call_id)
        if expected:
            with records._database(agent.home_paths.owner_home_dir) as conn:
                assert [row[0] for row in conn.execute('SELECT call_id FROM llm_call_records ORDER BY call_id')] == expected
        else:
            assert not database.exists()


def test_sparse_and_empty_filter_combinations_have_bounded_indexed_pages(tmp_path):
    """对全部筛选组合核实际分页指令数，不把 LIMIT 误当成扫描工作量的上界。"""
    host = _host(tmp_path)
    fields = ('task_id', 'role', 'model', 'status')
    with records._database(host.home_paths.owner_home_dir) as conn:
        _seed(conn, 'alice', 100000)
        conn.execute('UPDATE llm_call_records SET role="worker", status="failed" WHERE record_id="alice-0000000"')
        conn.execute('ANALYZE llm_call_records')
        for width in range(len(fields) + 1):
            for selected in combinations(fields, width):
                for absent in (False, True):
                    values = {'task_id': 'task-a', 'model': 'model-a',
                              'role': 'absent' if absent else 'worker',
                              'status': 'cancelled' if absent else 'failed'}
                    params = {key: values[key] for key in selected}
                    call = _call(host, params={**params, 'limit': 1, 'since': 0., 'until': 20000.})
                    ticks = []

                    def bounded_progress():
                        ticks.append(1)
                        return int(len(ticks) >= 100)

                    conn.set_progress_handler(bounded_progress, 100)
                    try:
                        page = records._list(conn, call)
                    except sqlite3.OperationalError as error:
                        pytest.fail(f'分页超过固定查询预算: {params}: {error}')
                    finally:
                        conn.set_progress_handler(None, 0)
                    assert len(ticks) < 100, params
                    sparse = bool({'role', 'status'} & set(selected))
                    if sparse and absent:
                        assert page['items'] == [] and page['coverage'] == 'complete'
                    elif sparse:
                        assert [row['record_id'] for row in page['items']] == ['alice-0000000']
                        assert page['coverage'] == 'complete'
                    else:
                        assert page['items'][0]['record_id'] == 'alice-0099999'
                        assert page['next_cursor'] and page['coverage'] == 'partial'
                    where, args = records._filters(call)
                    plan = ' '.join(row['detail'] for row in conn.execute(
                        'EXPLAIN QUERY PLAN SELECT * FROM llm_call_records WHERE ' + where +
                        ' ORDER BY created_at DESC, record_id DESC LIMIT ?', (*args, 2)))
                    assert 'SEARCH' in plan and 'SCAN ' not in plan and 'TEMP B-TREE' not in plan, (params, plan)
