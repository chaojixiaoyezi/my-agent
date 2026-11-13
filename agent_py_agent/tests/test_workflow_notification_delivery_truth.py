"""通知落本地日志不代表外送；复用真实通知端口的排队与无通道状态。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.workflows.datasource_runtime import bind_asset_im_notifier
from agent_py_agent.agent.workflows.notification_steps import (
    execute_notification_step,
    notify_unattended_run_failure,
)
from agent_py_agent.tests.test_wf_05_06_notifications_and_locks import _make_run, _make_step


@pytest.mark.parametrize('delivery', ['no_channel', 'queued'])
def test_real_native_fallback_preserves_delivery_truth_and_dedupes_queue(tmp_path, delivery):
    calls = []
    engine = SimpleNamespace(owner_home=tmp_path)
    run, step = _make_run(tmp_path), _make_step()
    def notifier(notice):
        calls.append(notice)
        return delivery
    with bind_asset_im_notifier(tmp_path, notifier):
        execute_notification_step(engine=engine, run=run, workflow=None, step=step,
            spec={'notification_spec': {'channel': 'im', 'template': '步骤通知'}}, context=None, outputs={})
        assert (tmp_path / 'workspace/runtime/notifications/im_alerts.jsonl').exists()
        if delivery == 'no_channel':
            assert step.status == 'failed'
            assert step.output_snapshot['delivery_result']['delivery_status'] == 'no_channel'
            assert 'WORKFLOW_NOTIFICATION_NO_CHANNEL' in step.error_message
            assert 'delivered_at' not in step.output_snapshot
        else:
            assert step.status == 'succeeded' and step.output_snapshot['status'] == 'queued'
            assert 'queued_at' in step.output_snapshot and 'delivered_at' not in step.output_snapshot
            execute_notification_step(engine=engine, run=run, workflow=None, step=_make_step(),
                spec={'notification_spec': {'channel': 'im', 'template': '步骤通知'}}, context=None, outputs={})
            assert len(calls) == 1


@pytest.mark.parametrize('delivery', ['no_channel', 'queued'])
def test_unattended_failure_native_fallback_does_not_claim_unsent(tmp_path, delivery):
    run = _make_run(tmp_path)
    run.status, run.failed_step_id = 'failed', 'original-step'
    run.trigger_source = 'schedule'
    with bind_asset_im_notifier(tmp_path, lambda _notice: delivery):
        result = notify_unattended_run_failure(engine=SimpleNamespace(owner_home=tmp_path), run=run)
    assert run.status == 'failed' and run.failed_step_id == 'original-step'
    assert result['status'] == ('queued' if delivery == 'queued' else 'notification_failed')
    assert result['delivery_result']['delivery_status'] == delivery
    if delivery == 'no_channel':
        assert result['error_code'] == 'WORKFLOW_NOTIFICATION_NO_CHANNEL'
    else:
        assert 'notified_at' not in result and result['queued_at']


@pytest.mark.parametrize('surface', ['step', 'run'])
@pytest.mark.parametrize('delivery', ['queued', 'no_channel'])
def test_native_delivery_preserves_local_record_failures(tmp_path, monkeypatch, surface, delivery):
    from agent_py_agent.agent.workflows import datasource_runtime, notification_steps

    sent = []
    def local_failure(*args, **kwargs):
        raise OSError('local receipt storage failed')
    monkeypatch.setattr(datasource_runtime, 'append_text_beneath', local_failure)
    monkeypatch.setattr(notification_steps, 'write_text_atomic_beneath', local_failure)
    engine = SimpleNamespace(owner_home=tmp_path)
    run, step = _make_run(tmp_path), _make_step()
    run.status, run.failed_step_id = 'failed', 'original-step'
    run.trigger_source = 'schedule'
    def sender(notice):
        sent.append(notice)
        return delivery
    def invoke():
        if surface == 'run':
            return notify_unattended_run_failure(engine=engine, run=run)
        execute_notification_step(engine=engine, run=run, workflow=None, step=step,
            spec={'notification_spec': {'channel': 'im', 'template': '步骤通知'}}, context=None, outputs={})
        assert step.status == ('succeeded' if delivery == 'queued' else 'failed')
        return step.output_snapshot
    with bind_asset_im_notifier(tmp_path, sender):
        receipt = invoke()
        assert receipt['delivery_result'] == {'recorded': False, 'delivery_status': delivery,
                                             'record_error': 'NOTIFICATION_STORE_UNAVAILABLE'}
        assert 'delivered_at' not in receipt and 'notified_at' not in receipt
        if delivery == 'queued':
            assert receipt['status'] == 'queued'
            assert receipt['receipt_persistence_error']['error_type'] == 'OSError'
            assert invoke() == receipt
        elif surface == 'run':
            assert receipt['error_code'] == 'NOTIFICATION_STORE_UNAVAILABLE'
        else:
            assert 'NOTIFICATION_STORE_UNAVAILABLE' in step.error_message
    assert len(sent) == 1
    assert run.status == 'failed' and run.failed_step_id == 'original-step'
