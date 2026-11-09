"""精确模型调用查询复用唯一索引，不因保留历史增长而复制/扫描。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFinishParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallLedgerOptions,
    ModelCallRecord,
    ModelCallStartedParams,
)


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
