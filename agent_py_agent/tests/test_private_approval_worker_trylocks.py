"""原W控制Future只保留同receipt责任，持锁不等、不重投决定输入。"""
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import replace

import pytest

from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts import private_approval_control as private
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_ready import (
    registered_worker_heartbeat,
)
from agent_py_agent.agent.gateway_parts.multiprocess_control_worker import WorkerControlPump
from agent_py_agent.agent.gateway_parts.multiprocess_global_write import GlobalWriteError
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
from agent_py_agent.agent.user_space.owner_resolver import owner_identity_from_config
from agent_py_agent.tests.test_coordinator_approval_decision import seed, waiting_case


# LLM: 原registry/heartbeat/真实ownerAgent/pin来自生产，原ThreadPool保持；只暴露同一个泵供实际持锁观察，不制造执行源。
# 函数用途: 为临时C受理后的原W控制准备可清理的真实组件资源。
@contextmanager
def original_pump(case, binding, monkeypatch):
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    agent, _peer, _credential, _body, server, _runtime = case
    launch = replace(binding.snapshot.bootstrap.launch, parent_pid=os.getppid(), instance_id='trylock-control-instance')
    heartbeat = registered_worker_heartbeat(launch, launch.instance_id, {'ready': True, 'gateway_ports': [43210]}, time.time())
    registry = {'schema_version': 'multiprocess_worker_registry.v1', 'c_run_id': launch.partition.c_run_id,
        'worker_count': 2, 'process_identity': heartbeat['process_identity'], 'http_port': 43210,
        'workers': [{'worker': launch.partition.index, 'instance_id': launch.instance_id,
            'process_identity': heartbeat['process_identity'], 'executor_ledger_ref': f'workers/executors/w{launch.partition.index}'}]}
    write_json_file_atomic(server.paths.root / 'workers/registry.json', registry)
    write_json_file_atomic(server.paths.root / f'workers/w{launch.partition.index}.json', heartbeat)
    owner = owner_identity_from_config(agent.config)
    pool = OwnerScopedAgentPool(agent.config, agent.root, workspace_roots=tuple(agent.workspace_roots))
    def build_original(_config, _root, selected, _roots):
        assert selected == owner
        return agent
    pool._builder = build_original
    with monkeypatch.context() as scoped:
        scoped.setattr(mode, '_WORKER_PARTITION', launch.partition)
        pump = WorkerControlPump(freeze_worker_bootstrap(launch), pool)
        try:
            yield pump
        finally:
            pump.close()


# LLM: 原Thread执行被测方法，主线程释放准确本次持锁后必join；不改权限、receipt或线程注册表。
# 函数用途: 验证锁持有期间方法已经退出，避免只在释放锁后测一个虚假的短耗时。
def start_observation(operation):
    done, values, errors = threading.Event(), [], []
    def observe():
        try:
            values.append(operation())
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()
    thread = threading.Thread(target=observe, name='approval-lock-observation')
    thread.start()
    return thread, done, values, errors


def test_private_claim_json_lock_busy_preserves_prepared_without_future_or_input(tmp_path, monkeypatch):
    with waiting_case(tmp_path, monkeypatch) as (case, binding, handle, body):
        body['decision'] = 'approved'
        queued = seed.post(case[4], case[2], body)[1]
        path = ops.gateway_control_operation_path(case[4].paths, queued['operation_id'])
        before, permission = path.read_bytes(), handle.path.read_bytes()
        with original_pump(case, binding, monkeypatch) as pump:
            with locked_json_path(path):
                thread, done, values, errors = start_observation(pump.poll)
                finished = done.wait(.5)
            thread.join(5)
            assert not thread.is_alive()
            assert finished, 'W observation/claim waited for the original JSON lock'
            assert values == [0] and not errors
            assert path.read_bytes() == before and not pump.inflight
            assert handle.path.read_bytes() == permission
            assert pump.poll() == 1
        assert json.loads(handle.path.read_text())['decision_input']['operation_id'] == queued['operation_id']


