"""物理进程树退出与外部业务结果UNKNOWN分别核验，绝不重放工具。"""
import copy
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.session_task_stop import _attempt_physical_operations_exited


def source():
    process = {'cleanup_schema': 'foreground-process-cleanup.v1', 'status': 'cancelled',
               'cleanup_confirmed': True, 'pid': 4100, 'birth_token': 'original-birth',
               'termination': {'method': 'SIGTERM', 'confirmed': True, 'return_code': -15,
                               'observed_processes': 2, 'unresolved_pids': []}}
    return {'operation_id': 'op-original', 'attempt_id': 'attempt-original', 'agent_run_id': 'agent-original',
            'status': 'UNKNOWN', 'outcome_json': json.dumps({'result': {'handler_executed': True,
              'effect_outcome': 'unknown', 'result_envelope': {'process': process}}})}


def repository(row, status='UNKNOWN'):
    return SimpleNamespace(unsettled_attempt_operations=lambda _: [{'operation_id': 'op-original', 'status': status}],
                           get_operation=lambda _: row)


def test_host_process_receipt_proves_physical_exit_without_settling_business_unknown():
    row = source()
    before = copy.deepcopy(row)
    assert _attempt_physical_operations_exited(repository(row), 'attempt-original', 'agent-original')
    assert row == before and row['status'] == 'UNKNOWN'
    assert json.loads(row['outcome_json'])['result']['effect_outcome'] == 'unknown'


@pytest.mark.parametrize('mutation', ['attempt', 'run', 'status', 'legacy', 'unconfirmed', 'pid', 'birth',
                                    'unresolved', 'observed', 'handler', 'corrupt', 'missing'])
def test_unconfirmed_mutated_or_other_attempt_receipts_never_release_resources(mutation):
    row = source()
    result = json.loads(row['outcome_json'])
    process = result['result']['result_envelope']['process']
    if mutation in {'attempt', 'run', 'status'}:
        row[{'attempt': 'attempt_id', 'run': 'agent_run_id', 'status': 'status'}[mutation]] = 'other'
    elif mutation == 'legacy':
        process.pop('cleanup_schema')
    elif mutation == 'unconfirmed':
        process['termination']['confirmed'] = False
    elif mutation == 'pid':
        process['pid'] = 0
    elif mutation == 'birth':
        process['birth_token'] = ''
    elif mutation == 'unresolved':
        process['termination']['unresolved_pids'] = [4101]
    elif mutation == 'observed':
        process['termination']['observed_processes'] = 0
    elif mutation == 'handler':
        result['result']['handler_executed'] = False
    row['outcome_json'] = '{' if mutation == 'corrupt' else json.dumps(result)
    assert not _attempt_physical_operations_exited(repository(None if mutation == 'missing' else row),
                                                  'attempt-original', 'agent-original')


@pytest.mark.parametrize('status', ['EXECUTING', 'CLAIMED'])
def test_live_operation_never_becomes_physically_exited_from_old_receipt(status):
    assert not _attempt_physical_operations_exited(repository(source(), status), 'attempt-original', 'agent-original')
