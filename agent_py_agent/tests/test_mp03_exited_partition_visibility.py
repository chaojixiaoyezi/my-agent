# LLM: 真实短命child验证原内核退出观察；心跳测试用原C/lease和受控worker组，不启动模型或监听服务。
# 模块用途: 验证退出分区可见、日志去重、Count0空值及故障隔离。
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.multiprocess_drain_process import (
    OriginalWorkerExitObservation,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.cli import gateway_loops as loops
from agent_py_agent.cli import gateway_worker_runtime as workers
from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case
from agent_py_agent.tests.test_gateway_worker_runtime import context as context
from agent_py_agent.tests.test_gateway_worker_runtime import physical as physical


# LLM: 只spawn本测试的真实Python child，先安装原observer再等待；不调用Popen.poll、不伪造waitstatus。
# 函数用途: 验证短命worker得到真实退出确认并出现在分区列表。
def test_exited_partitions_reports_confirmed_exit(tmp_path):
    process = subprocess.Popen([sys.executable, '-c', 'pass'])
    observation = None
    try:
        observation = OriginalWorkerExitObservation(process, workers.worker_process_identity(process.pid))
        if observation.supported is not True:
            pytest.skip('original worker waitstatus observation unsupported on this platform')
        observation.install()
        group = workers.CoordinatorWorkers(SimpleNamespace(paths=gateway_paths_from_root(tmp_path)), None, 0)
        group._created_origins = [(None, process, observation)]
        deadline = time.monotonic() + 5
        while group.exited_partitions() != (0,) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert observation.confirmed and group.exited_partitions() == (0,)
        assert process.returncode == 0
        assert process.wait(timeout=1) == 0
    finally:
        if process.returncode is None:
            process.kill()
            process.wait(timeout=5)


# LLM: 原实际start与close保留，只有既有physical夹具的物理Popen/native边界受控。
# 函数用途: 验证成功start登记原组，关闭撤销，Count0查询为空。
def test_process_local_registry_tracks_successful_start_and_close(physical):
    root = str(physical.control.paths.root)
    assert workers.observe_exited_worker_partitions(physical.control.paths) == ()
    physical.start()
    assert workers._CURRENT_WORKERS[root] is physical
    assert workers.observe_exited_worker_partitions(physical.control.paths) == ()
    for process in physical.processes:
        process.exit_status = 0
    assert workers.observe_exited_worker_partitions(physical.control.paths) == tuple(range(len(physical.processes)))
    physical.close()
    assert root not in workers._CURRENT_WORKERS
    assert workers.observe_exited_worker_partitions(physical.control.paths) == ()


# LLM: 不构造死亡证据，只为日志/载荷测试提供已确认观察；原心跳lease末写门保持真实。
# 函数用途: 将测试内受控分区观察注册到同一个原C。
def _register_group(control, monkeypatch):
    group = workers.CoordinatorWorkers(control, None, 0)
    process = SimpleNamespace(pid=987654321, returncode=-9, poll=Mock(side_effect=AssertionError('no poll')))
    observation = SimpleNamespace(observe=Mock(return_value=True), confirmed=True)
    group._created_origins = [(None, process, observation)]
    monkeypatch.setitem(workers._CURRENT_WORKERS, str(control.paths.root), group)
    return group, process, observation


# LLM: 原C根/lease/心跳文件真正写入，重复心跳不重复日志；不同代次同分区必须重新记录。
# 函数用途: 验证退出分区载荷和每代一次的事件及stderr诊断。
@pytest.mark.parametrize('with_agent', [True, False])
def test_heartbeat_payload_includes_exited_partitions_and_logs_once(tmp_path, monkeypatch, capsys, with_agent):
    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, _, _):
        control = gate.paths._coordinator_role_lease.control_plane
        group, process, observation = _register_group(control, monkeypatch)
        event = Mock()
        monkeypatch.setattr(loops, 'log_gateway_event', event)
        business = agent if with_agent else None
        for _ in range(2):
            loops._write_gateway_heartbeat(gate.paths, business, status='running', pid=os.getpid())
            assert read_json_file(gate.paths.heartbeat)['worker_partitions_exited'] == [0]
        notices = [json.loads(line) for line in capsys.readouterr().err.splitlines()
                   if 'gateway_worker_partition_exited' in line]
        payload = {'worker': 0, 'pid': process.pid, 'returncode': -9}
        assert notices == [{'event': 'gateway_worker_partition_exited', **payload}]
        assert observation.observe.call_count == 2
        process.poll.assert_not_called()
        if with_agent:
            event.assert_called_once_with(agent, 'gateway_worker_partition_exited', payload)
        else:
            event.assert_not_called()
        # 仅改变本测试日志分组的代次，不给真实lease换代或执行权限。
        group.control = SimpleNamespace(c_run_id='another-generation')
        loops._write_gateway_heartbeat(gate.paths, business, status='running', pid=os.getpid())
        assert capsys.readouterr().err.count('gateway_worker_partition_exited') == 1


# LLM: 观察异常由原tick错误路径记录，原心跳末写仍必须执行；不删除断言或吞外层失败。
# 函数用途: 验证退出观察故障不让心跳停止。
def test_observation_failure_records_error_but_heartbeat_continues(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (_agent, _, _, gate, _, _):
        control = gate.paths._coordinator_role_lease.control_plane
        _group, _process, observation = _register_group(control, monkeypatch)
        observation.observe.side_effect = RuntimeError('controlled_observer_fault')
        error = Mock()
        monkeypatch.setattr(loops, '_note_loop_tick_error', error)
        loops._write_gateway_heartbeat(gate.paths, None, status='running', pid=os.getpid())
        assert read_json_file(gate.paths.heartbeat)['worker_partitions_exited'] == []
        error.assert_called_once()
        assert error.call_args.args[0].context == 'gateway.worker_exit_observation'
        assert str(error.call_args.args[1]) == 'controlled_observer_fault'


# LLM: 不登记worker组的Count0调用与冷C均返回空；此测试原C心跳源仍真实，单进程注册查询独立核空。
# 函数用途: 验证无worker组时心跳包含稳定的空分区列表。
def test_absent_workers_heartbeat_has_empty_exited_partitions(tmp_path, monkeypatch):
    assert workers.observe_exited_worker_partitions(gateway_paths_from_root(tmp_path / 'count0')) == ()
    with curator_case(tmp_path, monkeypatch) as (_agent, _, _, gate, _, _):
        loops._write_gateway_heartbeat(gate.paths, None, status='running', pid=os.getpid())
        assert read_json_file(gate.paths.heartbeat)['worker_partitions_exited'] == []


# LLM: 日志系统故障不能擦掉已经确认的退出事实；心跳末写和tick错误记录都必须发生。
# 函数用途: 验证事件日志失败时仍能从心跳看到退出分区。
def test_event_log_failure_keeps_confirmed_partition_visible(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, _, _, gate, _, _):
        control = gate.paths._coordinator_role_lease.control_plane
        _register_group(control, monkeypatch)
        monkeypatch.setattr(loops, 'log_gateway_event', Mock(side_effect=OSError('controlled_log_fault')))
        errors = Mock()
        monkeypatch.setattr(loops, '_note_loop_tick_error', errors)
        loops._write_gateway_heartbeat(gate.paths, agent, status='running', pid=os.getpid())
        assert read_json_file(gate.paths.heartbeat)['worker_partitions_exited'] == [0]
        errors.assert_called_once()
