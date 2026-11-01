"""100/1000/10000规模的操作计数。索引夹具不代表线上长时间老化/RSS验收。"""
import json
from pathlib import Path

import pytest

from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFinishParams,
    ModelCallLedger,
    ModelCallStartedParams,
)
from agent_py_agent.agent.conversation import session_list_index as indexes


@pytest.mark.parametrize('history', [100, 1000, 10000])
def test_warm_list_reads_and_scans_do_not_grow_with_history(tmp_path, monkeypatch, request, history):
    root = tmp_path / 'records'
    root.mkdir()
    for number in range(history):
        (root / f'{number:06}.json').write_text(json.dumps({
            'task_id': str(number), 'target_thread_id': 'target', 'created_at': number}))
    reads, scans, stamps = [], [], []

    def reader(path):
        reads.append(path)
        return json.loads(path.read_text()), None

    index = indexes.SessionListIndex(root, kind='session_task', reader=reader)
    for _ in range(history // 128 + 2):
        reads.clear()
        page = index.page(limit=5, filter_key='target')
        assert len(reads) <= 128 + 5  # 原合同冷回填批次加当前页原文，不读全部历史。
        if page['coverage'] == 'complete':
            break
    else:
        pytest.fail('有界冷回填必须能追平，不得假not_found')
    expected = [str(i) for i in reversed(range(history - 5, history))]
    assert [item['task_id'] for item in page['items']] == expected
    original_scandir, original_stamp = indexes.os.scandir, indexes._stamp

    def scan(path):
        if Path(path) == root:
            scans.append(path)
        return original_scandir(path)

    def stamp(path):
        if Path(path).is_relative_to(root):
            stamps.append(path)
        return original_stamp(path)

    monkeypatch.setattr(indexes.os, 'scandir', scan)
    monkeypatch.setattr(indexes, '_stamp', stamp)
    measurements = []
    for _ in range(3):
        reads.clear()
        stamps.clear()
        warm = index.page(limit=5, filter_key='target')
        assert [item['task_id'] for item in warm['items']] == expected
        assert warm['coverage'] == 'complete' and not warm['load_errors']
        assert len(reads) == 5
        assert len(stamps) == 8  # 根身份1、原位变化探针2、返回原正文5。
        assert scans == []
        measurements.append({'json_reads': len(reads), 'source_stats': len(stamps), 'directory_scans': len(scans)})
    request.node.user_properties.append(('aging_operation_counts', json.dumps({
        'history': history, 'samples': measurements, 'unit': 'operations_not_wall_time'})))


@pytest.mark.parametrize('calls', [100, 1000, 10000])
def test_finished_call_retention_stays_bounded_without_losing_cumulative_usage(calls):
    ledger = ModelCallLedger()
    for i in range(calls):
        ledger.started(ModelCallStartedParams(str(i), 'fixture', 'model', 10,
            request_id='request', run_id='run'))
        ledger.finished(ModelCallFinishParams(str(i), input_tokens=10, output_tokens=2))
    assert len(ledger.records()) == min(calls, ledger.options.max_records)
    summary = ledger.cumulative_summary(run_id='run')
    assert summary['physical_model_attempt_count'] == calls
    assert summary['accounted_input_tokens'] == calls * 10
    assert summary['output_tokens'] == calls * 2
