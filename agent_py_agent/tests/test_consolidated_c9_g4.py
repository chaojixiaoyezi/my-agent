"""Wave 3 consolidated tests (shard c9, group 4: test_r8_local_store_stage_timing.py, test_r8_log_cursor_lifetime.py, test_r8_model_activity_cost.py, test_r8_model_call_exact_lookup.py, test_r8_model_worker_context_release.py, test_r8_notice_tree_read_cost.py, test_r8_owner_directory_paging.py)."""
import os
import sqlite3
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import asdict
from pathlib import Path
from threading import Barrier, Event, Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import owner_wake_discovery as discovery
from agent_py_agent.agent.contracts import model_call_ledger as module
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFinishParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallLedgerOptions,
    ModelCallRecord,
    ModelCallStartedParams,
)
from agent_py_agent.agent.conversation.display_archive import archive_display_rows
from agent_py_agent.agent.gateway_parts.audit_service import audit_request_processing
from agent_py_agent.agent.gateway_parts.business_api_v13_readers import _OwnerReaders
from agent_py_agent.agent.local_storage import LocalStore, diagnostics, records, schema
from agent_py_agent.agent.local_storage.control_plane_models import AgentRunRecord
from agent_py_agent.agent.local_storage.diagnostics import (
    local_store_stage,
    local_store_stage_timing_scope,
)
from agent_py_agent.tests import test_v13_w0_observation_readers as readers

# ============================================================================
# Source: agent_py_agent/tests/test_r8_local_store_stage_timing.py
# ============================================================================

def test_inactive_and_closed_timing_scope_do_not_sample_clock(monkeypatch):
    def forbidden():
        pytest.fail('inactive timing must not sample a clock')

    monkeypatch.setattr(diagnostics, 'time', SimpleNamespace(monotonic=forbidden))
    with local_store_stage('unused_ms'):
        pass
    stages = {}
    with local_store_stage_timing_scope(stages):
        captured = copy_context()
    captured.run(lambda: _measured('expired_ms'))
    assert stages == {}


def _measured(name):
    with local_store_stage(name):
        pass


def test_timing_preserves_original_exception_and_accumulates(monkeypatch):
    clock = iter((1.0, 1.25, 2.0, 2.5))
    monkeypatch.setattr(diagnostics, 'time', SimpleNamespace(monotonic=lambda: next(clock)))
    stages = {'operation_ms': 10.0}
    original = OSError('isolated failure')
    with pytest.raises(OSError) as raised:
        with local_store_stage_timing_scope(stages):
            _measured('operation_ms')
            with local_store_stage('operation_ms'):
                raise original
    assert raised.value is original and stages == {'operation_ms': 760.0}


def test_concurrent_scopes_do_not_mix_request_fields():
    barrier = Barrier(2)

    def run(name):
        stages = {}
        with local_store_stage_timing_scope(stages):
            with local_store_stage(name):
                barrier.wait(timeout=5)
        return stages

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run, 'first_ms')
        second = executor.submit(run, 'second_ms')
        assert set(first.result(timeout=5)) == {'first_ms'}
        assert set(second.result(timeout=5)) == {'second_ms'}


