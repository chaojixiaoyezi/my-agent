# LLM: 真实临时队列/pump配合已完成Future注入；readiness仅替换状态读取及等待时钟，保原循环和心跳写入，不宣称线上故障恢复已验。
# 模块用途: 验证故障只报一次且保责任、就绪短暂不可读容忍、日志通知去重。
import hashlib
import json
import time
from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import ConversationControlResult
from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.agent.gateway_parts import multiprocess_control_worker as controls
from agent_py_agent.agent.gateway_parts import multiprocess_worker as worker
from agent_py_agent.agent.gateway_parts import multiprocess_worker_dispatch as dispatch
from agent_py_agent.agent.gateway_parts import request_worker
from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import WorkerBootstrapError
from agent_py_agent.cli import gateway_worker as entry
from agent_py_agent.tests.test_gateway_multiprocess_control_worker import prepared_case
from agent_py_agent.tests.test_gateway_multiprocess_worker_dispatch import (  # noqa: F401
    enqueue,
    setup,
)
from agent_py_agent.tests.test_native_transport_diagnostic import diagnostic_rows


# LLM: 错误Future由测试注入，后续请求仍走真实claim→provider边界→canonical；不得清掉未结claim或计为已结清。
# 函数用途: 验证普通请求失败只诊断一次且不占后续请求执行槽。
def test_request_future_error_contained_and_retained(setup, monkeypatch, capsys):  # noqa: F811
    pump = dispatch.WorkerRequestPump(setup.frozen, setup.pool, 1)
    failed = Future()
    failed.set_exception(RuntimeError('fixture-failure'))
    claim = SimpleNamespace(request_id='failed-request', execution_attempt_id='failed-attempt')
    pump._active[failed] = claim
    path = enqueue(setup, setup.request)
    monkeypatch.setattr(request_worker, '_process_claimed_gateway_request',
                        lambda context: {'id': context.request_id, 'ok': True, 'status': 'done'})
    try:
        for _ in range(3):
            pump._reap()
        assert pump._active == {failed: claim} and pump.inflight == 1
        rows = diagnostic_rows(capsys)
        assert len(rows) == 1 and rows[0]['stage'] == 'worker_future_result'
        assert rows[0]['request']['sha256'] == hashlib.sha256(claim.request_id.encode()).hexdigest()
        assert rows[0]['attempt']['sha256'] == hashlib.sha256(claim.execution_attempt_id.encode()).hexdigest()
        assert pump.poll() == 1
        pump._executor.shutdown(wait=True)
        pump._reap()
        assert (setup.paths.terminal / path.name).exists()
        assert pump._active == {failed: claim}
        assert pump.drain_snapshot()[2] == ((failed, claim),)
        assert diagnostic_rows(capsys) == []
    finally:
        pump.close()
    assert pump._active == {failed: claim}


# LLM: 普通控制Future失败不经过私卡退休分支；保同一receipt/future，P6仍看到未结责任。
# 函数用途: 验证控制故障隔离及单次安全诊断。
def test_control_future_error_contained(tmp_path, monkeypatch, capsys):
    _paths, receipt, _scope, frozen, pool, _owners = prepared_case(tmp_path, monkeypatch)
    pump = controls.WorkerControlPump(frozen, pool)
    failed = Future()
    failed.set_exception(RuntimeError('fixture-control-failure'))
    pump._active[failed] = receipt
    try:
        for _ in range(3):
            pump._reap()
        assert pump._active == {failed: receipt} and pump.inflight == 1
        assert pump.drain_snapshot()[2] == ((failed, receipt),)
        rows = diagnostic_rows(capsys)
        assert len(rows) == 1 and rows[0]['stage'] == 'worker_future_result'
        assert rows[0]['request']['sha256'] == hashlib.sha256(receipt.operation_id.encode()).hexdigest()
        assert rows[0]['attempt'] is None
    finally:
        pump.close()
    assert diagnostic_rows(capsys) == [] and pump._active == {failed: receipt}


