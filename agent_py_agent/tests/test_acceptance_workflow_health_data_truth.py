"""WF-11 原 Record 内存窗口的纯值合同；不冒充生产 HTTP/Store I/O 验收。"""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.workflows.models import StepExecutionRecord, WorkflowRunRecord
from agent_py_agent.agent.workflows.query_health import (
    build_workflow_health_view,
    summarize_run_step_health,
)


def step(**changes):
    record = StepExecutionRecord('s', '计算', 'tool_call', 'succeeded', 0, 0, 0, False,
                                 escalation_result={'tool_receipt': {'operation_id': 'op-original'}})
    for key, value in changes.items():
        setattr(record, key, value)
    return record


def run(*steps):
    return WorkflowRunRecord('r', 'wf', 'owner', 'manual', 'succeeded', 0, 0, 0,
                             steps=tuple(steps))


class Window:
    """已授权原对象窗口；list_runs 无 I/O，coverage 由采样者声明。"""
    def __init__(self, runs, coverage='complete', **metadata):
        self.runs = runs
        self.calls = []
        self.health_read_coverage = {'coverage': coverage, 'truncated': False, **metadata}

    def list_runs(self, *, workflow_id, limit):
        self.calls.append((workflow_id, limit))
        return self.runs[:limit]


def view(*steps, **kwargs):
    return build_workflow_health_view(Window([run(*steps)]), workflow_id='wf', **kwargs)


@pytest.mark.parametrize('value', [None, float('nan'), float('inf'), -float('inf'), True, False, -1, '2'])
def test_duration_invalid_is_not_a_zero_sample(value):
    result = view(step(duration_ms=value))
    duration = result['step_health']['s']['duration_ms']
    assert duration == {'count': 0, 'min_ms': None, 'avg_ms': None, 'max_ms': None, 'p95_ms': None}
    assert result['coverage'] != 'complete'
    assert result['step_health']['s']['field_coverage']['duration_ms']['unknown'] == 1
    json.dumps(result, allow_nan=False)


def test_missing_duration_and_real_zero_remain_distinct():
    missing = SimpleNamespace(**{k: v for k, v in vars(step()).items() if k != 'duration_ms'})
    assert summarize_run_step_health(run(missing))['steps'][0]['duration_ms'] is None
    result = view(step(duration_ms=0), missing, step(duration_ms=12))
    duration = result['step_health']['s']['duration_ms']
    assert duration == {'count': 2, 'min_ms': 0.0, 'avg_ms': 6.0, 'max_ms': 12.0, 'p95_ms': 12.0}
    assert result['coverage'] == 'partial'
    assert result['step_health']['s']['field_coverage']['duration_ms'] == {
        'known': 2, 'unknown': 1, 'coverage': 'partial'}


@pytest.mark.parametrize('value', [None, True, False, 0, -1, 1.5, '2'])
def test_unknown_or_illegal_attempt_is_not_imputed(value):
    record = step(attempt_count=value)
    summary = summarize_run_step_health(run(record))['steps'][0]
    assert summary['attempt_count'] is None
    assert summary['retried'] is None
    result = view(record)
    stats = result['retry_stats']
    assert stats['total_attempts'] is None
    assert stats['unknown_attempt_steps'] == 1
    assert stats['coverage'] == 'unknown'
    assert result['coverage'] != 'complete'


def test_missing_attempt_and_invalid_max_attempts_have_explicit_coverage():
    missing = SimpleNamespace(**{k: v for k, v in vars(step()).items() if k != 'attempt_count'})
    result = view(missing, step(attempt_count=3, max_attempts=None))
    assert result['retry_stats'] == {'total_attempts': 3, 'retried_steps': 1,
        'known_attempt_steps': 1, 'unknown_attempt_steps': 1, 'coverage': 'partial'}
    fields = result['step_health']['s']['field_coverage']
    assert fields['attempt_count']['unknown'] == 1
    assert fields['max_attempts']['unknown'] == 1
    assert result['coverage'] == 'partial'


