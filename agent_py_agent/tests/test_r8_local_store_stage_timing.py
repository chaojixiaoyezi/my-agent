"""隔离实际审计写入，计时只观察耗时，不影响审计、互斥或异常。"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Barrier, Event, Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.audit_service import audit_request_processing
from agent_py_agent.agent.local_storage import LocalStore, diagnostics, records
from agent_py_agent.agent.local_storage.diagnostics import (
    local_store_stage,
    local_store_stage_timing_scope,
)


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
