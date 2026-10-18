# LLM: 原guard/control/Root/P3/input均为临时实际组件；只用受控Python线程，不启动服务、模型、W/native或网络。
# 模块用途: 验ordinary新责任末发表与stop共门，旧terminal/release/ack/takeover共用叶仍原语义，不把occupied洗成已排空。
import copy
import threading
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_admission as admission
from agent_py_agent.agent.gateway_parts.io import (
    gateway_turn_transition,
    read_json_file,
    write_json_file_atomic,
)
from agent_py_agent.agent.gateway_parts.request_owner_binding import frozen_request_owner
from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
from agent_py_agent.tests.test_gateway_claim_transition_scope import c_actual as c_actual
from agent_py_agent.tests.test_gateway_cold_request_pump import pump, queue_request
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context


# LLM: actual pump构造并retain同一个原gate/runtime；仅把原prepared owner/session交原admit，不构造JSON权限。
# 函数用途: 驱动一条真实普通准入，便于把屏障放在原末叶之前或A1之后。
def admit(current, receipt):
    gate = current._admission_gate
    payload = receipt.prepared_request
    return gate.admit(current.paths.inbox / (receipt.request_id + '.json'),
        (frozen_request_owner(payload), admission.session_scope_for_request(payload)), AdmissionLimits(8, 8), snapshot=payload)


# LLM: 原观察者仅在A1后抛实际异常保留原半发表，不手写reservation或改route/指纹；后继补齐仍由同gate原codec。
# 函数用途: 取得同代P3已耐久但route尚未发布的真实组件窗口。
def half_published(current, receipt):
    gate = current._admission_gate
    error = RuntimeError('synthetic interruption after original A1')

    def observer(event, _payload):
        if event == 'reservation_persisted':
            raise error

    gate.observer = observer
    try:
        with pytest.raises(RuntimeError) as caught:
            admit(current, receipt)
        assert caught.value is error
    finally:
        gate.observer = None
    path = admission.reservation_file(current.paths, receipt.request_id)
    assert read_json_file(path)['state'] == 'occupied'
    assert 'route' not in read_json_file(current.paths.inbox / (receipt.request_id + '.json'))
    return path


def test_stop_after_preparation_wins_before_new_a1_a2(context, monkeypatch):
    receipt, current = queue_request(context), pump(context)
    gate = current._admission_gate
    original = gate._stamp_request

    def stamp(*args):
        value = original(*args)
        current.runtime.stop_producers()
        return value

    monkeypatch.setattr(gate, '_stamp_request', stamp)
    before = (current.paths.inbox / (receipt.request_id + '.json')).read_bytes()
    assert admit(current, receipt)['status'] == 'blocked'
    assert not admission.reservation_file(current.paths, receipt.request_id).exists()
    assert (current.paths.inbox / (receipt.request_id + '.json')).read_bytes() == before


def test_same_generation_ordinary_reuse_cannot_publish_after_stop(context):
    receipt, current = queue_request(context), pump(context)
    path = half_published(current, receipt)
    before = path.read_bytes(), (current.paths.inbox / (receipt.request_id + '.json')).read_bytes()
    current.runtime.stop_producers()
    assert admit(current, receipt)['status'] == 'blocked'
    assert (path.read_bytes(), (current.paths.inbox / (receipt.request_id + '.json')).read_bytes()) == before


def test_final_leaf_wins_and_stop_waits_until_both_a1_and_a2_complete(context):
    receipt, current = queue_request(context), pump(context)
    entered, release, stop_started, stopped = (threading.Event() for _ in range(4))
    results, errors = [], []

    def observer(event, _payload):
        if event == 'reservation_persisted':
            assert current.runtime._gate._is_owned()
            assert current._controller_origin._mutex._is_owned()
            assert current._lease_origin._lifecycle_gate._is_owned()
            entered.set()
            assert release.wait(3)

    def writer():
        try:
            results.append(admit(current, receipt))
        except BaseException as error:
            errors.append(error)

    def stop():
        stop_started.set()
        try:
            current.runtime.stop_producers()
            stopped.set()
        except BaseException as error:
            errors.append(error)

    current._admission_gate.observer = observer
    writing = threading.Thread(target=writer)
    stopping = threading.Thread(target=stop)
    writing.start()
    try:
        assert entered.wait(3)
        stopping.start()
        assert stop_started.wait(3) and not stopped.wait(0.05)
    finally:
        release.set()
        writing.join(3)
        if stopping.ident is not None:
            stopping.join(3)
    assert not writing.is_alive() and not stopping.is_alive() and errors == []
    assert stopped.is_set() and results[0]['status'] == 'admitted'
    assert 'route' in read_json_file(current.paths.inbox / (receipt.request_id + '.json'))
    other = queue_request(context, 'after-stop', 'bob')
    assert admit(current, other)['status'] == 'blocked'
    assert not admission.reservation_file(current.paths, other.request_id).exists()


