"""用真实读取后的确定性时间推进覆盖Curator分片，绝不扩大生产25ms预算。"""
import time
from types import SimpleNamespace


def expire_first_thread_read(monkeypatch, store):
    from agent_py_agent.agent.memory_store import curator_inputs, retention_scan_budget

    offset = [0.0]
    fired = []
    clock = SimpleNamespace(monotonic=lambda: time.monotonic() + offset[0])
    monkeypatch.setattr(curator_inputs, 'time', clock)
    monkeypatch.setattr(retention_scan_budget, 'time', clock)
    original = store.threads.record_snapshot_report

    def read(*args, **kwargs):
        result = original(*args, **kwargs)
        if not fired:
            fired.append(True)
            offset[0] += .026
        return result

    monkeypatch.setattr(store.threads, 'record_snapshot_report', read)
    return fired


def run_until_candidate(service, *, max_slices=32):
    """仅沿明确partial继续；错误/非partial空结果仍立即失败。"""
    results = []
    for _ in range(max_slices):
        result = service.run(reason='admin')
        results.append(result)
        assert result.status in {'succeeded', 'busy'} and not result.failure_code, result
        if service.candidate_service.list():
            return result, results
        assert 'message_scan_partial' in result.warnings, result
    raise AssertionError(f'Curator未在{max_slices}片内推进: {results!r}')
