"""监护整代恢复组件合同：原记录/关闭producer，OS与启动边界受控；不启动服务或模型。"""
import copy
import json
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import daemon_metadata
from agent_py_agent.agent.gateway_parts import multiprocess_generation_metadata as history
from agent_py_agent.agent.gateway_parts import supervisor as sv
from agent_py_agent.agent.gateway_parts import supervisor_generation as sg
from agent_py_agent.agent.gateway_parts.daemon_control import RunningPidReport
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_records import serialize_record
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.tests.test_dead_worker_role_closure import _dead_runtime
from agent_py_agent.tests.test_gateway_worker_runtime import context, physical

_FIXTURES = (context, physical)


@pytest.fixture
def healthy_generation(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    (paths.root / 'workers').mkdir(parents=True)
    config = tmp_path / 'agent.yaml'
    home = tmp_path / 'home'
    config.write_text(f'model_backend: echo\nmy_agent_home: "{home}"\ngateway_workspace: "{paths.root}"\ngateway_worker_process_count: 2\n')
    identity = {'host_id': 'synthetic-host', 'pid': 51001, 'start_time': '51'}
    now = time.time()
    state = {'status': 'running', 'pid': 51001, 'process_identity': identity,
             'started_at': now-3, 'worker_count': 2, 'c_run_id': 'old-c'}
    agent = SimpleNamespace(config=SimpleNamespace(gateway_worker_process_count=2, config_path=str(config)), home_paths=SimpleNamespace(root=home))
    supervisor = sv.GatewaySupervisor(str(config), agent=agent)
    supervisor._paths = paths
    registry = {'schema_version': 'multiprocess_worker_registry.v1', 'c_run_id': 'old-c', 'worker_count': 2,
                'http_port': 8111, 'process_identity': {'schema_version': 'multiprocess_process_identity.v1', **identity},
                'workers': []}
    for i in range(2):
        row = {'worker': i, 'instance_id': f'instance-{i}', 'process_identity': {
            'schema_version': 'multiprocess_process_identity.v1', 'host_id': 'synthetic-host',
            'pid': 52001+i, 'start_time': f'52{i}'}, 'executor_ledger_ref': f'workers/executors/w{i}'}
        registry['workers'].append(row)
        value = {'schema_version': 'multiprocess_worker_heartbeat.v1', 'worker': i, 'of': 2,
                 'pid': row['process_identity']['pid'], 'process_identity': row['process_identity'],
                 'instance_id': row['instance_id'], 'c_run_id': 'old-c', 'ready': True, 'loop_health': 'healthy',
                 'inflight': 0, 'updated_at': now, 'readiness': {'ready': True, 'gateway_ports': [8111]}}
        (paths.root / f'workers/w{i}.json').write_bytes(serialize_record(value))
    (paths.root / 'workers/registry.json').write_bytes(serialize_record(registry))
    write_json_file_atomic(paths.state, state)
    write_json_file_atomic(paths.pid, {**identity, 'kind': 'my-agent-gateway'})
    write_json_file_atomic(paths.heartbeat, {'status': 'running', 'pid': 51001, 'started_at': now-3,
        'updated_at': now, 'dispatcher_alive': True, 'dispatcher_state': 'running'})
    monkeypatch.setattr(sv, 'get_running_pid_report', lambda *a, **kw: RunningPidReport(51001))
    monkeypatch.setattr(sv, 'process_identity_is_live', lambda value: True)
    monkeypatch.setattr(sg, 'process_identity_is_live', lambda value: True)
    return SimpleNamespace(supervisor=supervisor, paths=paths, state=state, registry=registry, identity=identity, now=now)


def test_healthy_c_and_all_original_workers_readonly(healthy_generation):
    case = healthy_generation
    before = {p: p.read_bytes() for p in case.paths.root.rglob('*.json')}
    assert case.supervisor._is_gateway_healthy()
    assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize('fault', ['dead', 'unknown', 'missing', 'stale', 'future', 'not_ready', 'wrong_instance',
    'wrong_birth', 'wrong_generation', 'missing_slot', 'wrong_count', 'malformed', 'changed_registry', 'changed_c',
    'symlink', 'hardlink'])
def test_fresh_c_cannot_hide_worker_or_generation_failure(healthy_generation, monkeypatch, fault):
    case = healthy_generation
    path = case.paths.root / 'workers/w0.json'
    value = json.loads(path.read_bytes())
    if fault in {'dead', 'unknown'}:
        monkeypatch.setattr(sg, 'process_identity_is_live', lambda _: False if fault == 'dead' else None)
    elif fault == 'missing':
        path.unlink()
    elif fault in {'symlink', 'hardlink'}:
        original = path.with_suffix('.original')
        path.rename(original)
        if fault == 'symlink':
            path.symlink_to(original)
        else:
            import os

            os.link(original, path)
    elif fault == 'missing_slot':
        case.registry['workers'].pop()
        (case.paths.root / 'workers/registry.json').write_bytes(serialize_record(case.registry))
    elif fault == 'wrong_count':
        case.state['worker_count'] = 1
        write_json_file_atomic(case.paths.state, case.state)
    elif fault == 'malformed':
        path.write_bytes(b'not json')
    elif fault == 'changed_registry':
        original = sg.read_worker_registry
        calls = []
        def read(root):
            result = original(root)
            calls.append(1)
            return result if len(calls) == 1 else {**result, 'c_run_id': 'next-c'}
        monkeypatch.setattr(sg, 'read_worker_registry', read)
    elif fault == 'changed_c':
        monkeypatch.setattr(sg, '_state', lambda _: {**case.state, 'c_run_id': 'next-c'})
    else:
        if fault == 'stale':
            value['updated_at'] = case.now-16
        elif fault == 'future':
            value['updated_at'] = case.now+100
        elif fault == 'not_ready':
            value['ready'] = False
        elif fault == 'wrong_instance':
            value['instance_id'] = 'late-instance'
        elif fault == 'wrong_birth':
            value['process_identity']['start_time'] = 'another-birth'
        else:
            value['c_run_id'] = 'oldest-c'
        path.write_bytes(serialize_record(value))
    assert not case.supervisor._is_gateway_healthy()
    assert case.supervisor._last_health_load_errors


# LLM: 由原C/OriginalExit/P4/P6实际producer封存，只有OS进程活性与spawn边界fake；不手写可通过的closure JSON。
# 函数用途: 构建能被只读监护消费的原完整关闭事实。
@pytest.fixture
def sealed_generation(physical, monkeypatch, tmp_path):
    runtime = _dead_runtime(physical)
    paths = runtime.control.paths
    registry = sg.read_worker_registry(paths.root)
    identity = {key: registry['process_identity'][key] for key in ('host_id', 'pid', 'start_time')}
    write_json_file_atomic(paths.state, {'status': 'stopped', 'pid': identity['pid'], 'process_identity': identity,
                                       'worker_count': registry['worker_count'], 'c_run_id': registry['c_run_id']})
    config = tmp_path / 'agent.yaml'
    home = runtime.control.home_root
    config.write_text(f'model_backend: echo\nmy_agent_home: "{home}"\ngateway_workspace: "{paths.root}"\ngateway_worker_process_count: {registry["worker_count"]}\n')
    agent = SimpleNamespace(config=SimpleNamespace(gateway_worker_process_count=registry['worker_count'], config_path=str(config)), home_paths=SimpleNamespace(root=home))
    supervisor = sv.GatewaySupervisor(str(config), agent=agent)
    supervisor._paths = paths
    observed = sg.capture_generation(supervisor)
    runtime.stop_producers()
    runtime.seal_generation()
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _: False)
    return SimpleNamespace(supervisor=supervisor, paths=paths, runtime=runtime, observed=observed, registry=registry)


