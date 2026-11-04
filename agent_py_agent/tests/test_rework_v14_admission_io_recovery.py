"""原C写者的设施创建、canonical发表与索引确认中断恢复。"""
import ast
import errno
import inspect
import os
import textwrap
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
from agent_py_agent.agent.gateway_parts import multiprocess_admission as admission
from agent_py_agent.agent.gateway_parts import multiprocess_executor_active_index as active
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.multiprocess_executor_ledger import ExecutorLedger
from agent_py_agent.agent.gateway_parts.multiprocess_executor_runtime import _reservation
from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
from agent_py_agent.agent.gateway_parts.request_owner_binding import frozen_request_owner
from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_gateway_claim_transition_scope import c_actual as c_actual
from agent_py_agent.tests.test_gateway_prepared_route_immutability import (
    _prepare,
)
from agent_py_agent.tests.test_gateway_prepared_route_immutability import (
    context as context,
)
from agent_py_agent.tests.test_gateway_worker_runtime import physical as physical
from agent_py_agent.tests.test_scale_native_executor_retirement import original_c_source


def ordinary_case(context):
    receipt, _ = _prepare(context.paths)
    with delivery.gateway_input_transition(context.paths, receipt.request_id):
        delivery.queue_gateway_input_locked(context.paths, receipt)
    gate = admission.coordinator_admission_gate(
        context, context.paths, SimpleNamespace(coordinator_worker_count=2))
    GatewayDrainRuntime(context).retain_admission(gate)
    ownership = (frozen_request_owner(receipt.prepared_request),
                 admission.session_scope_for_request(receipt.prepared_request))
    return gate, context.paths.inbox / (receipt.request_id + '.json'), ownership


def fd_count():
    return len(os.listdir('/proc/self/fd' if os.path.isdir('/proc/self/fd') else '/dev/fd'))