def test_failure_categories_missing_reason_and_status_are_not_complete():
    result = view(step(status='failed', error_code='SCRIPT_TIMEOUT'),
                  step(status='failed', error_code=''), step(status=None, error_code=None))
    assert result['reason_classes'] == {'SCRIPT_TIMEOUT': 1, 'STEP_FAILED': 1}
    assert result['step_health']['s']['failure_count'] == 2
    assert result['field_coverage']['error_code']['unknown'] == 2
    assert result['field_coverage']['status']['unknown'] == 1
    assert result['coverage'] == 'partial'


def test_canonical_operation_only_comes_from_original_tool_receipt():
    record = step(effect_id='effect-not-operation', escalation_result={'operation_id': 'top-not-operation'})
    assert summarize_run_step_health(run(record))['steps'][0]['operation_id'] == ''
    assert view(record)['canonical_references']['operation_ids'] == []
    assert view(step())['canonical_references']['operation_ids'] == ['op-original']


@pytest.mark.parametrize('value', [True, False, 1.0, 2.5, '2', None, 0, -1, 101])
def test_limit_is_strict_int_and_rejected_before_read(value):
    window = Window([])
    with pytest.raises(ValueError):
        build_workflow_health_view(window, limit=value)
    assert window.calls == []


@pytest.mark.parametrize('coverage', ['partial', 'unknown'])
def test_source_coverage_never_upgraded_by_complete_fields(coverage):
    window = Window([run(step())], coverage, scan_limit=7, scan_count=3, read_limit=2, read_count=1)
    result = build_workflow_health_view(window, workflow_id='wf')
    assert result['coverage'] == coverage
    assert result['source_coverage']['coverage'] == coverage
    assert result['source_coverage']['scan_limit'] == 7
    assert result['source_coverage']['scan_count'] == 3
    assert window.calls == [('wf', 50)]


def test_source_unknown_empty_is_not_complete_zero_and_legacy_is_not_bounded_io_claim():
    assert build_workflow_health_view(Window([], 'unknown'))['coverage'] == 'unknown'
    class Legacy:
        def list_runs(self, **kwargs):
            return []
    result = build_workflow_health_view(Legacy())
    assert result['sampled_runs'] == 0
    assert result['coverage'] == 'unknown'
    assert result['source_coverage']['io_bounded'] is False


def test_prefer_readonly_sample_port_without_legacy_reads():
    class Sampler:
        def sample_runs_readonly(self, *, workflow_id, limit):
            assert (workflow_id, limit) == ('wf', 1)
            return {'runs': [run(step())], 'coverage': 'complete', 'truncated': False,
                    'scan_limit': 8, 'scan_count': 1, 'read_limit': 1, 'read_count': 1}
        def list_runs(self, **kwargs):
            pytest.fail('legacy read called')
    result = build_workflow_health_view(Sampler(), workflow_id='wf', limit=1)
    assert result['coverage'] == 'complete'
    assert result['sampled_runs'] == 1
    assert result['source_coverage']['read_count'] == 1


def test_permission_error_is_not_an_unknown_empty_success():
    class Denied:
        def list_runs(self, **kwargs):
            raise PermissionError('denied')
    with pytest.raises(PermissionError):
        build_workflow_health_view(Denied())


def test_read_failure_is_unknown_with_safe_concrete_diagnostics():
    class Broken:
        def list_runs(self, **kwargs):
            raise OSError('password=synthetic-read-secret')
    result = build_workflow_health_view(Broken())
    assert result['coverage'] == 'unknown'
    assert result['source_coverage']['error_type'] == 'OSError'
    assert result['source_coverage']['error_code'] == 'WORKFLOW_HEALTH_READ_FAILED'
    assert 'synthetic-read-secret' not in json.dumps(result)


def test_actual_synthetic_secrets_are_redacted_without_mutating_records():
    secrets = ['synthetic-bearer-987', 'synthetic-pass-654', 'synthetic-query-321']
    record = step(status='failed', error_code='SCRIPT_TIMEOUT', error_message=(
        f'Bearer {secrets[0]} password={secrets[1]} https://invalid.local/?token={secrets[2]}'))
    original = deepcopy(record)
    summary = summarize_run_step_health(run(record))
    result = view(record)
    for secret in secrets:
        assert secret not in json.dumps(summary)
        assert secret not in json.dumps(result)
    assert record == original
    assert secrets[0] in summarize_run_step_health(run(record), redact=False)['steps'][0]['error_message']


