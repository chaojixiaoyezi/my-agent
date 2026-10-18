"""原waiter负向退休不等op，permission/JSON忙保准确未结，批准能力始终关闭。"""
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision
from agent_py_agent.agent.conversation import agent_tool_approval as approval
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts import private_approval_control as private
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.tests.test_coordinator_approval_decision import (
    mode,
    poll_original_control,
    seed,
    waiting_case,
)


# LLM: 只让另一真实线程合法占用原锁，释放/原Thread.join全在finally；不改permission行/执行scope或替代原writer。
# 函数用途: 复现原等待线程面临的正常文件锁竞争，不留下后台设施。
@contextmanager
def held_original_lock(path):
    entered, release = threading.Event(), threading.Event()
    failures = []

    def hold():
        try:
            with locked_json_path(path):
                entered.set()
                assert release.wait(5)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=hold, name='approval-original-lock-holder')
    thread.start()
    assert entered.wait(5)
    try:
        yield
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive() and not failures


# LLM: 输入由原HTTP→P5控制泵受理，测试不制造request/claim/RunParams或手改行；None仅真的没有输入。
# 函数用途: 选择是否先受理一次原私卡点击，并保留原输入引用。
def queue_original(case, binding, handle, body, monkeypatch, *, has_input):
    if not has_input:
        return None
    body['decision'] = 'approved'
    status, queued = seed.post(case[4], case[2], body)
    assert status == 200 and queued['status'] == 'queued'
    poll_original_control(case, binding, monkeypatch, prepare=True)
    assert json.loads(handle.path.read_text())['decision_input']['operation_id'] == queued['operation_id']
    return queued


@pytest.mark.parametrize('has_input', [False, True])
@pytest.mark.parametrize('lock_kind', ['permission', 'json'])
def test_busy_retirement_is_timely_typed_and_keeps_original_obligation(tmp_path, monkeypatch, has_input, lock_kind):
    with waiting_case(tmp_path, monkeypatch) as (case, binding, handle, body):
        queue_original(case, binding, handle, body, monkeypatch, has_input=has_input)
        row_before = handle.path.read_bytes()
        p3 = reservation_file(case[4].paths, binding.claim.request_id)
        p3_before = p3.read_bytes()
        path = approval._transition_path(handle.path) if lock_kind == 'permission' else handle.path
        with held_original_lock(path), monkeypatch.context() as scoped:
            scoped.setattr(mode, '_WORKER_PARTITION', binding.snapshot.bootstrap.launch.partition)
            started = time.monotonic()
            with pytest.raises(approval.ApprovalRetirementUnconfirmedError) as caught:
                approval.wait_for_agent_tool_approval(handle, cancellation_token=SimpleNamespace(cancelled=True))
            assert time.monotonic() - started < 0.5
        error = caught.value
        fact = approval.approval_retirement_obligation(error)
        assert fact is error.obligation
        assert fact.path is handle.path and fact.scope is handle.scope and fact.request is handle.request
        assert fact.decision.decision == 'cancelled' and not fact.decision.approved
        assert bool(fact.input_refs) is has_input
        assert error.reason == ('permission_transition_busy' if lock_kind == 'permission' else 'permission_json_busy')
        assert handle.path.read_bytes() == row_before and p3.read_bytes() == p3_before
        assert not (case[4].paths.terminal / binding.claim.path.name).exists()
        with pytest.raises(ValueError, match='original waiter is unavailable'):
            handle.decision_input()
        with pytest.raises(FrozenInstanceError):
            fact.path = tmp_path
        assert approval.approval_retirement_obligation(RuntimeError(error.error_code)) is None
        # 裸wait已明确未结；本片没有消费/重试API，不能把批准callback关闭说成责任已结清。
        assert fact is approval.approval_retirement_obligation(error)


@pytest.mark.parametrize('state', ['no_input', 'committed', 'prior_decided'])
def test_retirement_four_state_result_preserves_prior_decision(tmp_path, monkeypatch, state):
    with waiting_case(tmp_path, monkeypatch) as (case, binding, handle, body):
        queued = queue_original(case, binding, handle, body, monkeypatch, has_input=state != 'no_input')
        seen = []
        original = approval._try_retire_approval_permission

        def observe(*args):
            result = original(*args)
            seen.append(result)
            return result

        with monkeypatch.context() as scoped:
            scoped.setattr(mode, '_WORKER_PARTITION', binding.snapshot.bootstrap.launch.partition)
            if state == 'prior_decided':
                assert handle.decision_input().approved
                saved = handle.path.read_bytes()
            scoped.setattr(approval, '_try_retire_approval_permission', observe)
            decision = approval.wait_for_agent_tool_approval(handle, cancellation_token=SimpleNamespace(cancelled=True))
        assert decision.decision == 'cancelled' and not decision.approved
        assert len(seen) == 1 and seen[0].status == state
        if state == 'no_input':
            assert not handle.path.exists()
            return
        row = json.loads(handle.path.read_text())
        if state == 'prior_decided':
            assert handle.path.read_bytes() == saved and seen[0].prior_decision.approved
            assert 'decision_origin' not in row
        else:
            assert row['decision_origin'] == 'waiter_retirement' and row['decision'] == decision.to_dict()
        poll_original_control(case, binding, monkeypatch)
        receipt = ops.read_gateway_control_operation(case[4].paths, queued['operation_id'], read_only=True)
        assert receipt.state == 'completed'
        assert receipt.result['details']['decision'] == ('approved' if state == 'prior_decided' else 'cancelled')


