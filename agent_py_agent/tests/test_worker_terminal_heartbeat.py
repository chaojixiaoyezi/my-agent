"""原run_worker、真实线程池关闭和末心跳；只控制父身份/信号/ready，不启动服务或模型。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_native_runtime as native
from agent_py_agent.agent.gateway_parts import multiprocess_worker as worker
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
from agent_py_agent.agent.gateway_parts.multiprocess_worker_dispatch import WorkerRequestPump
from agent_py_agent.tests.test_gateway_multiprocess_worker_dispatch import setup

_FIXTURES = (setup,)


# LLM: 复用原WorkerRequestPump/实际close和原执行loop，不把手写outcome或P6许可作为刺激；无native/P4本测试不签物理退出。
# 函数用途: 以原control或真实ready拒绝触发三个正常返回路径，记录实际池关闭和原末心跳时序。
def prepare(case, monkeypatch, outcome, *, close_error=None, fail_at=1, native_error=None, native_mode=False):
    launch = case.frozen.launch
    if native_mode:
        launch = replace(launch, instance_id="controlled-native")
    frozen = freeze_worker_bootstrap(launch)
    pump = WorkerRequestPump(frozen, case.pool, 1)
    notices, effects = [], []
    actual_close = pump.close
    readiness_calls = []
    close_calls = []
    actual_stop = pump._drain_resource.stop

    def stop_resource():
        close_calls.append('stop')
        if close_error is not None and len(close_calls) == fail_at:
            raise close_error
        actual_stop()

    monkeypatch.setattr(pump._drain_resource, 'stop', stop_resource)

    def readiness(_launch):
        readiness_calls.append(_launch)
        return {'ready': not (outcome == 'not_ready' and len(readiness_calls) > 1)}

    def close():
        effects.append('close_entered')
        actual_close()
        effects.append('original_close_returned')
        effects.append('close_returned')

    def notice(payload):
        if payload.get('loop_health') == 'stopped':
            effects.append('publish_stopped')
        notices.append(dict(payload))

    monkeypatch.setattr(pump, 'close', close)
    if native_mode:
        def close_native():
            effects.append('native_close_entered')
            if native_error is not None:
                raise native_error
            effects.append('native_closed')
        monkeypatch.setattr(native, 'connect_worker_native', lambda _: SimpleNamespace(close=close_native))
        monkeypatch.setattr(worker, 'wait_worker_registration', lambda *_: None)
        monkeypatch.setattr(worker, 'require_worker_registration', lambda *_, **__: None)
    monkeypatch.setattr(worker, 'create_request_pump', lambda *_, **__: pump)
    monkeypatch.setattr(worker, '_install_stop_handlers', lambda _: {})
    monkeypatch.setattr(worker, '_arm_parent_exit', lambda _: '')
    monkeypatch.setattr(worker.os, 'getppid', lambda: launch.parent_pid)
    monkeypatch.setattr(worker, 'prepare_worker_readiness', readiness)
    monkeypatch.setattr(worker, 'WORKER_HEARTBEAT_SECONDS', 0)
    monkeypatch.setattr(worker, 'WORKER_PARENT_POLL_SECONDS', 0.001)
    if outcome != 'not_ready':
        write_json_file_atomic(launch.gateway_root/'workers/control.json',
                              {'c_run_id': launch.partition.c_run_id, 'drain' if outcome == 'drained' else 'exit': True})
    return launch, pump, notices, effects, notice


@pytest.mark.parametrize('outcome', ['not_ready', 'drained', 'stopped'])
def test_original_worker_close_success_has_stopped_terminal_with_original_reason(setup, monkeypatch, outcome):
    launch, pump, notices, effects, notice = prepare(setup, monkeypatch, outcome)
    assert worker.run_worker(launch, notice) == 0
    assert notices[-2]['loop_health'] == 'stopping'
    assert notices[-1]['loop_health'] == 'stopped'
    assert notices[-1]['readiness']['reason'] == outcome
    assert notices[-1]['ready'] is False and notices[-1]['inflight'] == 0
    assert effects.count('close_entered') == effects.count('original_close_returned') == effects.count('close_returned')
    assert effects[-2:] == ['close_returned', 'publish_stopped']
    assert pump.claiming_stopped and pump.inflight == 0
    assert read_json_file(launch.gateway_root/f'workers/w{launch.partition.index}.json') == notices[-1]


@pytest.mark.parametrize('outcome,fail_at', [('not_ready', 1), ('drained', 1), ('stopped', 2)])
def test_original_worker_close_failure_never_publishes_stopped(setup, monkeypatch, outcome, fail_at):
    error = RuntimeError('synthetic original resource stop failure')
    launch, _pump, notices, effects, notice = prepare(setup, monkeypatch, outcome, close_error=error, fail_at=fail_at)
    with pytest.raises(RuntimeError) as raised:
        worker.run_worker(launch, notice)
    assert raised.value is error
    assert effects.count('close_entered') == 2
    assert effects.count('original_close_returned') == 1
    assert not any(row.get('loop_health') == 'stopped' for row in notices)
    assert notices[-1]['ready'] is False
    if fail_at == 1:
        assert notices[-1]['readiness']['reason'] == 'worker_runtime_failed'
        assert notices[-1]['readiness']['error_code'] == 'RuntimeError'
    else:
        assert notices[-1]['loop_health'] == 'stopping'
        assert notices[-1]['readiness']['reason'] == outcome


@pytest.mark.parametrize('fails', [False, True])
def test_native_cleanup_must_return_before_stopped(setup, monkeypatch, fails):
    error = RuntimeError('synthetic native close failure') if fails else None
    launch, _pump, notices, effects, notice = prepare(setup, monkeypatch, 'stopped', native_error=error, native_mode=True)
    if fails:
        with pytest.raises(RuntimeError) as raised:
            worker.run_worker(launch, notice)
        assert raised.value is error
        assert effects[-1] == 'native_close_entered'
        assert not any(row.get('loop_health') == 'stopped' for row in notices)
    else:
        assert worker.run_worker(launch, notice) == 0
        assert effects[-2:] == ['native_closed', 'publish_stopped']
        assert notices[-1]['loop_health'] == 'stopped'
        assert notices[-1]['readiness']['reason'] == 'stopped'
    assert effects.count('close_entered') == effects.count('original_close_returned')