def test_private_operation_busy_retains_same_failed_future_until_unknown_is_persisted(tmp_path, monkeypatch):
    with waiting_case(tmp_path, monkeypatch) as (case, binding, handle, body):
        body['decision'] = 'approved'
        queued = seed.post(case[4], case[2], body)[1]
        entered, proceed = threading.Event(), threading.Event()
        original = WorkerControlPump._execute
        def begin(pump, receipt):
            entered.set()
            assert proceed.wait(5)
            return original(pump, receipt)
        monkeypatch.setattr(WorkerControlPump, '_execute', begin)
        with original_pump(case, binding, monkeypatch) as pump:
            assert pump.poll() == 1 and entered.wait(5)
            [future] = pump._active
            permission = handle.path.read_bytes()
            with ops.gateway_control_operation_transition(pump.paths, queued['operation_id']):
                proceed.set()
                deadline = time.monotonic() + .5
                while not future.done() and time.monotonic() < deadline:
                    time.sleep(.005)
                finished = future.done()
                if finished:
                    before = ops.read_gateway_control_operation(pump.paths, queued['operation_id'], read_only=True)
                    start = time.monotonic()
                    assert pump.poll() == 0
                    assert time.monotonic() - start < .5
                    assert pump._active.get(future) is not None
                    stop_slot = pump._stop_executor.submit(lambda: 'original-independent-stop-slot')
                    assert stop_slot.result(timeout=.5) == 'original-independent-stop-slot'
                    assert ops.read_gateway_control_operation(pump.paths, queued['operation_id'], read_only=True) == before
            if not finished:
                future.result(timeout=5)
            assert finished, 'private Future waited for operation lock instead of preserving responsibility'
            error = future.exception()
            assert isinstance(error, GlobalWriteError)
            with pytest.raises(GlobalWriteError) as caught:
                pump.poll()
            assert caught.value is error and future not in pump._active
            receipt = ops.read_gateway_control_operation(pump.paths, queued['operation_id'], read_only=True)
            assert receipt.state == 'terminal_unknown' and receipt.result == {}
            assert handle.path.read_bytes() == permission and 'decision_input' not in json.loads(permission)


@pytest.mark.parametrize('decided_first', [False, True])
def test_partial_input_keeps_original_future_during_busy_settlement_and_never_reapplies(tmp_path, monkeypatch, decided_first):
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )
    with waiting_case(tmp_path, monkeypatch) as (case, binding, handle, body):
        body['decision'] = 'approved'
        queued = seed.post(case[4], case[2], body)[1]
        input_written, proceed, calls = threading.Event(), threading.Event(), []
        original = private._submit_approval_input
        error = RequestExecutionOutcomeUncertainError(json.loads(binding.claim.path.read_text()), 'approval_lock_settlement')
        def partial(agent, receipt, **kwargs):
            calls.append(receipt.operation_id)
            original(agent, receipt, **kwargs)
            input_written.set()
            assert proceed.wait(5)
            raise error
        monkeypatch.setattr(private, '_submit_approval_input', partial)
        with original_pump(case, binding, monkeypatch) as pump:
            assert pump.poll() == 1 and input_written.wait(5)
            [future] = pump._active
            path = ops.gateway_control_operation_path(pump.paths, queued['operation_id'])
            with locked_json_path(path):
                proceed.set()
                deadline = time.monotonic() + .5
                while not future.done() and time.monotonic() < deadline:
                    time.sleep(.005)
                finished = future.done()
                if finished:
                    assert pump.poll() == 0 and pump._active.get(future) is not None
            if not finished:
                with pytest.raises(RequestExecutionOutcomeUncertainError):
                    future.result(timeout=5)
            assert finished, 'failure settlement waited on original JSON lock'
            assert future.exception() is error
            if decided_first:
                assert handle.decision_input().decision == 'approved'
            with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
                pump.poll()
            assert caught.value is error and future not in pump._active
            receipt = ops.read_gateway_control_operation(pump.paths, queued['operation_id'], read_only=True)
            if decided_first:
                assert receipt.state == 'completed' and receipt.result['details']['decision'] == 'approved'
            else:
                assert receipt.state == 'terminal_unknown'
                assert handle.decision_input().decision == 'unavailable'
                assert pump.poll() == 0
            assert calls == [queued['operation_id']]
            row = json.loads(handle.path.read_text())
            assert row['decision_input'] == {'operation_id': receipt.operation_id, 'input_digest': receipt.input_digest}


