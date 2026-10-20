"""监护公开启动的当前配置与Count0来源负向预核；不创建实际服务或迁移授权。"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import supervisor as sv
from agent_py_agent.agent.gateway_parts.daemon_control import RunningPidReport
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.tests.test_gateway_standalone_migration import stopped_home
from agent_py_agent.tests.test_gateway_worker_runtime import context, physical
from agent_py_agent.tests.test_supervisor_generation_recovery import sealed_generation

_FIXTURES = (context, physical, sealed_generation)


@pytest.mark.parametrize('target_count', [0, 3])
def test_current_selected_yaml_topology_mismatch_must_not_spawn(sealed_generation, monkeypatch, target_count):
    case = sealed_generation
    assert target_count != case.supervisor._agent.config.gateway_worker_process_count
    with open(case.supervisor.config_path, 'w') as stream:
        stream.write(f'model_backend: echo\ngateway_worker_process_count: {target_count}\n')
    effects = []
    monkeypatch.setattr(sv, 'get_running_pid_report', lambda *a, **kw: RunningPidReport(None))
    monkeypatch.setattr(sv, '_spawn_gateway_process', lambda *a, **kw: effects.append('spawn') or SimpleNamespace(pid=99991))
    monkeypatch.setattr(sv, '_wait_for_gateway_start', lambda *a, **kw: None)
    assert sv.start_gateway(case.supervisor) is None
    assert effects == []


@pytest.mark.parametrize('field', ['my_agent_home', 'gateway_workspace'])
def test_current_yaml_home_or_gateway_root_change_has_zero_spawn(sealed_generation, monkeypatch, field, tmp_path):
    case = sealed_generation
    path = Path(case.supervisor.config_path)
    lines = path.read_text().splitlines()
    path.write_text('\n'.join(f'{field}: "{tmp_path / "changed-root"}"' if line.startswith(field+':') else line for line in lines)+'\n')
    effects = []
    monkeypatch.setattr(sv, 'get_running_pid_report', lambda *a, **kw: RunningPidReport(None))
    monkeypatch.setattr(sv, '_spawn_gateway_process', lambda *a: effects.append('spawn'))
    assert sv.start_gateway(case.supervisor) is None
    assert effects == []


def test_current_same_yaml_and_original_mp_closure_allow_public_start_attempt(sealed_generation, monkeypatch):
    case = sealed_generation
    effects = []
    monkeypatch.setattr(sv, 'get_running_pid_report', lambda *a, **kw: RunningPidReport(None))
    monkeypatch.setattr(sv, '_spawn_gateway_process', lambda *a: effects.append('spawn') or object())
    monkeypatch.setattr(sv, '_wait_for_gateway_start', lambda *a: None)
    assert sv.start_gateway(case.supervisor) is None
    assert effects == ['spawn']


@pytest.mark.parametrize('fault', ['none', 'missing_stop', 'mixed_mp'])
def test_supported_standalone_stop_only_preflights_before_real_child_capture(tmp_path, monkeypatch, fault):
    case = stopped_home(tmp_path, monkeypatch)
    agent = SimpleNamespace(config=case.config, home_paths=home_paths(case.home))
    supervisor = sv.GatewaySupervisor(str(case.args.config), agent=agent)
    supervisor._paths = case.paths
    state = json.loads(case.paths.state.read_bytes())
    assert state['worker_count'] == 0 and 'c_run_id' not in state
    if fault == 'missing_stop':
        state.pop('standalone_stop')
        write_json_file_atomic(case.paths.state, state)
    elif fault == 'mixed_mp':
        state['c_run_id'] = 'unproven-mixed-generation'
        state['worker_count'] = 2
        write_json_file_atomic(case.paths.state, state)
    before = case.paths.state.read_bytes()
    effects = []
    monkeypatch.setattr(sv, 'get_running_pid_report', lambda *a, **kw: RunningPidReport(None))
    monkeypatch.setattr(sv, '_spawn_gateway_process', lambda *a: effects.append('spawn') or object())
    monkeypatch.setattr(sv, '_wait_for_gateway_start', lambda *a: None)
    assert sv.start_gateway(supervisor) is None
    assert effects == (['spawn'] if fault == 'none' else [])
    assert case.paths.state.read_bytes() == before
    assert not (case.paths.root / 'standalone_migrations').exists(), 'parent cannot create a migration variant/archive'