# LLM: 四个已失败Future只属测试注入责任；真实prepared仍走原authority/短锁，已返回责任不冒占普通执行槽。
# 函数用途: 防止控制故障保留后让健康控制永久饥饿。
def test_control_capacity_ignores_done_faulted_futures(tmp_path, monkeypatch):
    _paths, receipt, _scope, frozen, pool, _owners = prepared_case(tmp_path, monkeypatch)
    pump = controls.WorkerControlPump(frozen, pool)
    retained = {}
    for index in range(4):
        future = Future()
        future.set_exception(RuntimeError('fixture-control-failure'))
        retained[future] = replace(receipt, operation_id=f'faulted-{index}', state='claimed')
    pump._active.update(retained)
    effects = []

    # LLM: 只观察原领域执行边界，不替换claim或授权。
    # 函数用途: 记录健康控制已获得原执行槽。
    def execute(_agent, _paths, command, _scope):
        effects.append(command.kind)
        return ConversationControlResult(command.kind, True, 'fixture-completed')

    monkeypatch.setattr(ops, 'execute_gateway_conversation_control', execute)
    try:
        assert pump.poll() == 1
        pump._executor.shutdown(wait=True)
        pump._reap()
        assert effects == [receipt.command_kind] and pump._active == retained
    finally:
        pump.close()


