"""原append唯一账与全部必要锁的资源合同；只合法占用本测试原锁，不运行模型或native。"""
import threading
import time

import pytest

from agent_py_agent.agent.backends.request_scope import provider_request_budget
from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
    gateway_short_transaction,
    gateway_transaction_writer,
)
from agent_py_agent.agent.conversation.input_resource_retry import run_input_safe_point
from agent_py_agent.agent.gateway_parts.input_guidance_payload import (
    input_guidance_payload,
    original_input_guidance,
)
from agent_py_agent.agent.gateway_parts.io import (
    _locked_json_path,
    locked_file_transition,
    try_gateway_turn_transition,
    write_json_file_atomic,
)
from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
from agent_py_agent.tests.test_gateway_active_input_publication import held_lock
from agent_py_agent.tests.test_gateway_active_input_transport import (
    foreground,
    inputs,
    params_for,
    send,
)


# LLM: 用原source/T/末writer生成真实规范guidance；不修改存储行/伪造授权或ACK，输入receipt尚未binding是原追加中间态。
# 函数用途: 准备后续读取锁的合法既有账和投影，模型从未被调用。
def seed_original_guidance(agent, receipt):
    with gateway_short_transaction('input.transport.append'), gateway_transaction_writer('input.transport.guidance'):
        return agent.conversation_store.guidance.append_once(input_guidance_payload(receipt), dedupe_key=receipt.guidance_dedupe_key)


@pytest.mark.parametrize('lock_name', ['dedupe', 'canonical_read', 'canonical_write', 'delivered', 'turn_read', 'turn_write', 'input_read', 'input_write', 'queue'])
def test_all_original_append_locks_exit_gateway_gates_before_wait(tmp_path, monkeypatch, lock_name):
    with foreground(tmp_path, monkeypatch) as (agent, backend, gate, _c, runtime, server, claim, thread, _lane):
        code, incoming = send(server, message='guidance-lock-'+lock_name)
        assert code == 202
        receipt = inputs.read_gateway_input_receipt(gate.paths, incoming['request_id'])
        storage = agent.conversation_store.storage
        canonical = storage.guidance_dedupe_path(receipt.guidance_dedupe_key)
        if lock_name in {'canonical_read', 'delivered', 'turn_read', 'input_read'}:
            seed_original_guidance(agent, receipt)
        if lock_name == 'delivered':
            with gateway_short_transaction('input.transport.append'), gateway_transaction_writer('input.transport.guidance'):
                write_json_file_atomic(storage.guidance_delivered_path, {})
        paths = {
            'dedupe': canonical.with_name('.'+canonical.name+'.transition'),
            'canonical_read': canonical, 'canonical_write': canonical,
            'delivered': storage.guidance_delivered_path,
            'turn_read': storage.guidance_turn_index_path(claim.request_id, receipt.guidance_dedupe_key),
            'turn_write': storage.guidance_turn_index_path(claim.request_id, receipt.guidance_dedupe_key),
            'input_read': storage.guidance_input_index_path(receipt.request_id),
            'input_write': storage.guidance_input_index_path(receipt.request_id),
            'queue': storage.guidance_path('request', claim.request_id),
        }
        path = paths[lock_name]
        lock = locked_file_transition(path) if lock_name == 'dedupe' else (
            json_io.locked_json_path(path) if lock_name in {'canonical_write', 'turn_write', 'input_write', 'queue'} else _locked_json_path(path))
        input_path = inputs.gateway_input_receipt_path(gate.paths, receipt.request_id)
        original, p3 = input_path.read_bytes(), reservation_file(gate.paths, claim.request_id).read_bytes()
        params = params_for(agent, claim, thread)
        with held_lock(lock) as release:
            timer = threading.Timer(0.30, release.set)
            timer.start()
            try:
                with provider_request_budget(0.06), pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
                    run_input_safe_point(params.pending_turn_input_source, agent, 'drain', params)
                assert caught.value.phase == 'input.transport.retry_admission'
                assert not release.is_set(), '原IO不能在Gateway门内等到合法持锁者释放'
                # 原文件锁仍被占有时，从独立普通Thread只观察原T/controller/lease/Root是否已释放。
                observed, failures = [], []

                def observe_original_gates():
                    owned = []
                    try:
                        with try_gateway_turn_transition(gate.paths, claim.request_id) as acquired:
                            assert acquired
                            try:
                                controller = gate._runtime_takeover
                                for mutex in (controller._mutex, controller._lease._lifecycle_gate, runtime._gate):
                                    assert mutex.acquire(timeout=0.2)
                                    owned.append(mutex)
                                observed.append(True)
                            finally:
                                for mutex in reversed(owned):
                                    mutex.release()
                    except BaseException as error:
                        failures.append(error)

                observer = threading.Thread(target=observe_original_gates)
                observer.start()
                observer.join(2)
                assert not observer.is_alive() and not failures, [repr(error) for error in failures]
                assert observed == [True]

            finally:
                release.set()
                timer.join(2)
        assert input_path.read_bytes() == original and reservation_file(gate.paths, claim.request_id).read_bytes() == p3
        assert not (gate.paths.terminal / (claim.request_id+'.json')).exists() and backend.calls == 0
        # 同一规范guidance若已部分提交，解锁后只修原投影/绑定，不重造identity或执行模型。
        before = original_input_guidance(agent.conversation_store, receipt)
        run_input_safe_point(params.pending_turn_input_source, agent, 'drain', params)
        after = original_input_guidance(agent.conversation_store, receipt)
        assert after is not None and (before is None or after.entry.guidance_id == before.entry.guidance_id)
        assert inputs.read_gateway_input_receipt(gate.paths, receipt.request_id).state == 'active_pending' and backend.calls == 0


