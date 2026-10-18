"""真实HTTP/原source固定preview端口；临时测试分派不代表发行memory入口ready。"""
import threading
from contextlib import contextmanager

import pytest

from agent_py_agent.tests.test_coordinator_agents_domain import submit
from agent_py_agent.tests.test_coordinator_control_forward import cold
from agent_py_agent.tests.test_coordinator_memory_candidates_source import install, ready


# LLM: 仅fixture设置阶段使用原CandidateService写真实候选；HTTP执行仍走真实G1/C和固定preview端口。
# 函数用途: 给每个普通事务测试创建相同原件，不造候选服务替身。
def seed(case):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope

    service = case.seeded.memory_candidates
    row = service.observe(CandidateObservation(candidate_type='project', content='本项目用UTC记录时间',
        subject_key='preview.transaction', scope=MemoryScope('personal', 'personal'), origin='reviewed',
        promotion_target='long_term'))
    return service.transition(row, 'pending_review', reviewer='alice')


# LLM: 固定原调用只传真实三引用，不注入writer/holder；test hook仅观察或制造普通锁/CAS故障。
# 函数用途: 将实际HTTP source接到本片服务/事务组件，nonce仍由原complete写入。
def install_domain(case, monkeypatch, *, hook=None):
    from agent_py_agent.agent.gateway_parts import memory_candidate_review as review
    from agent_py_agent.agent.gateway_parts.memory_candidate_control import memory_control_failure
    from agent_py_agent.agent.gateway_parts.memory_preview_transaction import (
        MemoryPreviewTransactionRejected,
        memory_preview_transaction,
    )

    def exercise(source, original):
        scope, _result, thread_id = ready(source)
        services = review.MemoryCandidateServices(case.seeded.memory_candidates,
            case.seeded.memory_promotion, source._management_memory_binding.home.owner_id)
        command = source.http_origin[3]
        if command.operation in {'list', 'show', 'preview'}:
            result = review.run_memory_candidate_control_from_services(services, source.paths, command, scope, thread_id)
        else:
            try:
                with memory_preview_transaction(source, services, thread_id) as transaction:
                    if hook:
                        hook(source, services, transaction)
                    result = review.run_memory_candidate_control_from_services(services, source.paths,
                        command, scope, thread_id, preview_transaction=transaction)
            except MemoryPreviewTransactionRejected as error:
                result = memory_control_failure(error.code)
        return result, thread_id

    return install(monkeypatch, exercise)


def preview(case, candidate):
    status, row = submit(case, '/memory candidates preview ' + candidate.candidate_id, 'preview-one')
    assert status == 200 and row['state'] == 'completed' and row['result']['ok'], row
    return row['result']['details']


def read(case, nonce):
    from agent_py_agent.agent.gateway_parts.memory_candidate_review import _read_preview_raw
    return _read_preview_raw(case.server.paths, nonce)


# LLM: 争用只由本测试原锁/Thread制造；finally显式归还，不能把超时或缺行当作已拿到锁。
# 函数用途: 在另一真实线程持原锁，核C只try不等待。
@contextmanager
def held(lock):
    entered, release = threading.Event(), threading.Event()
    errors = []

    def run():
        try:
            with lock():
                entered.set()
                assert release.wait(5)
        except BaseException as error:
            errors.append(error)
            entered.set()

    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(3) and not errors
    try:
        yield
    finally:
        release.set()
        thread.join(3)
        assert not thread.is_alive() and not errors


def test_cancel_and_receipt_never_lookup_candidate_or_extend_preview(cold, monkeypatch):  # noqa: F811
    candidate = seed(cold)
    observed = install_domain(cold, monkeypatch)
    details = preview(cold, candidate)
    monkeypatch.setattr(cold.seeded.memory_candidates, 'get', lambda *_: pytest.fail('cancel/receipt不得查候选'))
    status, cancelled = submit(cold, details['cancel_command'], 'cancel-one')
    assert status == 200 and cancelled['result']['details']['state'] == 'cancelled', (cancelled, observed)
    record, raw = read(cold, details['nonce'])
    assert record.state == 'completed' and record.result['details']['expires_at'] == details['expires_at']
    assert submit(cold, details['cancel_command'], 'cancel-one') == (status, cancelled)
    status, projected = submit(cold, details['receipt_command'], 'receipt-two')
    assert status == 200 and projected['result']['details']['state'] == 'cancelled'
    assert read(cold, details['nonce'])[1] == raw and cold.seeded.memory.all() == []