# LLM: 撤权错误不归类普通故障，不用缓存或容忍窗口掩盖，认领入口必须仍传播同一对象。
# 函数用途: 验证control_worker_authority失败仍立即拒绝poll。
def test_bootstrap_error_still_propagates(tmp_path, monkeypatch):
    _paths, _receipt, _scope, frozen, pool, _owners = prepared_case(tmp_path, monkeypatch)
    pump = controls.WorkerControlPump(frozen, pool)
    error = WorkerBootstrapError('control_registry_generation', 'registry')

    # LLM: 仅注入原authority边界故障，不伪造有效执行权。
    # 函数用途: 在真实poll到达原授权检查时拒绝。
    def rejected(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(controls, 'control_worker_authority', rejected)
    try:
        with pytest.raises(WorkerBootstrapError) as caught:
            pump.poll()
        assert caught.value is error
    finally:
        pump.close()


# LLM: 在已完成普通Future中撤权也不被吞；私卡和审批退休另由原回归覆盖。
# 函数用途: 验证两种泵的future撤权传播合同。
@pytest.mark.parametrize('kind', ['request', 'control'])
def test_future_bootstrap_error_still_propagates(tmp_path, monkeypatch, kind):
    _paths, receipt, _scope, frozen, pool, _owners = prepared_case(tmp_path, monkeypatch)
    pump = (dispatch.WorkerRequestPump(frozen, pool, 1) if kind == 'request'
            else controls.WorkerControlPump(frozen, pool))
    future = Future()
    error = WorkerBootstrapError('control_registry_generation', 'registry')
    future.set_exception(error)
    pump._active[future] = (SimpleNamespace(request_id='revoked', execution_attempt_id='attempt')
                            if kind == 'request' else receipt)
    try:
        with pytest.raises(WorkerBootstrapError) as caught:
            pump._reap()
        assert caught.value is error and future in pump._active
    finally:
        with pytest.raises(WorkerBootstrapError):
            pump.close()


# LLM: 只把等待改成确定性时钟、状态读取改成给定序列；保原_worker_execution_loop/_refresh/_publish与真实临时心跳写入。
# 函数用途: 观察每秒循环和每两秒心跳间是否错误接单或提前退出。
def loop_case(tmp_path, monkeypatch, beats):
    paths, _receipt, _scope, frozen, _pool, _owners = prepared_case(tmp_path, monkeypatch)
    launch = replace(frozen.launch, instance_id='')
    clock, polls, notices = [0.0], [], []
    sequence = iter(beats)

    # LLM: 单测时钟只作用于被测模块，不修改全局time或真实等待。
    # 类用途: 在有限轮数内推进原worker循环。
    class Stop:
        # LLM: 确定性推进一秒；超过给定状态所需轮数就失败，不能靠无限循环通过。
        # 函数用途: 代替实际等待并暴露循环未终止。
        def wait(self, delay):
            clock[0] += delay
            assert clock[0] < 30, 'worker loop did not finish'
            return False

    monkeypatch.setattr(worker, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(worker, 'prepare_worker_readiness', lambda _: next(sequence))
    pump = SimpleNamespace(inflight=0, claiming_stopped=False, poll=lambda: polls.append(clock[0]))
    outcome = worker._worker_execution_loop(launch, Stop(), notices.append, pump)
    return outcome, notices, polls, read_json_file(paths.root / 'workers' / f'w{launch.partition.index}.json')


# LLM: 连续四次不可读只有第四次退出；每次真实文件均不就绪，所有中间非心跳轮次也不得接单。
# 函数用途: 覆盖容忍上限与保守心跳投影。
def test_unreadable_state_tolerated_then_exit(tmp_path, monkeypatch):
    beats = [{'ready': False, 'reason': 'coordinator_state_unreadable', 'error_code': 'WORKER_NOT_READY'}
             for _ in range(4)]
    outcome, notices, polls, last = loop_case(tmp_path, monkeypatch, beats)
    assert outcome == 'not_ready' and len(notices) == 4 and polls == []
    assert all(row['ready'] is False and row['loop_health'] == 'coordinator_state_unreadable' for row in notices)
    assert last == notices[-1]


# LLM: 恢复到ready才重开poll并重置计数；两轮不可读不会让整个W永久退出，也不会在心跳间隙假恢复。
# 函数用途: 验证短暂读取故障恢复后的接单与容忍计数重置。
def test_ready_beat_resets_unreadable_counter(tmp_path, monkeypatch):
    unreadable = {'ready': False, 'reason': 'coordinator_state_unreadable'}
    beats = [dict(unreadable), dict(unreadable), {'ready': True}, dict(unreadable), dict(unreadable),
             dict(unreadable), dict(unreadable)]
    outcome, notices, polls, _last = loop_case(tmp_path, monkeypatch, beats)
    assert outcome == 'not_ready' and len(notices) == 7
    assert polls == [5.0, 6.0]


# LLM: 容忍白名单仅一个reason；所有身份/父/端口拒绝都在首个心跳退出，不尝试任何poll。
# 函数用途: 防止状态不可读容忍扩大成撤权后继续接单。
@pytest.mark.parametrize('reason', ['coordinator_generation', 'coordinator_parent', 'coordinator_identity',
                                    'coordinator_port', 'gateway_port_readback'])
def test_other_not_ready_reasons_exit_immediately(tmp_path, monkeypatch, reason):
    outcome, notices, polls, last = loop_case(tmp_path, monkeypatch, [{'ready': False, 'reason': reason}])
    assert outcome == 'not_ready' and len(notices) == 1 and polls == []
    assert last['readiness']['reason'] == reason and last['ready'] is False


# LLM: 日志只看指定四元组；不减少原心跳文件写入，也不把inflight变化当健康切换。
# 函数用途: 验证十条同态载荷只打印首条，状态变化仍立即打印。
def test_heartbeat_notice_prints_only_on_change(capsys):
    notice = entry._HeartbeatNotice()
    payload = {'ready': True, 'loop_health': 'healthy', 'reason': '', 'error_code': ''}
    for index in range(10):
        notice({**payload, 'inflight': index})
    assert len(capsys.readouterr().out.splitlines()) == 1
    notice({**payload, 'ready': False})
    assert json.loads(capsys.readouterr().out)['ready'] is False
    for field, value in [('loop_health', 'not_ready'), ('reason', 'fixture-reason'), ('error_code', 'WORKER_NOT_READY')]:
        payload = {**payload, field: value}
        notice(payload)
        assert len(capsys.readouterr().out.splitlines()) == 1


# LLM: 使用原_publish_heartbeat写真实临时文件，通知类只抑制stdout；第二个实例不继承第一次的去重状态。
# 函数用途: 保证降噪没有降频心跳落盘或压掉新worker首条。
def test_heartbeat_file_still_updates_each_publish(tmp_path, monkeypatch, capsys):
    paths, _receipt, _scope, frozen, _pool, _owners = prepared_case(tmp_path, monkeypatch)
    notice = entry._HeartbeatNotice()
    for index in range(10):
        worker._publish_heartbeat(frozen.launch, {'ready': True, 'inflight': index}, notice)
        assert read_json_file(paths.root / 'workers' / f'w{frozen.launch.partition.index}.json')['inflight'] == index
    assert len(capsys.readouterr().out.splitlines()) == 1
    worker._publish_heartbeat(frozen.launch, {'ready': True}, entry._HeartbeatNotice())
    assert len(capsys.readouterr().out.splitlines()) == 1