def test_serialization_expiry_cannot_commit_guidance_after_original_budget(tmp_path, monkeypatch):
    with foreground(tmp_path, monkeypatch) as (agent, backend, gate, _c, _r, server, claim, thread, _lane):
        code, incoming = send(server, message='guidance-serialization-budget')
        assert code == 202
        receipt = inputs.read_gateway_input_receipt(gate.paths, incoming['request_id'])
        serialize = json_io.serialize_private_json_no_newline
        delayed = []

        def slow(payload, **options):
            result = serialize(payload, **options)
            if payload.get('schema_version') == 'conversation_guidance_once.v4' and not delayed:
                delayed.append(True)
                time.sleep(0.10)
            return result

        monkeypatch.setattr(json_io, 'serialize_private_json_no_newline', slow)
        params = params_for(agent, claim, thread)
        with provider_request_budget(0.04), pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
            run_input_safe_point(params.pending_turn_input_source, agent, 'drain', params)
        assert caught.value.phase == 'input.transport.retry_admission' and delayed
        assert original_input_guidance(agent.conversation_store, receipt) is None
        assert inputs.read_gateway_input_receipt(gate.paths, receipt.request_id).state == 'pending' and backend.calls == 0


def test_bounded_replace_permission_error_is_same_object_without_internal_sleep(tmp_path, monkeypatch):
    from pathlib import Path
    with foreground(tmp_path, monkeypatch) as (agent, backend, gate, _c, _r, server, claim, thread, _lane):
        code, incoming = send(server, message='guidance-projection-permission-error')
        assert code == 202
        receipt = inputs.read_gateway_input_receipt(gate.paths, incoming['request_id'])
        target = agent.conversation_store.storage.guidance_input_index_path(receipt.request_id)
        original_replace = Path.replace
        error = PermissionError('synthetic own temporary projection failure')
        attempts = []

        def replace(path, destination):
            if destination == target:
                attempts.append(path)
                raise error
            return original_replace(path, destination)

        monkeypatch.setattr(Path, 'replace', replace)
        params = params_for(agent, claim, thread)
        with provider_request_budget(1), pytest.raises(PermissionError) as caught:
            run_input_safe_point(params.pending_turn_input_source, agent, 'drain', params)
        assert caught.value is error and len(attempts) == 1
        partial = original_input_guidance(agent.conversation_store, receipt)
        assert partial is not None and partial.status == 'pending'
        assert inputs.read_gateway_input_receipt(gate.paths, receipt.request_id).state == 'pending'
        monkeypatch.setattr(Path, 'replace', original_replace)
        run_input_safe_point(params.pending_turn_input_source, agent, 'drain', params)
        assert original_input_guidance(agent.conversation_store, receipt).entry.guidance_id == partial.entry.guidance_id
        assert backend.calls == 0