def test_original_full_closure_allows_readonly_restart_gate(sealed_generation):
    case = sealed_generation
    before = {p: p.read_bytes() for p in case.paths.root.rglob('*.json')}
    sg.require_closed_observation(case.supervisor, case.observed)
    assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize('fault', ['missing_closure', 'unknown_life', 'live_worker', 'changed_role', 'added_source',
    'changed_registry', 'changed_config', 'changed_count', 'missing_tree', 'changed_source'])
def test_unproven_old_generation_never_spawns(sealed_generation, monkeypatch, fault):
    case = sealed_generation
    root = case.paths.root
    if fault == 'missing_closure':
        value, _ = history.read_generation_metadata(root, case.registry['c_run_id'])
        value['closure'] = None
        (root.joinpath(*history.generation_metadata_parts(case.registry['c_run_id']))).write_bytes(serialize_record(value))
    elif fault in {'unknown_life', 'live_worker'}:
        monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _: None if fault == 'unknown_life' else True)
    elif fault == 'changed_role':
        path = root / 'workers/w0.json'
        value = json.loads(path.read_bytes())
        value['updated_at'] += 1
        path.write_bytes(serialize_record(value))
    elif fault in {'added_source', 'changed_source'}:
        original = sg.generation_source_files
        monkeypatch.setattr(sg, 'generation_source_files', lambda paths: {**original(paths), 'control_operations/new.json': b'{}'})
    elif fault == 'changed_registry':
        value = copy.deepcopy(case.registry)
        value['workers'][0]['instance_id'] = 'replacement'
        (root / 'workers/registry.json').write_bytes(serialize_record(value))
    elif fault == 'changed_config':
        with open(case.supervisor.config_path, 'a') as stream:
            stream.write('# changed\n')
    elif fault == 'changed_count':
        case.supervisor._agent.config.gateway_worker_process_count += 1
    else:
        original = sg.require_closed_generation
        def unknown(*args, **kwargs):
            original(*args, **kwargs)
            raise ValueError('generation_tree_evidence_changed')
        monkeypatch.setattr(sg, 'require_closed_generation', unknown)
    effects = []
    monkeypatch.setattr(sg, 'capture_generation', lambda _: case.observed)
    monkeypatch.setattr(case.supervisor, '_stop_gateway', lambda **kw: effects.append(('stop', kw)) or True)
    monkeypatch.setattr(case.supervisor, '_start_gateway', lambda: effects.append('spawn') or 777)
    assert not case.supervisor._restart_gateway()
    assert 'spawn' not in effects


