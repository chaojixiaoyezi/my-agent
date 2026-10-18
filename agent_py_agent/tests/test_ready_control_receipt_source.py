# LLM: 使用原实际Agent、lease、操作T与固定receipt叶；不补Root或修改真人memory正例，不执行模型/服务/native。
# 模块用途: 验证ready lease-only回执分界、晚引用变化、原状态图与部分写失败保持。
import copy
import errno
import threading
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.conversation.control_commands import ConversationControlResult
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts import multiprocess_local_control as local
from agent_py_agent.agent.gateway_parts import multiprocess_takeover_runtime as takeover
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import (
    bind_coordinator_generation,
    coordinator_guard,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.user_space.owner_resolver import owner_identity_from_config
from agent_py_agent.tests.test_memory_candidate_control import memory_control  # noqa: F401


# LLM: 原fixture实际Agent保持原对象；这里只构造固定合法receipt和原T/lease/source，不授memory业务封印或执行权限。
# 函数用途: 为固定写叶正反例提供原factory活栈，full模式取得原真实controller而非替身Root。
@contextmanager
def _source_case(control, mode='ready'):
    agent = (SimpleNamespace(config=control.agent.config, home_paths=control.agent.home_paths) if mode == 'fake'
             else None if mode == 'missing' else control.agent)
    scope = GatewayControlScope('alice', 'feishu', 'private-a', {'message_id': 'ready-receipt-test'},
                                resolved_owner=owner_identity_from_config(control.agent.config))
    operation, digest = ops.gateway_control_operation_identity(scope, '/memory candidates')
    receipt = ops._new_control_operation_receipt(scope, 'memory', '/memory candidates', operation, digest)
    with coordinator_guard(control.paths.root, 2) as lease:
        bind_coordinator_generation(control.paths.root, 2, 'ready-receipt-generation')
        if mode == 'full':
            takeover.prepare_coordinator_takeover(control.paths, 2, lease.c_run_id)
        with ops.gateway_control_operation_transition(control.paths, operation):
            with local.local_control_operation_scope(agent, control.paths, receipt) as source:
                yield SimpleNamespace(source=source, lease=lease, receipt=receipt,
                                      path=ops.gateway_control_operation_path(control.paths, operation))


def test_ready_receipt_uses_original_operation_transition_without_controller(memory_control, monkeypatch):  # noqa: F811
    with _source_case(memory_control) as case:
        original = local._write_original_control_receipt
        visits = []

        def observe(source, path, payload, **kwargs):
            with ops.try_gateway_control_operation_transition(source.paths, case.receipt.operation_id) as acquired:
                assert not acquired
            visits.append(path)
            return original(source, path, payload, **kwargs)

        monkeypatch.setattr(local, '_write_original_control_receipt', observe)
        assert case.source.commit_control_receipt(case.receipt.to_dict()) == case.receipt
        assert visits == [case.path]
        assert case.lease.controller is None and case.lease.control_plane is None
        assert ops.read_gateway_control_operation(memory_control.paths, case.receipt.operation_id) == case.receipt


@pytest.mark.parametrize('changed', ['agent', 'paths', 'context', 'lease', 'controller', 'owner', 'home', 'pin', 'closing', 'generation', 'control_plane'])
def test_ready_receipt_rejects_changed_original_facts(memory_control, monkeypatch, tmp_path, changed):  # noqa: F811
    with _source_case(memory_control) as case, monkeypatch.context() as patcher:
        source, lease = case.source, case.lease
        if changed == 'agent':
            patcher.setattr(source, 'agent', SimpleNamespace())
        elif changed == 'paths':
            patcher.setattr(source, 'paths', gateway_paths_from_root(tmp_path / 'other-gateway'))
        elif changed == 'context':
            patcher.setattr(source, 'context', object())
        elif changed == 'lease':
            patcher.setattr(source, 'lease', copy.copy(lease))
        elif changed == 'controller':
            patcher.setattr(source, 'controller', object())
        elif changed == 'owner':
            patcher.setattr(source.agent.config, 'my_agent_owner_id', 'bob')
        elif changed == 'home':
            patcher.setattr(source.agent, 'home_paths', replace(source.agent.home_paths, owner_home_dir=tmp_path / 'foreign'))
        elif changed == 'pin':
            patcher.setattr(lease, '_commit_pins', set())
        elif changed == 'closing':
            patcher.setattr(lease, '_closing', True)
        elif changed == 'generation':
            patcher.setattr(lease, '_generation', 'other-generation')
        else:
            patcher.setattr(lease, 'control_plane', object())
        with pytest.raises(local.LocalControlCoordinatorUnavailable):
            source.commit_control_receipt(case.receipt.to_dict())
        assert not case.path.exists() and not case.path.with_name(case.path.name + '.lock').exists()


@pytest.mark.parametrize('mode', ['fake', 'missing'])
def test_missing_or_fake_agent_is_not_lease_only_authority(memory_control, mode):  # noqa: F811
    with _source_case(memory_control, mode) as case:
        with pytest.raises(local.LocalControlCoordinatorUnavailable):
            case.source.commit_control_receipt(case.receipt.to_dict())
        assert not case.path.exists()


def test_original_full_controller_cannot_downgrade_to_late_none(memory_control, monkeypatch):  # noqa: F811
    with _source_case(memory_control, 'full') as case, monkeypatch.context() as patcher:
        assert case.source.controller is case.lease.controller and case.source.controller is not None
        patcher.setattr(case.source, 'controller', None)
        patcher.setattr(case.lease, 'controller', None)
        with pytest.raises(local.LocalControlCoordinatorUnavailable):
            case.source.commit_control_receipt(case.receipt.to_dict())
        assert not case.path.exists()


def test_ready_source_does_not_enable_general_perform(memory_control):  # noqa: F811
    with _source_case(memory_control) as case:
        calls = []
        with pytest.raises(local.LocalControlCoordinatorUnavailable):
            case.source.perform(lambda: calls.append('effect'))
        assert calls == [] and not case.path.exists()


def test_retired_source_cannot_write_receipt(memory_control):  # noqa: F811
    with _source_case(memory_control) as case:
        source, receipt, path = case.source, case.receipt, case.path
    with pytest.raises((ValueError, local.LocalControlCoordinatorUnavailable)):
        source.commit_control_receipt(receipt.to_dict())
    assert not path.exists()


def test_fixed_leaf_rejects_wrong_path(memory_control, tmp_path):  # noqa: F811
    with _source_case(memory_control) as case:
        wrong = tmp_path / 'other.json'
        with case.lease._lifecycle_gate, pytest.raises(local.LocalControlCoordinatorUnavailable):
            local._write_original_control_receipt(case.source, wrong, case.receipt.to_dict())
        assert not wrong.exists() and not case.path.exists()


@pytest.mark.parametrize('commit_before_error', [False, True])
def test_io_error_keeps_original_object_and_actual_partial(memory_control, monkeypatch, commit_before_error):  # noqa: F811
    with _source_case(memory_control) as case:
        original = json_io.write_private_json_file_atomic_no_newline_unlocked
        physical = OSError(errno.EIO, 'synthetic physical failure')

        def fail(path, payload, **kwargs):
            if commit_before_error:
                original(path, payload, **kwargs)
            raise physical

        monkeypatch.setattr(json_io, 'write_private_json_file_atomic_no_newline_unlocked', fail)
        with pytest.raises(OSError) as observed:
            case.source.commit_control_receipt(case.receipt.to_dict())
        assert observed.value is physical
        assert case.path.exists() is commit_before_error
        if commit_before_error:
            assert ops.read_gateway_control_operation(memory_control.paths, case.receipt.operation_id).state == 'prepared'
        assert case.source._receipt_raw is None and case.source._receipt_state is None


def test_readback_drift_is_not_completion(memory_control, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.gateway_parts import multiprocess_control_forward as forward
    with _source_case(memory_control) as case:
        original = forward.read_control_operation_raw

        def changed(paths, operation, *, deadline):
            raw = original(paths, operation, deadline=deadline)
            return raw + b' ' if raw is not None else raw

        monkeypatch.setattr(forward, 'read_control_operation_raw', changed)
        with pytest.raises(local.LocalControlCoordinatorUnavailable):
            case.source.commit_control_receipt(case.receipt.to_dict())
        assert ops.read_gateway_control_operation(memory_control.paths, case.receipt.operation_id).state == 'prepared'
        assert case.source._receipt_raw is None and case.source._receipt_state is None


@pytest.mark.parametrize('previous', ['prepared', 'terminal_unknown'])
def test_ready_receipt_keeps_original_terminal_transition_graph(memory_control, previous):  # noqa: F811
    with _source_case(memory_control) as case:
        source, receipt = case.source, case.receipt
        source.commit_control_receipt(receipt.to_dict())
        if previous == 'terminal_unknown':
            source.commit_control_receipt(replace(receipt, state='executing').to_dict())
            source.commit_control_receipt(replace(receipt, state='terminal_unknown', error={'error_code': 'synthetic_unknown'}).to_dict())
        completed = replace(receipt, state='completed', result=ConversationControlResult('memory', True, '合成结果').to_dict())
        with pytest.raises(local.LocalControlCoordinatorUnavailable):
            source.commit_control_receipt(completed.to_dict())
        assert ops.read_gateway_control_operation(memory_control.paths, receipt.operation_id).state == previous


@pytest.mark.parametrize('changed', ['context', 'controller_install'])
def test_waiting_original_lease_rechecks_late_reference_without_publication(memory_control, monkeypatch, changed):  # noqa: F811
    created, proceed, checked, observed, cleanup = (threading.Event() for _ in range(5))
    state, errors = {}, []
    original = local._require_receipt_origin

    def track(source, receipt):
        result = original(source, receipt)
        if threading.current_thread() is thread:
            checked.set()
        return result

    def worker():
        try:
            with _source_case(memory_control) as case:
                state['case'] = case
                created.set()
                assert proceed.wait(5)
                try:
                    case.source.commit_control_receipt(case.receipt.to_dict())
                except local.LocalControlCoordinatorUnavailable as error:
                    state['refusal'] = error
                else:
                    raise AssertionError('late source unexpectedly published')
                observed.set()
                assert cleanup.wait(5)
        except BaseException as error:
            errors.append(error)
            observed.set()

    thread = threading.Thread(target=worker)
    monkeypatch.setattr(local, '_require_receipt_origin', track)
    thread.start()
    installed = None
    try:
        assert created.wait(5)
        case = state['case']
        with case.lease._lifecycle_gate:
            proceed.set()
            assert checked.wait(5)
            if changed == 'context':
                case.source.context = object()
            else:
                installed = takeover.prepare_coordinator_takeover(memory_control.paths, 2, case.lease.c_run_id)
                assert installed is case.lease.controller
        assert observed.wait(5) and not errors
        assert isinstance(state.get('refusal'), local.LocalControlCoordinatorUnavailable)
        assert not case.path.exists() and not case.path.with_name(case.path.name + '.lock').exists()
    finally:
        if created.is_set():
            case = state['case']
            case.source.context = case.source.paths
            case.lease.controller = None
        if installed is not None:
            installed.close()
        proceed.set()
        cleanup.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
