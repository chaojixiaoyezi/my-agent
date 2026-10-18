# LLM: 正例复用原C guard/P4 publisher及心跳producer，只替换已有物理端点；不启动服务、子进程、模型或native。
# 模块用途: 区分当前W动态投影与身份/旧代原字节变化，角色分类不替代健康、准入和物理关闭证明。
import copy
import json
import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import daemon_metadata
from agent_py_agent.agent.gateway_parts import multiprocess_bootstrap_ready as ready
from agent_py_agent.agent.gateway_parts import multiprocess_state as state
from agent_py_agent.agent.gateway_parts import multiprocess_takeover_runtime as takeover
from agent_py_agent.agent.gateway_parts import multiprocess_takeover_sources as sources
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_publish import read_worker_registry
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context
from agent_py_agent.tests.test_gateway_role_lifecycle_closure import closing as closing
from agent_py_agent.tests.test_gateway_worker_runtime import physical as physical


# LLM: 原producer运行于本测试的受控W出生投影；原C PID和全局os保持，记录仍经原完整schema校验。
# 函数用途: 构造该实际P4来源中指定W的心跳，不手写可授权的P4或进程证明。
def publish_heartbeat(workers, monkeypatch, *, index=0, stamp=1.0, healthy=True):
    lane, process, _observation = workers._created_origins[index]
    registry = read_worker_registry(workers.control.paths.root)
    entry = next(row for row in registry['workers'] if row['worker'] == index)
    readiness = {'ready': True, 'gateway_ports': [registry['http_port']]} if healthy else {'ready': False, 'inflight': 1}
    with monkeypatch.context() as patch:
        patch.setattr(state, 'os', SimpleNamespace(getpid=lambda: process.pid))
        patch.setattr(state, 'build_process_identity', lambda: copy.deepcopy(entry['process_identity']))
        patch.setattr(ready, 'process_birth', lambda _pid: entry['process_identity']['start_time'])
        heartbeat = ready.registered_worker_heartbeat(lane.launch, lane.launch.instance_id, readiness, stamp)
    write_json_file_atomic(workers.control.paths.root / 'workers' / f'w{index}.json', heartbeat)
    return heartbeat


@pytest.fixture
def running(physical, monkeypatch):
    physical.start()
    for index in range(physical.control.worker_count):
        publish_heartbeat(physical, monkeypatch, index=index)
    return physical


# LLM: 只在原reader第一次完整快照完成后发表一次测试变化；不重试、不延长预算，所有后续读取仍走原nofollow。
# 函数用途: 确定性重现原心跳和role双读交错，不依赖线程调度或真实时间。
def after_first_snapshot(monkeypatch, change):
    original = sources._role_snapshot
    seen = []

    def snapshot(root):
        result = original(root)
        seen.append(result)
        if len(seen) == 1:
            change()
        return result

    monkeypatch.setattr(sources, '_role_snapshot', snapshot)
    return seen


def test_original_producer_dynamic_heartbeat_does_not_permanently_quarantine(running, monkeypatch):
    before = read_worker_registry(running.control.paths.root)
    seen = after_first_snapshot(monkeypatch, lambda: publish_heartbeat(running, monkeypatch, stamp=2.0, healthy=False))
    controller = running.control.controller()
    assert takeover._refresh_targets(controller) is True
    assert controller._all_quarantined is False
    assert seen[0]['workers/w0.json'] != seen[1]['workers/w0.json']
    assert read_worker_registry(running.control.paths.root) == before
    # 角色归属可稳定，但非ready仍不能通过原15秒健康/端口门。
    candidate = json.loads(seen[1]['workers/w0.json'])
    with pytest.raises(ready.WorkerReadyError, match='worker_unhealthy'):
        ready.require_current_worker(before, candidate, running._created_origins[0][0].launch.partition, 2.0)


@pytest.mark.parametrize('fault', ['pid', 'birth', 'host', 'identity_schema', 'instance', 'index', 'count', 'generation',
                                 'schema', 'unknown_field', 'missing_field', 'bad_dynamic_type', 'missing_entry',
                                 'registry_raw', 'filename_binding', 'add_name', 'remove_name', 'c_state'])