def test_restart_transports_frozen_birth_then_requires_new_generation(sealed_generation, monkeypatch):
    case = sealed_generation
    effects = []
    monkeypatch.setattr(sg, 'capture_generation', lambda _: case.observed)
    monkeypatch.setattr(case.supervisor, '_stop_gateway', lambda **kw: effects.append(('stop', kw)) or True)
    monkeypatch.setattr(case.supervisor, '_start_gateway', lambda: effects.append('spawn') or 777)
    monkeypatch.setattr(sg, 'require_successor', lambda supervisor, observed, pid: effects.append(('successor', pid)))
    assert case.supervisor._restart_gateway()
    identity = {key: case.registry['process_identity'][key] for key in ('host_id', 'pid', 'start_time')}
    assert effects == [('stop', {'timeout': 10.0, 'expected_identity': identity}), 'spawn', ('successor', 777)]
    assert case.supervisor._gateway_pid == 777


def test_restart_does_not_stop_replacement_c(healthy_generation, monkeypatch):
    case = healthy_generation
    replacement = {**case.identity, 'start_time': 'next-birth'}
    case.paths.pid.write_text(json.dumps({**replacement, 'kind': 'my-agent-gateway'}))
    effects = []
    monkeypatch.setattr(sv, '_write_supervisor_stop_request', lambda *a: effects.append('request'))
    monkeypatch.setattr(sv, 'terminate_pid', lambda *a: effects.append('signal'))
    assert not sv.stop_gateway(case.supervisor, expected_identity=case.identity)
    assert effects == []


def test_cold_start_cannot_bypass_unclosed_old_generation(healthy_generation, monkeypatch):
    case = healthy_generation
    monkeypatch.setattr(sv, 'get_running_pid_report', lambda *a, **kw: RunningPidReport(None))
    effects = []
    monkeypatch.setattr(sv, '_spawn_gateway_process', lambda *a: effects.append('spawn') or object())
    monkeypatch.setattr(sv, '_wait_for_gateway_start', lambda *a: 777)
    assert sv.start_gateway(case.supervisor) is None
    assert effects == []