def test_original_write_responsibility_static_guard_rejects_unwrapped_fixture():
    def covers(source, scope_name, write_name, ack_name):
        tree = ast.parse(textwrap.dedent(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            contexts = [n for item in node.items for n in ast.walk(item.context_expr)]
            if not any(isinstance(n, ast.Call) and getattr(n.func, 'attr', '') == scope_name for n in contexts):
                continue
            calls = [n.func for statement in node.body for n in ast.walk(statement) if isinstance(n, ast.Call)]
            names = [n.id if isinstance(n, ast.Name) else getattr(n, 'attr', '') for n in calls]
            if write_name in names and ack_name in names:
                return True
        return False

    for method in (admission._persist_created_record, admission._persist_reused_record):
        assert covers(inspect.getsource(method), 'p3_write', '_write_gate_record_durable', 'remember_p3')
    assert not covers('def persist():\n _write_gate_record_durable()\n index.remember_p3()\n',
                      'p3_write', '_write_gate_record_durable', 'remember_p3')
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_executor_source import (
        CoordinatorExecutorSource,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_worker_commit import WorkerCommitIssuer
    for method in (CoordinatorExecutorSource._write, WorkerCommitIssuer._commit_executor):
        assert covers(inspect.getsource(method), 'executor_write', 'reserve', 'remember_executor')


@pytest.mark.parametrize('stage', ['before', 'after', 'ack', 'ack_after'])
@pytest.mark.parametrize('reused', [False, True])
def test_ordinary_p3_write_failure_does_not_quarantine_unrelated_writer(context, monkeypatch, stage, reused):
    gate, path, ownership = ordinary_case(context)
    controller = context.controller()
    index = active.index_for(controller)
    limits = AdmissionLimits(8, 8)
    if reused:
        def unavailable(*args):
            raise OSError(errno.EIO, 'simulated route publication interruption')
        with monkeypatch.context() as m:
            m.setattr(gate, '_publish_stamped', unavailable)
            with pytest.raises(OSError):
                gate.admit(path, ownership, limits)
        assert gate.occupied_snapshot()['total'] == 1
    original_write, original_ack = admission._write_record_durable, index.remember_p3
    calls = []

    def write(target, row):
        assert target.with_name(target.name + '.lock').is_file()
        if stage == 'before' and not calls:
            calls.append(stage)
            raise OSError(errno.EIO, 'simulated pre-write interruption')
        original_write(target, row)
        if stage == 'after' and not calls:
            calls.append(stage)
            raise OSError(errno.EIO, 'simulated completed-write interruption')

    def acknowledge(row, request=None):
        if stage == 'ack_after':
            original_ack(row, request)
        if not calls:
            calls.append(stage)
            raise OSError(errno.EIO, 'simulated index acknowledgement interruption')
        return original_ack(row, request)

    before = fd_count()
    with monkeypatch.context() as m:
        m.setattr(admission, '_write_record_durable', write)
        if stage.startswith('ack'):
            m.setattr(index, 'remember_p3', acknowledge)
        try:
            failed = gate.admit(path, ownership, limits)
        except OSError:
            failed = {'status': 'blocked', 'reason': 'reservation_write_failed'}
    assert calls == [stage] and failed['status'] == 'blocked'
    assert 'route' not in delivery.read_gateway_input_receipt(context.paths, path.stem).prepared_request
    ordinary = []
    decision = controller.launch_owner_writer(owner_digest(ownership[0]), lambda: ordinary.append('unrelated'))
    assert decision['status'] == 'started', (decision, controller._all_quarantined)
    assert ordinary == ['unrelated'] and controller._all_quarantined is False
    active.maintain_controller_index(controller)
    retried = gate.admit(path, ownership, limits)
    assert retried['status'] == 'admitted'
    assert gate.occupied_snapshot()['total'] == 1
    assert gate.admit(path, ownership, limits)['reservation'] == retried['reservation']
    index.check_sources()
    assert fd_count() - before == 0


def test_unknown_namespace_change_remains_refused(context):
    controller = context.controller()
    index = active.index_for(controller)
    before = fd_count()
    (context.paths.root / 'admission' / 'reservations' / 'unrecognized').mkdir()
    called = []
    decision = controller.launch_owner_writer(owner_digest(frozen_request_owner(
        _prepare(context.paths)[0].prepared_request)), lambda: called.append(True))
    assert decision['status'] == 'blocked' and not called
    assert controller._all_quarantined is True
    with pytest.raises(ValueError, match='responsibility_namespace_changed'):
        index.check_sources()
    assert fd_count() - before == 0


@pytest.mark.parametrize('stage', ['before', 'after', 'ack'])
def test_c_executor_canonical_write_and_ack_interruption_is_tracked(c_actual, monkeypatch, stage):
    with original_c_source(c_actual) as (source, claim):
        controller = source._sources[4]
        index = active.index_for(controller)
        binding = {'owner_sha': owner_digest(source.owner), 'request_id': claim.request_id,
                   'execution_attempt_id': claim.execution_attempt_id, 'lease_epoch': claim.lease_epoch,
                   'tool_call_id': 'io-recovery-call'}
        row = _reservation(source, binding, 'shell', 'io-recovery-c')
        original_write, original_ack = ExecutorLedger._write, index.remember_executor
        calls = []

        def write(ledger, record):
            if stage == 'before' and not calls:
                calls.append(stage)
                raise OSError(errno.EIO, 'simulated executor pre-write interruption')
            original_write(ledger, record)
            if stage == 'after' and not calls:
                calls.append(stage)
                raise OSError(errno.EIO, 'simulated executor completed-write interruption')

        def acknowledge(*args, **kwargs):
            calls.append(stage)
            raise OSError(errno.EIO, 'simulated executor index acknowledgement interruption')

        before = fd_count()
        with monkeypatch.context() as m:
            m.setattr(ExecutorLedger, '_write', write)
            if stage == 'ack':
                m.setattr(index, 'remember_executor', acknowledge)
            with pytest.raises(OSError):
                source.reserve(row)
        assert calls == [stage]
        result = controller.launch_owner_writer(owner_digest(source.owner), lambda: None)
        assert result['status'] == 'started' and controller._all_quarantined is False
        if stage == 'before':
            assert source.read(row['reservation_id']) is None
            assert source.reserve(row) == row
        else:
            assert source.read(row['reservation_id']) == row
            assert active._executor_key(row) in index.executors
        abandoned = source.advance(row, 'abandoned')
        assert abandoned['state'] == 'abandoned'
        index.check_sources()
        assert fd_count() - before == 0


@pytest.mark.parametrize('foreign', [False, True])
def test_retained_canonical_read_uncertainty_cannot_adopt_later_foreign_namespace(context, monkeypatch, foreign):
    gate, path, ownership = ordinary_case(context)
    controller = context.controller()
    index = active.index_for(controller)
    original_read = index._attempt_current
    writing_failed = []

    def fail_write(target, row):
        assert target.with_name(target.name + '.lock').is_file()
        writing_failed.append(True)
        raise OSError(errno.EIO, 'canonical write unavailable')

    def read(attempt):
        if writing_failed:
            raise OSError(errno.EIO, 'canonical confirmation temporarily unavailable')
        return original_read(attempt)

    before = fd_count()
    with monkeypatch.context() as m:
        m.setattr(admission, '_write_record_durable', fail_write)
        m.setattr(index, '_attempt_current', read)
        result = gate.admit(path, ownership, AdmissionLimits(8, 8))
    assert result == {'status': 'blocked', 'reason': 'reservation_write_failed'}
    assert index._write_attempts, 'uncertain original write responsibility was discarded'
    if foreign:
        (context.paths.root / 'admission' / 'reservations' / 'foreign-entry').write_text('unrecognized')
    called = []
    decision = controller.launch_owner_writer(owner_digest(ownership[0]), lambda: called.append(True))
    if foreign:
        assert decision['status'] == 'blocked' and called == []
        assert controller._all_quarantined is True
        assert index._write_attempts
    else:
        assert decision['status'] == 'started' and called == [True]
        assert controller._all_quarantined is False
        assert not index._write_attempts
        assert gate.admit(path, ownership, AdmissionLimits(8, 8))['status'] == 'admitted'
    assert fd_count() - before == 0


@pytest.mark.parametrize('stage', ['before', 'after', 'ack'])
def test_original_worker_issuer_write_recovery_with_controlled_physical_boundary(physical, monkeypatch, stage):
    # 仅物理Popen/native端点沿既有受控fixture；C租约、P4、issuer、P1和责任索引均真实。
    physical.start()
    owner = OwnerIdentity.provider_user('synthetic', 'r4-worker-write')
    digest = owner_digest(owner)
    issuer = physical.native_lanes[int(digest, 16) % physical.control.worker_count].issuer
    controller = issuer._controller
    index = active.index_for(controller)
    ledger = ExecutorLedger(physical.control.paths.root, issuer.launch.partition)
    row = _reservation(ledger, {'owner_sha': digest, 'request_id': 'r4-worker-request',
        'execution_attempt_id': 'r4-worker-attempt', 'lease_epoch': 1,
        'tool_call_id': 'r4-worker-call'}, 'shell', 'r4-worker-executor')
    original_write = ExecutorLedger._write
    calls = []

    def write(target, record):
        if stage == 'before' and not calls:
            calls.append(stage)
            raise OSError(errno.EIO, 'worker pre-write interruption')
        original_write(target, record)
        if stage == 'after' and not calls:
            calls.append(stage)
            raise OSError(errno.EIO, 'worker post-write interruption')

    def acknowledge(*args, **kwargs):
        calls.append(stage)
        raise OSError(errno.EIO, 'worker index acknowledgement interruption')

    before = fd_count()
    with monkeypatch.context() as m:
        m.setattr(ExecutorLedger, '_write', write)
        if stage == 'ack':
            m.setattr(index, 'remember_executor', acknowledge)
        with pytest.raises(OSError):
            issuer.reserve_executor(row)
    assert calls == [stage]
    assert controller.launch_owner_writer(digest, lambda: None)['status'] == 'started'
    assert controller._all_quarantined is False
    if stage == 'before':
        assert ledger.read(row['reservation_id']) is None
        assert issuer.reserve_executor(row) == row
    else:
        assert ledger.read(row['reservation_id']) == row
        assert index.executors[active._executor_key(row)]['holder'] is issuer
    assert issuer.advance_executor(row, 'abandoned')['state'] == 'abandoned'
    index.check_sources()
    assert fd_count() - before == 0