@pytest.mark.parametrize('actual_native', [False, True])
def test_consume_durable_before_review_failure_and_never_replays(cold, monkeypatch, actual_native):  # noqa: F811
    from agent_py_agent.agent.common.owner_write_errors import LocalControlCoordinatorUnavailable

    candidate = seed(cold)
    observed = install_domain(cold, monkeypatch)
    details = preview(cold, candidate)
    failure = LocalControlCoordinatorUnavailable('synthetic original native') if actual_native else OSError('synthetic ordinary review')
    calls = []

    def fail(*_args, **_kwargs):
        record, _raw = read(cold, details['nonce'])
        assert record.result['details']['state'] == 'consumed_unknown'
        calls.append(True)
        raise failure

    monkeypatch.setattr(cold.seeded.memory_promotion, 'review', fail)
    status, result = submit(cold, details['confirm_command'], 'confirm-failed')
    assert status == (503 if actual_native else 200), (result, observed)
    if actual_native:
        assert observed[-1] is failure and observed[-2]._native_source_failure is None
    else:
        assert not result['result']['ok'] and result['result']['error_code'] == 'MEMORY_CONTROL_RESULT_UNKNOWN'
    record, raw = read(cold, details['nonce'])
    assert record.state == 'completed' and record.result['details']['state'] == 'consumed_unknown'
    assert record.result['details']['consumed_request_id'] == 'confirm-failed'
    status, projected = submit(cold, details['confirm_command'], 'confirm-after-failed')
    assert status == 200 and not projected['result']['ok'] and calls == [True]
    assert read(cold, details['nonce'])[1] == raw
    assert cold.seeded.memory_candidates.get(candidate.candidate_id).status == 'pending_review'
    assert cold.seeded.memory.all() == []


def test_same_json_payload_with_changed_raw_is_not_consumed(cold, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.gateway_parts.control_operation_service import (
        gateway_control_operation_path,
    )

    candidate = seed(cold)
    changed = []

    def hook(source, services, transaction):
        path = gateway_control_operation_path(source.paths, transaction.nonce)
        raw = path.read_bytes() + b' '
        path.write_bytes(raw)
        changed.append(raw)

    observed = install_domain(cold, monkeypatch, hook=hook)
    details = preview(cold, candidate)
    status, result = submit(cold, details['cancel_command'], 'cancel-raw-changed')
    assert status == 503 and isinstance(observed[-1], ValueError), (result, observed)
    assert read(cold, details['nonce'])[1] == changed[0]
    assert read(cold, details['nonce'])[0].result['details']['state'] == 'preview'
    assert cold.seeded.memory_candidates.get(candidate.candidate_id).status == 'pending_review'


@pytest.mark.parametrize('kind', ['preview', 'json'])
def test_preview_and_json_lock_busy_do_not_consume_or_review(cold, monkeypatch, kind):  # noqa: F811
    from agent_py_agent.agent.common.json_io import locked_json_path
    from agent_py_agent.agent.gateway_parts.control_operation_service import (
        gateway_control_operation_path,
        gateway_control_operation_transition,
    )

    candidate = seed(cold)
    observed = install_domain(cold, monkeypatch)
    details = preview(cold, candidate)
    original, raw = read(cold, details['nonce'])
    lock = (lambda: gateway_control_operation_transition(cold.server.paths, details['nonce'])) if kind == 'preview' else (
        lambda: locked_json_path(gateway_control_operation_path(cold.server.paths, details['nonce'])))
    monkeypatch.setattr(cold.seeded.memory_promotion, 'review', lambda *_a, **_k: pytest.fail('忙时不能review'))
    with held(lock):
        status, result = submit(cold, details['confirm_command'], 'confirm-busy')
    if kind == 'preview':
        assert status == 200 and result['result']['error_code'] == 'MEMORY_PREVIEW_BUSY', (result, observed)
    else:
        assert status == 503 and isinstance(observed[-1], BlockingIOError), (result, observed)
    assert read(cold, details['nonce'])[1] == raw and original.result['details']['state'] == 'preview'
    assert cold.seeded.memory_candidates.get(candidate.candidate_id).status == 'pending_review'


@pytest.mark.parametrize('change', ['owner', 'thread', 'actor', 'nonce'])
def test_preview_complete_identity_mismatch_is_rejected(cold, monkeypatch, change):  # noqa: F811
    from agent_py_agent.agent.gateway_parts import control_operation_service as ops
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

    candidate = seed(cold)
    observed = install_domain(cold, monkeypatch)
    details = preview(cold, candidate)
    receipt, _raw = read(cold, details['nonce'])
    payload = receipt.to_dict()
    if change == 'owner':
        payload['owner_id'] = 'other-owner'
    elif change == 'thread':
        payload['conversation_thread_id'] = 'other-thread'
    else:
        payload['result']['details'][change] = 'other-original'
    write_json_file_atomic(ops.gateway_control_operation_path(cold.server.paths, details['nonce']), payload)
    path = ops.gateway_control_operation_path(cold.server.paths, details['nonce'])
    original = path.read_bytes()
    status, result = submit(cold, details['confirm_command'], 'confirm-identity')
    if change == 'owner':
        from agent_py_agent.agent.runtime_errors import DataCorruptionError
        assert status == 503 and isinstance(observed[-1], DataCorruptionError), (result, observed)
    else:
        assert status == 200 and result['result']['error_code'] == 'MEMORY_PREVIEW_BINDING_MISMATCH', (result, observed)
    assert path.read_bytes() == original and cold.seeded.memory.all() == []