def test_all_kind_observation_uses_original_readonly_codec_without_json_lock(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_gateway_multiprocess_control_worker import prepared_case
    paths, receipt, _scope, frozen, pool, _owners = prepared_case(tmp_path, monkeypatch, '/stop')
    path = ops.gateway_control_operation_path(paths, receipt.operation_id)
    before = path.read_bytes()
    pump = WorkerControlPump(frozen, pool)
    try:
        with locked_json_path(path):
            thread, done, values, errors = start_observation(lambda: pump._read_receipt(path))
            finished = done.wait(.5)
        thread.join(5)
        assert not thread.is_alive()
        assert finished, 'ordinary observation waited on JSON lock'
        assert not errors and values == [receipt]
        assert path.read_bytes() == before
    finally:
        pump.close()


def test_private_permission_lock_busy_is_unknown_without_input_or_blocking_future(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.agent_tool_approval import _transition_path
    with waiting_case(tmp_path, monkeypatch) as (case, binding, handle, body):
        body['decision'] = 'approved'
        queued = seed.post(case[4], case[2], body)[1]
        before = handle.path.read_bytes()
        with original_pump(case, binding, monkeypatch) as pump:
            with locked_json_path(_transition_path(handle.path)):
                assert pump.poll() == 1
                [future] = pump._active
                deadline = time.monotonic() + .5
                while not future.done() and time.monotonic() < deadline:
                    time.sleep(.005)
                assert future.done(), 'private Future waited for permission transition'
                with pytest.raises(GlobalWriteError) as caught:
                    pump.poll()
                assert caught.value is future.exception() and not pump.inflight
                receipt = ops.read_gateway_control_operation(pump.paths, queued['operation_id'], read_only=True)
                assert receipt.state == 'terminal_unknown' and receipt.result == {}
                assert handle.path.read_bytes() == before


def test_close_does_not_claim_settled_when_original_future_unknown_is_still_busy(tmp_path, monkeypatch):
    with waiting_case(tmp_path, monkeypatch) as (case, binding, _handle, body):
        body['decision'] = 'approved'
        queued = seed.post(case[4], case[2], body)[1]
        entered, proceed = threading.Event(), threading.Event()
        original = WorkerControlPump._execute
        def begin(pump, receipt):
            entered.set()
            assert proceed.wait(5)
            return original(pump, receipt)
        monkeypatch.setattr(WorkerControlPump, '_execute', begin)
        later = []
        try:
            with original_pump(case, binding, monkeypatch) as pump:
                assert pump.poll() == 1 and entered.wait(5)
                [future] = pump._active
                with ops.gateway_control_operation_transition(pump.paths, queued['operation_id']):
                    proceed.set()
                    deadline = time.monotonic() + .5
                    while not future.done() and time.monotonic() < deadline:
                        time.sleep(.005)
                    assert future.done()
                    with pytest.raises(GlobalWriteError) as caught:
                        pump.close()
                    assert caught.value is future.exception()
                    assert pump._active.get(future) is not None
                    assert ops.read_gateway_control_operation(pump.paths, queued['operation_id'], read_only=True).state == 'claimed'
        except GlobalWriteError as error:
            later.append(error)
        assert later == [future.exception()] and not pump.inflight
        assert ops.read_gateway_control_operation(pump.paths, queued['operation_id'], read_only=True).state == 'terminal_unknown'