def test_truly_fresh_count2_uses_existing_public_cli(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path/'fresh')
    paths.root.mkdir()
    config = tmp_path/'agent.yaml'
    home = tmp_path/'home'
    config.write_text(f'model_backend: echo\nmy_agent_home: "{home}"\ngateway_workspace: "{paths.root}"\ngateway_worker_process_count: 2\n')
    agent = SimpleNamespace(config=SimpleNamespace(gateway_worker_process_count=2, config_path=str(config)), home_paths=SimpleNamespace(root=home))
    supervisor = sv.GatewaySupervisor(str(config), agent=agent)
    supervisor._paths = paths
    monkeypatch.setattr(sv, 'get_running_pid_report', lambda *a, **kw: RunningPidReport(None))
    seen = []
    process = object()
    monkeypatch.setattr(sv, '_spawn_gateway_process', lambda sup, argv, *args: seen.append(argv) or process)
    monkeypatch.setattr(sv, '_wait_for_gateway_start', lambda sup, child: 777 if child is process else None)
    assert sv.start_gateway(supervisor) == 777
    assert seen[0][-2:] == ['gateway', 'run']
    assert '--worker-count' not in seen[0]


@pytest.mark.parametrize('fault', ['none', 'same_generation', 'same_identity', 'wrong_pid', 'wrong_count', 'unhealthy'])
def test_successor_requires_new_c_and_same_full_worker_topology(healthy_generation, monkeypatch, fault):
    case = healthy_generation
    observed = sg.capture_generation(case.supervisor)
    registry = copy.deepcopy(case.registry)
    registry['c_run_id'] = 'new-c'
    registry['process_identity']['pid'] = 53001
    registry['process_identity']['start_time'] = '53'
    if fault == 'same_generation':
        registry['c_run_id'] = 'old-c'
    if fault == 'same_identity':
        registry['process_identity'] = case.registry['process_identity']
    if fault == 'wrong_count':
        registry['worker_count'] = 3
    identity = {key: registry['process_identity'][key] for key in ('host_id', 'pid', 'start_time')}
    state = {**case.state, 'c_run_id': registry['c_run_id'], 'pid': identity['pid'], 'process_identity': identity,
             'worker_count': registry['worker_count']}
    (case.paths.root / 'workers/registry.json').write_bytes(serialize_record(registry))
    write_json_file_atomic(case.paths.state, state)
    called = []
    monkeypatch.setattr(case.supervisor, '_is_gateway_healthy', lambda: called.append('health') or fault != 'unhealthy')
    if fault == 'none':
        sg.require_successor(case.supervisor, observed, state['pid'])
        assert called == ['health']
    else:
        with pytest.raises(ValueError):
            sg.require_successor(case.supervisor, observed, 999 if fault == 'wrong_pid' else state['pid'])


def test_restart_config_change_during_old_stop_never_spawns(healthy_generation, monkeypatch):
    case = healthy_generation
    effects = []
    def stop(**kwargs):
        effects.append('stop')
        with open(case.supervisor.config_path, 'a') as stream:
            stream.write('# concurrent configuration change\n')
        return True
    monkeypatch.setattr(case.supervisor, '_stop_gateway', stop)
    monkeypatch.setattr(case.supervisor, '_start_gateway', lambda: effects.append('spawn'))
    assert not case.supervisor._restart_gateway()
    assert effects == ['stop']


def test_configuration_unknown_before_stop_has_zero_effect(healthy_generation, monkeypatch):
    case = healthy_generation
    from pathlib import Path

    Path(case.supervisor.config_path).unlink()
    effects = []
    monkeypatch.setattr(case.supervisor, '_stop_gateway', lambda **kw: effects.append('stop'))
    monkeypatch.setattr(case.supervisor, '_start_gateway', lambda: effects.append('spawn'))
    assert not case.supervisor._restart_gateway()
    assert effects == []