@pytest.mark.parametrize('reason', ['cancel', 'timeout', 'invalid'])
def test_negative_retirement_with_busy_op_preserves_input_and_never_approves(tmp_path, monkeypatch, reason):
    with waiting_case(tmp_path, monkeypatch) as (case, binding, handle, body):
        queued = queue_original(case, binding, handle, body, monkeypatch, has_input=True)
        raw_input = json.loads(handle.path.read_text())['decision_input']
        op_path = ops.gateway_control_operation_path(case[4].paths, queued['operation_id'])
        # operation transition有独立原锁身份，复用其公开锁上下文而非从文件名猜锁路径。
        entered, release = threading.Event(), threading.Event()
        failures = []

        def hold():
            try:
                with ops.gateway_control_operation_transition(case[4].paths, queued['operation_id']):
                    entered.set()
                    assert release.wait(5)
            except BaseException as error:
                failures.append(error)

        holder = threading.Thread(target=hold, name='retirement-operation-holder')
        holder.start()
        assert entered.wait(5)
        try:
            with monkeypatch.context() as scoped:
                scoped.setattr(mode, '_WORKER_PARTITION', binding.snapshot.bootstrap.launch.partition)
                if reason == 'timeout':
                    scoped.setattr(approval, 'time', SimpleNamespace(time=lambda: handle.request.requested_at + 100000,
                                                                   sleep=time.sleep))
                elif reason == 'invalid':
                    claims = case[0].conversation_store.claims
                    prior = claims.load_report(handle.scope.thread_id)[0]
                    claims.finish({'thread_id': handle.scope.thread_id, 'claim_id': prior['claim_id'], 'status': 'finished'})
                    claims.acquire({'thread_id': handle.scope.thread_id, 'task_id': handle.scope.run_id,
                                    'reason': 'thread_goal_continue'})
                before_op = op_path.read_bytes()
                started = time.monotonic()
                decision = approval.wait_for_agent_tool_approval(handle,
                    cancellation_token=SimpleNamespace(cancelled=reason == 'cancel'))
                assert time.monotonic() - started < 0.5 and not decision.approved
                assert op_path.read_bytes() == before_op
        finally:
            release.set()
            holder.join(5)
        assert not holder.is_alive() and not failures
        expected = {'cancel': 'cancelled', 'timeout': 'denied', 'invalid': 'unavailable'}[reason]
        assert decision.decision == expected
        if reason == 'timeout':
            assert decision.reason_code == 'APPROVAL_TIMEOUT'
        row = json.loads(handle.path.read_text())
        assert row['status'] == 'decided' and row['decision'] == decision.to_dict()
        assert row['decision_origin'] == 'waiter_retirement' and row['decision_input'] == raw_input
        poll_original_control(case, binding, monkeypatch)
        receipt = ops.read_gateway_control_operation(case[4].paths, queued['operation_id'], read_only=True)
        assert receipt.state == 'completed' and receipt.result['details']['ok'] is False


def test_unknown_receipt_cannot_be_completed_by_later_pure_retirement(tmp_path, monkeypatch):
    with waiting_case(tmp_path, monkeypatch) as (case, binding, handle, body):
        body['decision'] = 'approved'
        status, queued = seed.post(case[4], case[2], body)
        assert status == 200
        submit = private._submit_approval_input

        def lost_response(*args, **kwargs):
            submit(*args, **kwargs)
            raise OSError('ordinary synthetic response loss')

        with monkeypatch.context() as scoped:
            scoped.setattr(private, '_submit_approval_input', lost_response)
            poll_original_control(case, binding, monkeypatch, prepare=True)
        receipt = ops.read_gateway_control_operation(case[4].paths, queued['operation_id'], read_only=True)
        assert receipt.state == 'terminal_unknown'
        with monkeypatch.context() as scoped:
            scoped.setattr(mode, '_WORKER_PARTITION', binding.snapshot.bootstrap.launch.partition)
            decision = approval.wait_for_agent_tool_approval(handle, cancellation_token=SimpleNamespace(cancelled=True))
        row = json.loads(handle.path.read_text())
        assert row['decision_origin'] == 'waiter_retirement' and row['decision'] == decision.to_dict()
        poll_original_control(case, binding, monkeypatch)
        assert ops.read_gateway_control_operation(case[4].paths, queued['operation_id'], read_only=True) == receipt
        assert seed.post(case[4], case[2], body)[1]['status'] == 'unknown'
        assert not (case[4].paths.terminal / binding.claim.path.name).exists()


@pytest.mark.parametrize('decision', ['approved', 'approved_session', 'approved_owner', 'denied'])
def test_pure_retirement_leaf_rejects_authorization_or_normal_denial(tmp_path, monkeypatch, decision):
    with waiting_case(tmp_path, monkeypatch) as (_case, _binding, handle, _body):
        before = handle.path.read_bytes()
        with pytest.raises(ValueError, match='只能保存原负向'):
            approval._retire_approval_wait(handle, ToolApprovalDecision(handle.request.permission_id, decision))
        assert handle.path.read_bytes() == before
