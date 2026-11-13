"""固定资产失败经原冻结投递重试，不吞原错误或重新执行资产。"""
import json

import pytest

from agent_py_agent.agent.conversation.background_delivery import cached_owner_delivery
from agent_py_agent.tests.test_plan_w8_schedule_target import asset_case


@pytest.mark.parametrize('kind', ['script', 'workflow'])
def test_failed_target_notifies_once_and_retry_preserves_failure(tmp_path, monkeypatch, kind):
    case = asset_case(tmp_path, kind, origin_channel='feishu', delivery={'mode': 'origin'},
                      script_code='raise RuntimeError("synthetic-target-failure")')
    from agent_py_agent.agent.workflows import ScriptRunner
    calls = []
    original_run = ScriptRunner.run_script
    def run(self, *args, **kwargs):
        calls.append(args[0] if args else kwargs.get('script_id'))
        return original_run(self, *args, **kwargs)
    monkeypatch.setattr(ScriptRunner, 'run_script', run)
    channel = case.scheduler.runtime.channels
    case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    run = case.agent.scheduler_repository.queued_runs(now=1000)[0]
    case.scheduler.tick(now=1001)
    assert len(channel.attempts) == 1
    active = case.agent.scheduler_repository.get_active_run(run['run_id'])
    assert active and active['target_outcome']['status'] == 'failed'
    code = active['target_outcome']['error_code']
    assert code and not code.startswith('SCHEDULED_TASK_')
    pending = next(x for x in case.agent.conversation_store.wakes.pending() if x.root_task_id == run['run_id'])
    assert cached_owner_delivery(pending) is not None
    original = channel.attempts[0][0].idempotency_key
    channel.status = 'sent'
    case.scheduler.tick(now=1040)
    rows, errors = case.agent.scheduler_repository.history()
    assert not errors and len(rows) == 1, rows
    assert rows[0]['status'] == 'failed' and rows[0]['error_code'] == code
    assert rows[0]['delivery_status'] == 'sent'
    assert len(channel.attempts) == 2 and channel.attempts[1][0].idempotency_key == original
    assert case.agent.backend.prompts == []
    assert len(calls) == 1
    assert 'synthetic-target-failure' in json.dumps(rows[0]['structured_result'])


@pytest.mark.parametrize('delivery_mode', ['none', 'origin'])
def test_failed_target_no_delivery_or_cancelled_retry_does_not_send(tmp_path, delivery_mode):
    case = asset_case(tmp_path, origin_channel='feishu', delivery={'mode': delivery_mode},
                      script_code='raise ValueError("failure-before-notification")')
    case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    run = case.agent.scheduler_repository.queued_runs(now=1000)[0]
    case.scheduler.tick(now=1001)
    channel = case.scheduler.runtime.channels
    if delivery_mode == 'none':
        assert channel.attempts == []
        rows, _ = case.agent.scheduler_repository.history()
        assert rows[0]['status'] == 'failed'
    else:
        assert len(channel.attempts) == 1
        case.agent.conversation_store.tasks.update_status({'task_id': run['run_id'], 'status': 'cancelled',
            'expected_status': 'abandoned', 'now': 1002})
        channel.status = 'sent'
        case.scheduler.tick(now=1040)
        assert len(channel.attempts) == 1


def test_failed_target_initial_delivery_success_still_records_failed_execution(tmp_path):
    case = asset_case(tmp_path, origin_channel='feishu', delivery={'mode': 'origin'},
                      script_code='raise RuntimeError("initial-failure")')
    case.scheduler.runtime.channels.status = 'sent'
    case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    case.scheduler.tick(now=1001)
    rows, errors = case.agent.scheduler_repository.history()
    assert not errors and len(rows) == 1
    assert rows[0]['status'] == 'failed' and rows[0]['delivery_status'] == 'sent'
    assert rows[0]['error_code'] == rows[0]['target_outcome']['error_code']
    assert len(case.scheduler.runtime.channels.attempts) == 1