def test_valid_zero_full_fields_and_known_counts_keep_original_meaning():
    result = view(step(duration_ms=0, attempt_count=2), step(duration_ms=10, status='failed', error_code='TIMEOUT'))
    assert result['coverage'] == 'complete'
    assert result['retry_stats']['total_attempts'] == 3
    assert result['retry_stats']['retried_steps'] == 1
    assert result['step_health']['s']['duration_ms']['avg_ms'] == 5
    assert result['reason_classes'] == {'TIMEOUT': 1}


@pytest.mark.parametrize('value', [None, True, False, 0, -1, 1.5, '2'])
def test_max_attempts_invalid_is_unknown_not_complete(value):
    result = view(step(max_attempts=value))
    assert result['step_health']['s']['field_coverage']['max_attempts'] == {
        'known': 0, 'unknown': 1, 'coverage': 'unknown'}
    assert result['coverage'] == 'partial'


def test_empty_verified_window_has_zero_runs_but_no_invented_duration():
    result = build_workflow_health_view(Window([]), limit=100)
    assert result['sampled_runs'] == 0
    assert result['coverage'] == 'complete'
    assert result['step_health'] == {}
    assert result['retry_stats']['total_attempts'] == 0


def test_malformed_source_metadata_does_not_claim_complete_or_proven_io_bounds():
    window = Window([run(step())], truncated='false')
    result = build_workflow_health_view(window)
    assert result['coverage'] == 'unknown'
    assert result['source_coverage'].get('io_bounded') is not True


def test_original_health_value_assertions_on_real_record_memory_window():
    # 原 Store 的新采样端口尚未在本片整合；原数值断言用原 Record 窗口复核，
    # 不改原测试，也不把此纯值合同冒充 Store 实际 I/O/生产 HTTP。
    records = []
    for i in range(3):
        calc = step(duration_ms=100 * (i + 1), attempt_count=1 + (i == 2), effect_id=f'eff_{i}')
        calc.step_id = 'step_calc'
        post = step(duration_ms=200, status='failed' if i == 1 else 'succeeded',
                    error_code='STEP_ERROR' if i == 1 else '')
        post.step_id = 'step_post'
        record = run(calc, post)
        record.workflow_run_id = f'run_{i}'
        record.workflow_id = 'wf_analytics'
        record.status = 'failed' if i == 1 else 'succeeded'
        records.append(record)
    partial = build_workflow_health_view(Window(records[:2], 'partial', truncated=True), limit=2)
    assert partial['bounded_limit'] == 2
    assert partial['sampled_runs'] == 2
    assert partial['coverage'] == 'partial'
    assert partial['truncated'] is True
    complete = build_workflow_health_view(Window(records), workflow_id='wf_analytics', limit=50)
    assert complete['schema_version'] == 'workflow_health.v1'
    assert complete['coverage'] == 'complete'
    assert complete['truncated'] is False
    assert complete['sampled_runs'] == 3
    assert complete['run_counts']['succeeded'] == 2
    assert complete['run_counts']['failed'] == 1
    assert complete['run_counts']['running'] == 0
    calc_health = complete['step_health']['step_calc']
    assert calc_health['duration_ms'] == {'count': 3, 'min_ms': 100.0, 'max_ms': 300.0,
                                          'avg_ms': 200.0, 'p95_ms': 300.0}
    assert calc_health['retry_stats']['total_attempts'] == 4
    assert calc_health['retry_stats']['retried_steps'] == 1
    assert len(calc_health['canonical_references']['effect_ids']) == 3
    assert complete['step_health']['step_post']['failure_count'] == 1
    assert complete['step_health']['step_post']['reason_classes'] == {'STEP_ERROR': 1}
    assert complete['reason_classes'] == {'STEP_ERROR': 1}