def test_quota_preparation_remains_outside_root_and_stop_can_win(context, monkeypatch):
    from agent_py_agent.agent.gateway_parts import multiprocess_curator_accounting as quota

    receipt, current = queue_request(context), pump(context)
    original = quota.curator_quota_admission

    @contextmanager
    def preparing(*args):
        with original(*args) as rejection:
            assert not current.runtime._gate._is_owned()
            current.runtime.stop_producers()
            yield rejection

    monkeypatch.setattr(quota, 'curator_quota_admission', preparing)
    assert admit(current, receipt)['status'] == 'blocked'
    assert not admission.reservation_file(current.paths, receipt.request_id).exists()


@pytest.mark.parametrize('fault', ['missing', 'gate_copy', 'runtime_copy', 'tuple_copy', 'removed', 'agent', 'paths', 'count', 'generation'])
def test_formal_ordinary_missing_or_changed_original_binding_refuses(context, fault):
    receipt, current = queue_request(context), pump(context)
    gate = current._admission_gate
    if fault == 'missing':
        del gate._ordinary_admission_origin
    elif fault == 'gate_copy':
        current._admission_gate = copy.copy(gate)
    elif fault == 'runtime_copy':
        gate._ordinary_admission_origin = (copy.copy(current.runtime), gate._ordinary_admission_origin[1])
    elif fault == 'tuple_copy':
        gate._ordinary_admission_origin = (current.runtime, (*gate._ordinary_admission_origin[1],))
    elif fault == 'removed':
        current.runtime._admission_origins.clear()
    elif fault == 'agent':
        gate.agent = copy.copy(gate.agent)
    elif fault == 'paths':
        gate.paths = copy.copy(gate.paths)
    elif fault == 'count':
        gate.count += 1
    else:
        gate.c_run_id = 'wrong-generation'
    try:
        decision = admit(current, receipt)
    except ValueError:
        decision = {'status': 'blocked'}
    assert decision['status'] == 'blocked'
    assert not admission.reservation_file(current.paths, receipt.request_id).exists()
    assert 'route' not in read_json_file(current.paths.inbox / (receipt.request_id + '.json'))


def test_default_shared_reuse_leaf_is_not_changed_into_ordinary_closing_gate(context):
    receipt, current = queue_request(context), pump(context)
    half_published(current, receipt)
    gate = current._admission_gate
    path = current.paths.inbox / (receipt.request_id + '.json')
    record, payload = read_json_file(admission.reservation_file(current.paths, receipt.request_id)), read_json_file(path)
    ownership = frozen_request_owner(payload), admission.session_scope_for_request(payload)
    current.runtime.stop_producers()
    # 只核默认共用叶；真正takeover资格仍由原restamp_for_takeover caller核，不把此直接叶当新授权。
    with gateway_turn_transition(current.paths, receipt.request_id), admission._runtime_decision_scope(gate):
        result = gate._publish_reuse(path, payload, record, ownership)
    assert result['status'] == 'admitted' and 'route' in read_json_file(path)


def test_preclosing_occupied_without_terminal_remains_incomplete_and_unsealed(context):
    receipt, current = queue_request(context), pump(context)
    assert admit(current, receipt)['status'] == 'admitted'
    path = admission.reservation_file(current.paths, receipt.request_id)
    before = path.read_bytes()
    current.runtime.stop_producers()
    assert current._admission_gate.release_if_terminal(receipt.request_id) == {'released': False, 'reason': 'terminal_missing'}
    assert path.read_bytes() == before and read_json_file(path)['state'] == 'occupied'
    assert not all(result.drained for result in current.runtime.assess())
    assert current.runtime._generation_observation is None and current.runtime._closed_generation_ref is None


def test_closing_keeps_original_c_ack_and_canonical_terminal_release(c_actual):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
        coordinator_execution_scope,
    )
    from agent_py_agent.agent.gateway_parts.request_binding import GatewayActiveTurnTransition

    gate, claim, agent = c_actual
    runtime = gate._ordinary_admission_origin[0]
    runtime.stop_producers()
    with coordinator_execution_scope(gate, claim, agent):
        transition = GatewayActiveTurnTransition(claim.path, claim.request_id, claim.execution_attempt_id)
        assert transition('acknowledge', lambda: 'original-ack') == 'original-ack'
    payload = read_json_file(claim.path)
    # 原canonical terminal输入夹具；不声称实际模型完成，不凭线程返回或projection释放。
    terminal = {**payload, 'schema_version': 'gateway_terminal_request.v1', 'turn_phase': 'closed', 'status': 'cancelled',
                'terminal_response': {'id': claim.request_id, 'ok': False, 'status': 'cancelled'}}
    write_json_file_atomic(gate.paths.terminal / claim.path.name, terminal)
    assert gate.release_if_terminal(claim.request_id)['released'] is True
    assert read_json_file(admission.reservation_file(gate.paths, claim.request_id))['state'] == 'released'