@pytest.mark.parametrize('failure', [False, True])
@pytest.mark.parametrize('cancelled', [False, True])
def test_saved_target_result_before_freeze_recovers_without_execution(tmp_path, monkeypatch, failure, cancelled):
    from agent_py_agent.agent.scheduler import execution
    from agent_py_agent.agent.workflows import ScriptRunner
    case = asset_case(tmp_path, origin_channel='feishu', delivery={'mode': 'origin'},
        script_code='raise RuntimeError("saved-failure")' if failure else None)
    calls = []
    original_run = ScriptRunner.run_script
    def run(self, *args, **kwargs):
        calls.append(1)
        return original_run(self, *args, **kwargs)
    monkeypatch.setattr(ScriptRunner, 'run_script', run)
    commit = execution._commit_target_result
    attempts = []
    def fail_before_freeze(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError('simulated process exit before frozen delivery')
        return commit(*args, **kwargs)
    monkeypatch.setattr(execution, '_commit_target_result', fail_before_freeze)
    case.scheduler.runtime.channels.status = 'sent'
    case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    run_id = case.agent.scheduler_repository.queued_runs(now=1000)[0]['run_id']
    with pytest.raises(OSError, match='simulated process exit'):
        case.scheduler.tick(now=1001)
    assert len(calls) == 1 and case.scheduler.runtime.channels.attempts == []
    case.scheduler.tick(now=1002)
    assert len(attempts) == 1  # 原 wake attempt 退避期内不热循环。
    if cancelled:
        task = case.agent.conversation_store.tasks.load(run_id)
        case.agent.conversation_store.tasks.update_status({'task_id': run_id, 'status': 'cancelled',
            'expected_status': task.status, 'now': 1002})
    case.scheduler.tick(now=1040)
    rows, errors = case.agent.scheduler_repository.history()
    assert not errors and len(rows) == 1, (case.agent.scheduler_repository.get_active_run(run_id), case.agent.conversation_store.wakes.pending(), attempts)
    assert rows[0]['status'] == ('cancelled' if cancelled else 'failed' if failure else 'done'), rows[0]
    assert rows[0]['delivery_status'] == ('suppressed' if cancelled else 'sent')
    assert len(calls) == 1 and len(case.scheduler.runtime.channels.attempts) == (0 if cancelled else 1)


@pytest.mark.parametrize('output', ['plain tool rejection', ''])
def test_non_json_failure_preserves_original_error(tmp_path, monkeypatch, output):
    from types import SimpleNamespace

    from agent_py_agent.agent.scheduler import execution
    case = asset_case(tmp_path)
    monkeypatch.setattr(execution, '_execute_target_tool', lambda *args, **kwargs:
        (SimpleNamespace(result=SimpleNamespace(ok=False, error_code='EXACT_TOOL_REJECTION', details={})), output))
    case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    case.scheduler.tick(now=1001)
    rows, errors = case.agent.scheduler_repository.history()
    assert not errors and len(rows) == 1
    assert rows[0]['status'] == 'failed'
    assert rows[0]['error_code'] == 'EXACT_TOOL_REJECTION'
    assert rows[0]['structured_result'] == output


def test_success_business_field_is_preserved(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.scheduler import execution
    case = asset_case(tmp_path)
    payload = {'scheduler_execution_result': {'business': 'owned'}, 'value': 7}
    monkeypatch.setattr(execution, '_execute_target_tool', lambda *args, **kwargs:
        (SimpleNamespace(result=SimpleNamespace(ok=True, error_code='')), json.dumps(payload)))
    case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    case.scheduler.tick(now=1001)
    rows, errors = case.agent.scheduler_repository.history()
    assert not errors and rows[0]['structured_result'] == payload


def test_failure_delivery_rejects_cross_thread_and_forged_wake(tmp_path):
    from copy import deepcopy

    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
    from agent_py_agent.agent.scheduler.execution import scheduled_target_failure

    case = asset_case(tmp_path, origin_channel='feishu', delivery={'mode': 'origin'},
        script_code='raise RuntimeError("scope-failure")')
    case.agent.scheduler_service.enqueue_ready_runs(now=1000)
    run = case.agent.scheduler_repository.queued_runs(now=1000)[0]
    case.scheduler.tick(now=1001)
    wake = case.agent.conversation_store.wakes.pending_one(run['wake_signal_id']).to_dict()
    request = BackgroundRunRequest(thread_id=run['thread_id'], task_id=run['run_id'],
        reason='scheduled_job_due', wake_signal=wake)
    assert scheduled_target_failure(case.agent, request) is not None
    for field, value in [('thread_id', 'other-thread'), ('wake_signal_id', 'other-wake'),
                         ('root_task_id', 'other-task')]:
        forged = deepcopy(wake)
        forged[field] = value
        request = BackgroundRunRequest(thread_id=run['thread_id'], task_id=run['run_id'],
            reason='scheduled_job_due', wake_signal=forged)
        assert scheduled_target_failure(case.agent, request) is None
    request = BackgroundRunRequest(thread_id='other-thread', task_id=run['run_id'],
        reason='scheduled_job_due', wake_signal=wake)
    assert scheduled_target_failure(case.agent, request) is None
