"""真实 claim 收尾保留持久任务，使用原输入/指导状态机。"""
import pytest

from agent_py_agent.agent.conversation import background_claim as claims
from agent_py_agent.agent.conversation import background_execution as execution
from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.tests.test_background_execution_ports import claimed
from agent_py_agent.tests.test_gateway_background_active_input import (
    actual as actual,
)
from agent_py_agent.tests.test_gateway_background_active_input import (
    bound_paths,
    gateway_scope,
    model_runner,
    prepared_input,
)
from agent_py_agent.tests.test_gateway_background_active_input import (
    c_actual as c_actual,
)
from agent_py_agent.tests.test_gateway_background_active_input import (
    case as case,
)
from agent_py_agent.tests.test_gateway_background_active_input import (
    context as context,
)
from agent_py_agent.tests.test_gateway_background_active_input import (
    transport_case as transport_case,
)


@pytest.mark.parametrize('crash_projection', [False, True])
def test_ended_slice_pending_guidance_recovers_without_finishing_task(transport_case, context, tmp_path, monkeypatch, crash_projection):
    kind, case = transport_case
    _origin, _gateway_claim, agent, store, thread, *_ = case
    thread = store.threads.get_or_create({'canonical_user_id': 'owner', 'channel': 'internal',
        'channel_conversation_id': 'transport', 'channel_user_id': 'owner'})
    case = (*case[:4], thread, *case[5:])
    if kind == 'C':
        monkeypatch.setattr(context.config, 'conversation_workspace', str(store.storage.root))
    receipts = []
    def action(bound):
        receipt = prepared_input(case, bound)
        receipts.append(receipt)
        bound.pending_turn_input_source.drain(agent, bound)
        assert store.guidance.receipt(receipt.guidance_dedupe_key).status == 'pending'
        return 'result'
    params = model_runner(case, tmp_path, monkeypatch, action)
    deps, args, row, _policy = claimed(case, lambda _: execution._run_background_model_attempt(agent, '任务', params)[0])
    if crash_projection:
        from agent_py_agent.agent.gateway_parts.multiprocess_background_input import (
            BackgroundPendingInput,
        )
        monkeypatch.setattr(BackgroundPendingInput, '_settle_input', lambda *_: None)
    with gateway_scope(kind, case):
        assert claims.run_with_heartbeat(deps, row['claim_id'], args, claim_scope_id=row['claim_scope_id']) == 'result'
    assert store.tasks.load('ports-task').status == 'active'
    assert store.claims.load(thread.thread_id)['status'] == 'finished'
    # C 走真实协调者消费者；W 的持久 Store 恢复单独验证，不冒充协调者来源。
    from agent_py_agent.agent.gateway_parts import multiprocess_mode
    monkeypatch.setattr(multiprocess_mode, '_WORKER_PARTITION', None)
    if kind == 'C':
        from types import SimpleNamespace

        from agent_py_agent.agent.gateway_parts.multiprocess_input_reconcile import (
            reconcile_coordinator_queued_inputs,
        )
        scan_context = SimpleNamespace(agent=agent, paths=case[0].paths, control_plane=context,
            drain_runtime=case[0]._ordinary_admission_origin[0], coordinator_worker_count=context.worker_count)
        if crash_projection:
            import threading
            import time
            held, release = threading.Event(), threading.Event()
            def hold_turn():
                with store.guidance.ledger.turn_guard('bg-invocation'):
                    held.set()
                    release.wait(10)
            holder = threading.Thread(target=hold_turn)
            holder.start()
            assert held.wait(5)
            started = time.monotonic()
            try:
                with pytest.raises(BlockingIOError):
                    reconcile_coordinator_queued_inputs(scan_context, case[0].paths, case[0])
                assert time.monotonic() - started < 1.0
            finally:
                release.set()
                holder.join(5)
            assert not holder.is_alive()
        summary = reconcile_coordinator_queued_inputs(scan_context, case[0].paths, case[0])
        assert summary['errors'] == 0, summary
        assert summary['queued'] >= 1, summary
    else:
        state = inputs._reconcile_bound_input(bound_paths(case), store,
            inputs.read_gateway_input_receipt(bound_paths(case), receipts[0].request_id))
        assert state == 'queued'

    receipt = inputs.read_gateway_input_receipt(bound_paths(case), receipts[0].request_id)
    assert receipt.state == 'queued'
    assert store.guidance.receipt(receipt.guidance_dedupe_key).status == 'rejected'
    assert (bound_paths(case).inbox / (receipt.request_id + '.json')).exists()


def test_unsettled_error_never_has_blank_identity():
    error = inputs.GatewayInputReconcileUnsettledError({'errors': 1, 'last_error': {
        'request_id': '<cursor_commit>', 'error_type': 'PermissionError', 'category': 'io',
        'context': 'gateway.input.reconcile.cursor', 'stage': 'cursor_commit'}})
    assert 'last=<cursor_commit>:PermissionError' in str(error)


@pytest.mark.parametrize('stage', ['receipt', 'cursor_commit'])
def test_counted_ordinary_failure_has_typed_bounded_stage(tmp_path, monkeypatch, stage):
    from agent_py_agent.tests.test_steer_delivery_recovery import _background_input
    agent, paths, request_id = _background_input(tmp_path, 'original-target')
    def fail(*_args, **_kwargs):
        raise PermissionError('secret message must not appear in summary')
    if stage == 'receipt':
        monkeypatch.setattr(inputs, '_reconcile_receipt_path', fail)
    else:
        monkeypatch.setattr(inputs, '_commit_reconcile_cursor', fail)
    summary = inputs.reconcile_gateway_input_receipts(paths, agent)
    assert summary['errors'] > 0
    assert summary['last_error']['stage'] == stage
    assert summary['last_error']['error_type'] == 'PermissionError'
    assert summary['last_error']['request_id'] == (request_id if stage == 'receipt' else '<cursor_commit>')
    assert 'secret' not in str(summary)
    with pytest.raises(inputs.GatewayInputReconcileUnsettledError) as error:
        inputs.raise_if_input_reconcile_unsettled(summary)
    assert 'last=:' not in str(error.value)