@pytest.mark.parametrize('code', [0, 3])
@pytest.mark.parametrize('count', [0, 1])
def test_original_child_wait_reaps_exit_instead_of_kill0_liveness(tmp_path, code, count):
    import os
    import subprocess
    import sys

    from agent_py_agent.agent.gateway_parts import multiprocess_drain_process as exits

    process = subprocess.Popen([sys.executable, '-B', '-c', f'import time; time.sleep(0.1); raise SystemExit({code})'])
    agent = SimpleNamespace(config=SimpleNamespace(gateway_worker_process_count=count, config_path=str(tmp_path/'unused.yaml')))
    supervisor = sv.GatewaySupervisor(str(tmp_path/'unused.yaml'), agent=agent)
    sg.retain_spawned_gateway(supervisor, process)
    child = supervisor._gateway_child
    try:
        deadline = time.monotonic()+5
        while exits._original_waitid(process.pid, nowait=True) is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        os.kill(process.pid, 0)
        assert sv.wait_for_pid_exit(process.pid, 0) is False, 'original kill0 sees the unreaped child'
        assert child.process is process and process.returncode is None
        assert sg.wait_spawned_gateway_exit(supervisor, process.pid, child.identity, 1) is True
        assert process.returncode == code
        assert daemon_metadata.process_identity_is_live(child.identity) is False
    finally:
        process.wait(timeout=5)


@pytest.mark.parametrize('fault', ['other_process', 'copied_record', 'wrong_identity', 'timeout'])
def test_original_child_reference_and_birth_are_not_replaceable(tmp_path, fault):
    import subprocess
    import sys

    process = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(0.5)'])
    other = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(0.5)'])
    agent = SimpleNamespace(config=SimpleNamespace(gateway_worker_process_count=0, config_path=str(tmp_path/'unused.yaml')))
    supervisor = sv.GatewaySupervisor(str(tmp_path/'unused.yaml'), agent=agent)
    sg.retain_spawned_gateway(supervisor, process)
    child = supervisor._gateway_child
    try:
        if fault == 'other_process':
            with pytest.raises(ValueError):
                sg.require_ready_child(supervisor, other, child.identity)
            assert sg.wait_spawned_gateway_exit(supervisor, other.pid, child.identity, 0) is None
        elif fault == 'copied_record':
            supervisor._gateway_child = copy.copy(child)
            with pytest.raises(ValueError):
                sg.require_ready_child(supervisor, process, child.identity)
            assert sg.wait_spawned_gateway_exit(supervisor, process.pid, child.identity, 0) is None
        elif fault == 'wrong_identity':
            wrong = {**child.identity, 'start_time': 'wrong-birth'}
            with pytest.raises(ValueError):
                sg.require_ready_child(supervisor, process, wrong)
            assert sg.wait_spawned_gateway_exit(supervisor, process.pid, wrong, 0) is None
        else:
            sg.require_ready_child(supervisor, process, child.identity)
            assert sg.wait_spawned_gateway_exit(supervisor, process.pid, child.identity, 0) is False
            assert supervisor._gateway_child is child and child.process is process
    finally:
        process.wait(timeout=5)
        other.wait(timeout=5)


def test_spawned_child_is_retained_even_when_readiness_times_out(tmp_path, monkeypatch):
    import subprocess
    import sys

    paths = gateway_paths_from_root(tmp_path/'fresh')
    paths.root.mkdir()
    config = tmp_path/'agent.yaml'
    home = tmp_path/'home'
    config.write_text(f'model_backend: echo\nmy_agent_home: "{home}"\ngateway_workspace: "{paths.root}"\ngateway_worker_process_count: 0\n')
    agent = SimpleNamespace(config=SimpleNamespace(gateway_worker_process_count=0, config_path=str(config)), home_paths=SimpleNamespace(root=home))
    supervisor = sv.GatewaySupervisor(str(config), agent=agent)
    supervisor._paths = paths
    process = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(0.3)'])
    monkeypatch.setattr(sv, 'get_running_pid_report', lambda *a, **kw: RunningPidReport(None))
    monkeypatch.setattr(sv, '_spawn_gateway_process', lambda *a: process)
    monkeypatch.setattr(sv, '_wait_for_gateway_start', lambda *a: None)
    try:
        assert sv.start_gateway(supervisor) is None
        child = getattr(supervisor, '_gateway_child', None)
        assert child is not None and child.process is process
        assert child is supervisor._gateway_child_origin
    finally:
        process.wait(timeout=5)
