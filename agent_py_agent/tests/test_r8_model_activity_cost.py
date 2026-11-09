"""流活动保留每片明细/原LRU，跳过无字段变化的累计统计工作。"""
from dataclasses import asdict
from threading import Event, Thread

import pytest

from agent_py_agent.agent.contracts import model_call_ledger as module


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