def test_each_nonheartbeat_change_still_refuses(running, monkeypatch, fault):
    root = running.control.paths.root
    marker = root / 'workers/w0.json'
    if fault == 'c_state':
        write_json_file_atomic(root / 'gateway_state.json', {'c_run_id': running.control.c_run_id, 'pid': os.getpid(), 'state': 'one'})

    def change():
        record = json.loads(marker.read_bytes())
        if fault in {'pid', 'birth', 'host', 'identity_schema'}:
            field, value = {'pid': ('pid', 999998), 'birth': ('start_time', 'other-birth'),
                            'host': ('host_id', 'other-host'), 'identity_schema': ('schema_version', 'unknown.v1')}[fault]
            record['process_identity'][field] = value
            if fault == 'pid':
                record['pid'] = value
        elif fault in {'instance', 'index', 'count', 'generation', 'schema', 'bad_dynamic_type'}:
            field, value = {'instance': ('instance_id', 'other-instance'), 'index': ('worker', 1), 'count': ('of', 3),
                            'generation': ('c_run_id', 'other-generation'), 'schema': ('schema_version', 'unknown.v1'),
                            'bad_dynamic_type': ('inflight', True)}[fault]
            record[field] = value
        elif fault == 'unknown_field':
            record['extra'] = True
        elif fault == 'missing_field':
            del record['updated_at']
        elif fault in {'missing_entry', 'registry_raw'}:
            path = root / 'workers/registry.json'
            if fault == 'registry_raw':
                path.write_bytes(path.read_bytes() + b' ')
            else:
                value = json.loads(path.read_bytes())
                value['workers'] = value['workers'][1:]
                write_json_file_atomic(path, value)
            return
        elif fault == 'filename_binding':
            record = json.loads((root / 'workers/w1.json').read_bytes())
        elif fault == 'add_name':
            write_json_file_atomic(root / 'workers/w9.json', record)
            return
        elif fault == 'remove_name':
            marker.unlink()
            return
        else:
            path = root / 'gateway_state.json'
            path.write_bytes(path.read_bytes() + b' ')
            return
        write_json_file_atomic(marker, record)

    if fault == 'registry_raw':
        original = sources._discovery_registry
        seen = []

        def registry(root):
            result = original(root)
            seen.append(result)
            if len(seen) == 1:
                change()
            return result

        monkeypatch.setattr(sources, '_discovery_registry', registry)
    else:
        after_first_snapshot(monkeypatch, change)
    with pytest.raises(ValueError):
        sources.require_role_coverage(running.control.paths, running.control.c_run_id, count=running.control.worker_count)


def test_even_unchanged_current_worker_requires_full_schema_and_p4_entry(running):
    root = running.control.paths.root
    value = json.loads((root / 'workers/w0.json').read_bytes())
    value['extra'] = 'not-a-heartbeat-field'
    write_json_file_atomic(root / 'workers/w0.json', value)
    with pytest.raises(ValueError):
        sources.require_role_coverage(running.control.paths, running.control.c_run_id, count=running.control.worker_count)


def test_partial_original_p4_before_attach_has_no_new_live_issuer_gate(physical, monkeypatch):
    from agent_py_agent.tests.test_gateway_worker_runtime import ControlledLane

    original = ControlledLane.attach
    observed = []

    def attach(lane, control, process):
        assert lane.issuer is None
        publish_heartbeat(physical, monkeypatch, index=lane.launch.partition.index)
        observed.append(sources.require_role_coverage(control.paths, control.c_run_id, count=control.worker_count))
        original(lane, control, process)

    monkeypatch.setattr(ControlledLane, 'attach', attach)
    physical.start()
    assert observed == [(), ()]


def test_exited_original_workers_still_classify_without_issuer_liveness(running, monkeypatch):
    for _lane, process, _observation in running._created_origins:
        process.exit_status = 0
    running.close(defer_native=True)
    after_first_snapshot(monkeypatch, lambda: publish_heartbeat(running, monkeypatch, stamp=2.0, healthy=False))
    assert sources.require_role_coverage(running.control.paths, running.control.c_run_id, count=running.control.worker_count) == ()


def test_closed_old_worker_timestamp_remains_full_raw(closing, monkeypatch):
    closing.seal_generation()
    paths = closing.control.paths
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _identity: False)
    captured = sources.require_role_coverage(paths, 'next-c', count=closing.scope.count)
    marker = paths.root / 'workers/w0.json'

    def change():
        value = json.loads(marker.read_bytes())
        value['updated_at'] += 1
        write_json_file_atomic(marker, value)

    after_first_snapshot(monkeypatch, change)
    with pytest.raises(ValueError, match='generation_role_sources_changed'):
        sources.require_role_coverage(paths, 'next-c', count=closing.scope.count, captured=captured)