def test_actual_audit_attributes_derived_lock_wait_before_body_and_sql(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / 'local.db')
    original_lock = records._derived_mutation_lock
    attempted = Event()
    clock = [100.0]
    stages = {}
    failures = []

    def observed_lock(value):
        attempted.set()
        return original_lock(value)

    monkeypatch.setattr(records, '_derived_mutation_lock', observed_lock)
    monkeypatch.setattr(diagnostics, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    context = {'request': {'id': 'fake-request', 'prompt': 'isolated audit'}, 'request_id': 'fake-request',
               'kind': 'ask', 'started_at': 1.0, 'request_path': tmp_path / 'request.json',
               'response_path': tmp_path / 'response.json'}

    def audit():
        try:
            with local_store_stage_timing_scope(stages):
                audit_request_processing(SimpleNamespace(local_store=store), context)
        except BaseException as error:
            failures.append(error)

    with original_lock(store):
        worker = Thread(target=audit)
        worker.start()
        assert attempted.wait(5)
        assert not store._content_file(store.make_record_id('gateway_request', 'fake-request')).exists()
        clock[0] = 105.0
    worker.join(5)
    assert not worker.is_alive() and not failures
    assert stages['local_store_derived_lock_acquire_ms'] == 5000.0
    assert set(stages) == {'gateway_audit_render_ms', 'local_store_derived_lock_acquire_ms',
        'local_store_body_write_ms', 'local_store_connection_acquire_ms', 'local_store_record_sql_ms',
        'local_store_fts_ms', 'local_store_event_sql_ms', 'local_store_event_jsonl_ms', 'local_store_commit_ms', 'local_store_connection_release_ms'}
    assert all(value == 0.0 for key, value in stages.items() if key != 'local_store_derived_lock_acquire_ms')
    record = store.get_record(store.make_record_id('gateway_request', 'fake-request'))
    assert record.metadata['status'] == 'processing' and 'isolated audit' in record.content
    with store._connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 2


@pytest.mark.parametrize('suppress', [False, True])
def test_connection_release_timing_preserves_exit_exception_and_suppression(monkeypatch, suppress):
    from agent_py_agent.agent.local_storage.diagnostics import LocalStoreConnectionTimingStack

    original = OSError('body failure')
    received = []
    stages = {}
    clock = iter((1.0, 1.125))
    monkeypatch.setattr(diagnostics, 'time', SimpleNamespace(monotonic=lambda: next(clock)))

    class Resource:
        def __enter__(self):
            return self

        def __exit__(self, kind, value, traceback):
            received.append((kind, value, traceback))
            return suppress

    def fail():
        with local_store_stage_timing_scope(stages):
            with LocalStoreConnectionTimingStack() as resources:
                resources.enter_context(Resource())
                raise original

    if suppress:
        fail()
    else:
        with pytest.raises(OSError) as raised:
            fail()
        assert raised.value is original
    assert received[0][0] is OSError and received[0][1] is original and received[0][2] is not None
    assert stages == {'local_store_connection_release_ms': 125.0}


@pytest.mark.parametrize('fault', ['entry', 'finish', 'accumulate', 'merge'])
@pytest.mark.parametrize('business_error', [False, True])
def test_diagnostic_exception_never_replaces_business_body(monkeypatch, fault, business_error):
    class BrokenValues(dict):
        def get(self, *args):
            raise ValueError('diagnostic mapping failure')

    calls = []
    def clock():
        calls.append(None)
        if fault == 'entry' or fault == 'finish' and len(calls) == 2:
            raise ValueError('diagnostic clock failure')
        return float(len(calls))
    monkeypatch.setattr(diagnostics, 'time', SimpleNamespace(monotonic=clock))
    original = OSError('original business failure')
    stages = BrokenValues() if fault == 'merge' else {}
    entered = []
    def run():
        with local_store_stage_timing_scope(stages):
            with local_store_stage('operation_ms'):
                entered.append(True)
                if fault == 'accumulate':
                    diagnostics._TIMING.get().values = BrokenValues()
                if business_error:
                    raise original
    if business_error:
        with pytest.raises(OSError) as raised:
            run()
        assert raised.value is original
    else:
        run()
    assert entered == [True]
    assert diagnostics._TIMING.get() is None


def test_diagnostic_error_cannot_replace_resource_exit_exception(monkeypatch):
    from agent_py_agent.agent.local_storage.diagnostics import LocalStoreConnectionTimingStack

    calls = []
    def clock():
        calls.append(None)
        if len(calls) == 2:
            raise ValueError('diagnostic finish failure')
        return 1.0
    monkeypatch.setattr(diagnostics, 'time', SimpleNamespace(monotonic=clock))
    original, closing = OSError('body'), RuntimeError('resource exit')
    class Resource:
        def __enter__(self):
            return self
        def __exit__(self, kind, value, traceback):
            assert value is original
            raise closing
    with pytest.raises(RuntimeError) as raised:
        with local_store_stage_timing_scope({}):
            with LocalStoreConnectionTimingStack() as resources:
                resources.enter_context(Resource())
                raise original
    assert raised.value is closing and closing.__context__ is original


@pytest.mark.parametrize('fault', ['entry', 'finish', 'merge'])
def test_diagnostic_base_exception_is_not_swallowed(monkeypatch, fault):
    calls = []
    original = KeyboardInterrupt('stop diagnostic observer')
    def clock():
        calls.append(None)
        if fault == 'entry' or fault == 'finish' and len(calls) == 2:
            raise original
        return float(len(calls))
    class StopValues(dict):
        def get(self, *args):
            raise original
    monkeypatch.setattr(diagnostics, 'time', SimpleNamespace(monotonic=clock))
    with pytest.raises(KeyboardInterrupt) as raised:
        with local_store_stage_timing_scope(StopValues() if fault == 'merge' else {}):
            with local_store_stage('operation_ms'):
                pass
    assert raised.value is original


@pytest.mark.parametrize('suppress', [False, True])
def test_exit_stack_suppression_survives_broken_diagnostic_clock(monkeypatch, suppress):
    from agent_py_agent.agent.local_storage.diagnostics import LocalStoreConnectionTimingStack

    calls = []
    def clock():
        calls.append(None)
        if len(calls) == 2:
            raise ValueError('diagnostic finish failure')
        return 1.0
    monkeypatch.setattr(diagnostics, 'time', SimpleNamespace(monotonic=clock))
    original = OSError('body')
    class Resource:
        def __enter__(self):
            return self
        def __exit__(self, kind, value, traceback):
            assert kind is OSError and value is original and traceback is not None
            return suppress
    def run():
        with local_store_stage_timing_scope({}):
            with LocalStoreConnectionTimingStack() as resources:
                resources.enter_context(Resource())
                raise original
    if suppress:
        run()
    else:
        with pytest.raises(OSError) as raised:
            run()
        assert raised.value is original


# ============================================================================
# Source: agent_py_agent/tests/test_r8_log_cursor_lifetime.py
# ============================================================================

background_http = readers.background_http
isolated_home = readers.isolated_home


def test_log_cursor_survives_namespace_churn(background_http, tmp_path):
    services, task, runtime, *_ = readers.seeded(background_http)
    reference = archive_display_rows(SimpleNamespace(conversation_store=services.host.conversation_store),
        thread_id=task.target_thread_id, rows=[{'kind': 'text', 'text': 'x' * 120}])
    readers.wire(services, runtime)
    params = {'source_ref': reference['archive_id'], 'max_bytes': 40}
    status, first = readers.background_call(background_http, 'logs.read', params)
    assert status == 200 and first['data']['next_cursor']
    for index in range(300):
        _OwnerReaders(tmp_path / str(index), f'local/owner-{index}', None, None)
    status, second = readers.background_call(background_http, 'logs.read',
        {**params, 'cursor': first['data']['next_cursor']})
    assert status == 200, second
    assert second['data']['chunks'][0]['text'] == 'x' * 40


def test_log_cursor_is_still_bound_to_namespace(tmp_path):
    original = _OwnerReaders(tmp_path / 'one', 'local/a', None, None)
    cursor = original.log_cursor('source', 2, 10)
    import pytest

    from agent_py_agent.agent.conversation.business_access import BusinessError

    for reader in (_OwnerReaders(tmp_path / 'one', 'local/b', None, None),
                   _OwnerReaders(tmp_path / 'two', 'local/a', None, None)):
        with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
            reader.log_position('source', cursor)


# ============================================================================
# Source: agent_py_agent/tests/test_r8_model_activity_cost.py
# ============================================================================

def _started(ledger, name='call', purpose='main'):
    return ledger.started(module.ModelCallStartedParams(name, 'fake', 'fake', 100,
        request_id='request', run_id='run', metadata={'purpose': purpose, 'logical_call_id': name}))


def test_stream_activity_does_not_recompute_unchanged_totals_but_real_updates_do(monkeypatch):
    ledger = module.ModelCallLedger()
    _started(ledger)
    counts = []
    original = module._partitioned_usage

    def observed(record):
        counts.append(1)
        return original(record)

    monkeypatch.setattr(module, '_partitioned_usage', observed)
    before = ledger.cumulative_summary(request_id='request')
    for _ in range(1000):
        ledger.activity(module.ModelCallActivityParams('call', 1))
    assert ledger.record('call').output_tokens_seen == 1000
    assert ledger.cumulative_summary(request_id='request') == before
    assert counts == []
    ledger.first_token(module.ModelCallFirstTokenParams('call', 1001))
    assert counts
    counts.clear()
    ledger.finished(module.ModelCallFinishParams('call', output_tokens=99, input_tokens=120, provider_usage_reported=True))
    assert counts
    assert ledger.cumulative_summary(request_id='request')['output_tokens'] == 99


@pytest.mark.parametrize('terminal', ['finished', 'failed', 'timeout'])
def test_activity_matches_original_accounting_across_real_lifecycle_changes(terminal):
    class Reference(module.ModelCallLedger):
        def _replace(self, record, *, activity_only=False):
            super()._replace(record)

    clock = [10.0]
    options = module.ModelCallLedgerOptions(max_records=2)
    context = module.ModelCallLedgerContext(now=lambda: clock[0])
    actual, expected = module.ModelCallLedger(options, context), Reference(options, context)
    pins = []
    for ledger in (actual, expected):
        for name, purpose in [('call', 'main'), ('decision', 'decision')]:
            _started(ledger, name, purpose)
        pins.append(ledger.retain_call('call'))

    def check():
        assert [asdict(row) for row in actual.records()] == [asdict(row) for row in expected.records()]
        for scope in ({'request_id': 'request'}, {'run_id': 'run'}):
            a, b = actual.cumulative_summary(**scope), expected.cumulative_summary(**scope)
            assert {k: v for k, v in a.items() if k != 'usage_scope_id'} == {k: v for k, v in b.items() if k != 'usage_scope_id'}
        assert list(actual._scope_aggregates) == list(expected._scope_aggregates)

    for step in range(8):
        clock[0] += 1
        for ledger in (actual, expected):
            ledger.activity(module.ModelCallActivityParams('call', step))
            ledger.activity(module.ModelCallActivityParams('decision', 0))
            if step == 2:
                ledger.first_token(module.ModelCallFirstTokenParams('call', 3))
            if step == 3:
                ledger.provider_attempt(module.ModelCallProviderAttemptParams('call', 'http', 'response_opened'))
        check()
    for ledger in (actual, expected):
        if terminal == 'finished':
            ledger.finished(module.ModelCallFinishParams('call', output_tokens=55, provider_usage_reported=True))
        elif terminal == 'failed':
            ledger.failed(module.ModelCallFailureParams('call', 'SyntheticError'))
        else:
            ledger.timeout(module.ModelCallTimeoutParams('call', 3, 'wall_clock'))
        ledger.activity(module.ModelCallActivityParams('call', 999))
        ledger.provider_attempt(module.ModelCallProviderAttemptParams('call', 'late-http', 'failed'))
    check()
    for pin in pins:
        pin.release()


def test_activity_and_terminal_event_interleave_under_original_lock():
    ledger = module.ModelCallLedger()
    _started(ledger)
    lock = ledger._lock
    entered, complete = Event(), Event()

    class ObservedLock:
        def __enter__(self):
            entered.set()
            lock.acquire()

        def __exit__(self, *args):
            lock.release()

    ledger._lock = ObservedLock()
    with lock:
        worker = Thread(target=lambda: (ledger.activity(module.ModelCallActivityParams('call', 1)), complete.set()))
        worker.start()
        assert entered.wait(2)
        terminal = ledger.failed(module.ModelCallFailureParams('call', 'SyntheticError'))
        assert not complete.is_set()
    assert complete.wait(2)
    worker.join(2)
    assert ledger.record('call') is terminal
    with pytest.raises(KeyError):
        ledger.activity(module.ModelCallActivityParams('missing', 1))


# ============================================================================
# Source: agent_py_agent/tests/test_r8_model_call_exact_lookup.py
# ============================================================================

@pytest.mark.parametrize('count', [1, 10000])
def test_exact_lookup_uses_one_index_and_one_row_access(count):
    ledger = ModelCallLedger()

    class Rows(list):
        reads = 0

        def __iter__(self):
            raise AssertionError('exact lookup must not scan history')

        def __getitem__(self, key):
            self.reads += 1
            return super().__getitem__(key)

    class Index(dict):
        reads = 0

        def get(self, key, default=None):
            self.reads += 1
            return super().get(key, default)

    rows = Rows(ModelCallRecord(str(i), 'echo', 'fake', 1) for i in range(count))
    ledger._records = rows
    ledger._index = Index({str(i): i for i in range(count)})
    ledger.records = lambda: pytest.fail('exact lookup must not copy records')
    assert ledger.record(str(count - 1)).call_id == str(count - 1)
    assert (ledger._index.reads, rows.reads) == (1, 1)
    assert ledger.record('absent') is None
    assert (ledger._index.reads, rows.reads) == (2, 1)


def test_exact_lookup_tracks_replacement_retention_and_eviction():
    ledger = ModelCallLedger(ModelCallLedgerOptions(max_records=1))
    ledger.started(ModelCallStartedParams('old', 'echo', 'fake', 1))
    previous = ledger.record('old')
    pin = ledger.retain_call('old')
    ledger.finished(ModelCallFinishParams('old'))
    assert previous.status == 'started' and ledger.record('old').status == 'finished'
    ledger.started(ModelCallStartedParams('new', 'echo', 'fake', 1))
    assert ledger.record('old') is not None
    pin.release()
    ledger.finished(ModelCallFinishParams('new'))
    assert ledger.record('old') is None
    assert ledger.record('new').status == 'finished'


def test_exact_metric_and_timeout_consumers_do_not_request_all_records():
    from agent_py_agent.agent.agent_core import tool_model_generation as generation
    from agent_py_agent.agent.agent_core.model.response_provenance import stamp_response_provenance
    from agent_py_agent.agent.conversation.model_metrics import _last_response_metrics

    ledger = ModelCallLedger(context=ModelCallLedgerContext(now=lambda: 20.0))
    ledger.started(ModelCallStartedParams('exact', 'echo', 'fake', 1, metadata={'purpose': 'main'}))
    ledger.finished(ModelCallFinishParams('exact'))
    ledger.records = lambda: pytest.fail('exact consumer copied history')
    assert generation._provider_timeout_elapsed(ledger, 'exact') == 0.0
    assert generation._provider_timeout_idle_silence(ledger, 'missing') is None
    response = SimpleNamespace(usage={})
    assert 'output_tps' in _last_response_metrics(response, ledger, 'exact')
    stamp_response_provenance(SimpleNamespace(), ledger, 'exact', response)
    assert response.response_provenance.call_id == 'exact'
    stamp_response_provenance(SimpleNamespace(), ledger, 'missing', response)
    assert response.response_provenance is None
    ledger.record = lambda call_id: (_ for _ in ()).throw(OSError('read failed'))
    assert generation._provider_timeout_elapsed(ledger, 'exact') == 0.0
    assert generation._provider_timeout_idle_silence(ledger, 'exact') is None
    with pytest.raises(OSError):
        _last_response_metrics(response, ledger, 'exact')


def test_exact_lookup_waits_for_same_update_lock():
    import threading

    ledger = ModelCallLedger()
    ledger.started(ModelCallStartedParams('exact', 'echo', 'fake', 1))
    lock = ledger._lock
    attempted, done = threading.Event(), threading.Event()
    results = []

    class ObservedLock:
        def __enter__(self):
            attempted.set()
            lock.acquire()

        def __exit__(self, *args):
            lock.release()

    ledger._lock = ObservedLock()
    with lock:
        worker = threading.Thread(target=lambda: (results.append(ledger.record('exact')), done.set()))
        worker.start()
        assert attempted.wait(2)
        assert not done.is_set()
    assert done.wait(2)
    worker.join(2)
    assert results[0].call_id == 'exact'


# ============================================================================
# Source: agent_py_agent/tests/test_r8_model_worker_context_release.py
# ============================================================================

def test_completed_guard_does_not_retain_inherited_request_context(tmp_path):
    code = r'''
import contextvars,threading,weakref
from types import SimpleNamespace
from agent_py_agent.agent.agent_core import tool_model_generation as generation
from agent_py_agent.agent.conversation.background_shutdown import BackgroundShutdown
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger,ModelCallStartedParams,ModelCallFinishParams
class Payload:
    def __init__(self):self.body=bytearray(512*1024)
current=contextvars.ContextVar('synthetic-request-history')
shutdown=BackgroundShutdown();ledger=ModelCallLedger();references=[];workers=[];seen=[]
def backend(*args):
    workers.append(threading.current_thread())
    seen.append(len(current.get().body))
    return 'done'
generation._effective_model_request_timeout_seconds=lambda *args,**kwargs:5.0
generation._generate_backend_response=backend
request=SimpleNamespace(agent=SimpleNamespace(backend=None),prompt='synthetic')
for i in range(3):
    payload=Payload();references.append(weakref.ref(payload));token=current.set(payload)
    ledger.started(ModelCallStartedParams(str(i),'fake','fake',0))
    state=SimpleNamespace(ledger=ledger,call_id=str(i),liveness=SimpleNamespace(abandon=lambda:None))
    try:
        with shutdown.observe(None):
            assert generation._generate_through_wall_guard(request,state)=='done'
            workers[-1].join(3)
            assert not workers[-1].is_alive()
            ledger.finished(ModelCallFinishParams(str(i)))
    finally:current.reset(token)
    del payload,token,state
assert seen==[512*1024]*3, '原显式Context仍必须传递业务身份和请求字段'
assert not shutdown.slices, '自然退出的原观察应可核收'
assert all(reference() is None for reference in references), '已退出Thread不能再钉住第二份隐式请求Context'
assert len(workers)==3 and all(not worker.is_alive() for worker in workers)
'''
    home = tmp_path / 'home'
    home.mkdir()
    args = [sys.executable]
    if hasattr(sys.flags, 'thread_inherit_context'):
        args += ['-X', 'thread_inherit_context=1']
    completed = subprocess.run([*args, '-c', code], capture_output=True, text=True, timeout=30,
        env={**os.environ, 'HOME': str(home), 'MY_AGENT_HOME': str(home)})
    assert completed.returncode == 0, completed.stderr


# ============================================================================
# Source: agent_py_agent/tests/test_r8_notice_tree_read_cost.py
# ============================================================================

def _seed(store, suffix=''):
    for run_id, depth in [('child-b', 1), ('parent', 0), ('child-a', 1)]:
        store.upsert_agent_run(AgentRunRecord(
            run_id=run_id + suffix, root_task_id='root', depth=depth,
            status='RUNNING', created_at=1, updated_at=2,
        ))
    store.upsert_agent_run(AgentRunRecord(run_id='foreign', root_task_id='other'))
    store.rebuild_task_rollup('root')


def test_tree_report_uses_one_short_read_connection_and_preserves_full_rows(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / 'local.db')
    _seed(store)
    expected_runs = store._agent_runs_for_task('root')
    expected_rollup = store.get_task_rollup('root')
    original = store._passive_connection
    connections, statements = [], []

    @contextmanager
    def observed():
        with original() as conn:
            connections.append(conn)
            conn.set_trace_callback(statements.append)
            yield conn

    monkeypatch.setattr(store, '_passive_connection', observed)
    report = store.list_agent_tree('root')
    assert report.runs == expected_runs
    assert report.rollup == expected_rollup
    assert [run.run_id for run in report.runs] == ['parent', 'child-a', 'child-b']
    assert len(connections) == 1
    assert len([sql for sql in statements if sql.lstrip().startswith('SELECT')]) == 2
    with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
        connections[0].execute('SELECT 1')
    assert store.list_agent_tree('missing').runs == []
    assert store.list_agent_tree('missing').rollup is None


def test_next_report_reopens_replaced_directory_without_retaining_old_database(tmp_path):
    original = LocalStore(tmp_path / 'active' / 'local.db')
    replacement = LocalStore(tmp_path / 'replacement' / 'local.db')
    _seed(original, '-old')
    _seed(replacement, '-new')
    assert all(run.run_id.endswith('-old') for run in original.list_agent_tree('root').runs)
    original.root.rename(tmp_path / 'retired')
    replacement.root.rename(original.root)
    assert all(run.run_id.endswith('-new') for run in original.list_agent_tree('root').runs)
    original.root.rename(tmp_path / 'replacement')
    (tmp_path / 'retired').rename(original.root)
    assert all(run.run_id.endswith('-old') for run in original.list_agent_tree('root').runs)


@pytest.mark.parametrize('failure', ['missing', 'directory', 'symlink', 'permission'])
def test_tree_read_preserves_source_validation_and_never_repairs(tmp_path, monkeypatch, failure):
    store = LocalStore(tmp_path / 'store' / 'local.db')
    _seed(store)
    store.db_path.rename(store.root / 'saved.db')
    expected = None
    if failure == 'directory':
        store.db_path.mkdir()
    elif failure == 'symlink':
        store.db_path.symlink_to(store.root / 'saved.db')
    elif failure == 'permission':
        (store.root / 'saved.db').rename(store.db_path)
        original = schema.require_plain_sqlite_files
        expected = PermissionError('fixture access denied')

        def denied(db, **kwargs):
            if db == store.db_path:
                raise expected
            return original(db, **kwargs)

        monkeypatch.setattr(schema, 'require_plain_sqlite_files', denied)
    with pytest.raises((FileNotFoundError, ValueError, PermissionError)) as caught:
        store.list_agent_tree('root')
    if expected is not None:
        assert caught.value is expected
    if failure == 'missing':
        assert not store.db_path.exists()


def test_second_query_failure_closes_the_same_connection_and_does_not_repair(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / 'local.db')
    _seed(store)
    with store._connection() as conn:
        conn.execute('DROP TABLE task_rollups')
        conn.commit()
    original = store._passive_connection
    connections = []

    @contextmanager
    def observed():
        with original() as conn:
            connections.append(conn)
            yield conn

    monkeypatch.setattr(store, '_passive_connection', observed)
    with pytest.raises(sqlite3.OperationalError, match='no such table: task_rollups'):
        store.list_agent_tree('root')
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
        connections[0].execute('SELECT 1')


# ============================================================================
# Source: agent_py_agent/tests/test_r8_owner_directory_paging.py
# ============================================================================

def test_cold_owner_page_bounds_scan_and_eventually_covers_all(tmp_path, monkeypatch):
    owners = tmp_path / 'owners'
    for number in reversed(range(500)):
        (owners / 'providers' / 'feishu' / 'users' / f'u{number:04d}').mkdir(parents=True)
    visits = []
    original = discovery._candidate_owner_homes

    def counted(root):
        for row in original(root):
            visits.append(row)
            yield row

    monkeypatch.setattr(discovery, '_candidate_owner_homes', counted)
    cursor, found = None, []
    for _ in range(140):
        before = len(visits)
        page = discovery.discover_owner_home_page(owners, limit=4, after_cursor=cursor)
        assert len(visits) - before <= 128
        assert len(page.targets) <= 4
        found.extend(target.identity.owner_id for target in page.targets)
        cursor = page.next_cursor
        if cursor is None:
            break
    else:
        raise AssertionError('分页未收敛')
    assert found == [f'u{number:04d}' for number in range(500)]
    assert len(visits) == 500


def test_removed_owner_not_returned_during_reconciliation(tmp_path):
    owners = tmp_path / 'owners'
    for number in range(300):
        (owners / 'local' / f'u{number:04d}').mkdir(parents=True)
    cursor = None
    for _ in range(10):
        page = discovery.discover_owner_home_page(owners, limit=100, after_cursor=cursor)
        cursor = page.next_cursor
        if cursor is None:
            break
    (owners / 'local' / 'u0000').rmdir()
    page = discovery.discover_owner_home_page(owners, limit=4)
    assert all(target.identity.owner_id != 'u0000' for target in page.targets)
    assert page.next_cursor is not None


def _coarse_directory_clock(monkeypatch):
    original = Path.stat

    def measured(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if not stat.S_ISDIR(result.st_mode):
            return result
        fields = {name: getattr(result, name) for name in dir(result) if name.startswith('st_')}
        fields.update(st_mtime_ns=1, st_ctime_ns=1)
        return SimpleNamespace(**fields)

    monkeypatch.setattr(Path, 'stat', measured)


def test_owner_add_remove_invalidates_even_inside_one_filesystem_clock_tick(tmp_path, monkeypatch):
    owners = tmp_path / 'owners'
    bucket = owners / 'providers' / 'feishu' / 'users'
    (bucket / 'first').mkdir(parents=True)
    _coarse_directory_clock(monkeypatch)
    assert [row.identity.owner_id for row in discovery.discover_owner_home_page(owners).targets] == ['first']
    (bucket / 'second').mkdir()
    assert [row.identity.owner_id for row in discovery.discover_owner_home_page(owners).targets] == ['first', 'second']
    (bucket / 'first').rmdir()
    assert [row.identity.owner_id for row in discovery.discover_owner_home_page(owners).targets] == ['second']


def test_new_run_is_detected_with_unchanged_directory_timestamps(tmp_path, monkeypatch):
    import json

    from agent_py_agent.tests.test_owner_wake_discovery import _settled_history, _write_link

    _coarse_directory_clock(monkeypatch)
    _settled, active = _settled_history(tmp_path)
    assert discovery.unfinished_task_ids(tmp_path) == ['task-100']
    new = active.parents[2] / 'new-run' / 'work' / 'state.json'
    new.parent.mkdir(parents=True)
    new.write_text(json.dumps({'task_id': 'new', 'status': 'RUNNING'}))
    _write_link(tmp_path, 'new', 'active')
    assert set(discovery.unfinished_task_ids(tmp_path)) == {'task-100', 'new'}


def test_large_link_cache_reenumerates_names_at_ttl_even_when_metadata_collides(tmp_path, monkeypatch):
    import json
    import os
    import time

    folder = tmp_path / 'links'
    folder.mkdir()
    old_ns = time.time_ns() - 120_000_000_000
    for index in range(100):
        path = folder / f'task-{index}.json'
        path.write_text(json.dumps({'task_id': f'task-{index}', 'status': 'completed'}))
        os.utime(path, ns=(old_ns, old_ns))
    _coarse_directory_clock(monkeypatch)
    first = discovery._directory_scan(folder, 'link')
    (folder / 'task-0.json').unlink()
    replacement = folder / 'new-task.json'
    replacement.write_text(json.dumps({'task_id': 'new', 'status': 'active'}))
    # 同数量文件替换可保持全部目录元数据；模拟这种合法文件系统结果，不伪造文件内容。
    monkeypatch.setattr(discovery, '_directory_fp', lambda root: first.dir_fp)
    assert discovery._directory_scan(folder, 'link').run_names == first.run_names
    monkeypatch.setattr(discovery.time, 'monotonic', lambda: first.verified_at + 61)
    refreshed = discovery._directory_scan(folder, 'link')
    assert 'task-0.json' not in refreshed.run_names
    assert ('new', 'active', replacement) in refreshed.active_rows
