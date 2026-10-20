"""原终态writer到P6/监护冷启动的身份合同；物理W/native边界受控，不启动服务或模型。"""
import json
import os
from argparse import Namespace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import daemon_metadata
from agent_py_agent.agent.gateway_parts import supervisor_generation as sg
from agent_py_agent.cli import gateway_process as gateway
from agent_py_agent.cli.models import GatewayRunContext
from agent_py_agent.tests.test_dead_worker_role_closure import _dead_runtime
from agent_py_agent.tests.test_gateway_worker_runtime import context, physical

_FIXTURES = (context, physical)


# LLM: 原真实C guard/controller/P4/OriginalExit与publisher均保留；只复用既有受控W/native端点，不手写可过的closure。
# 函数用途: 从原context producer取得本次身份，让真实terminal writer直接进入P6封存和冷start读门。
@pytest.fixture
def terminal(physical, tmp_path):
    runtime = _dead_runtime(physical)
    control = runtime.control
    config = tmp_path / 'agent.yaml'
    config.write_text(f'model_backend: echo\nmy_agent_home: "{control.home_root}"\ngateway_workspace: "{control.paths.root}"\ngateway_worker_process_count: {control.worker_count}\n')
    args = Namespace(config=str(config), _gateway_worker_count=control.worker_count, _gateway_control_plane=control)
    run = gateway._gateway_run_context_from_args(gateway._GatewayRunBuildRequest(None, control.paths, args))
    assert run.process_identity['pid'] == os.getpid() and run.process_started_at > 0
    view = SimpleNamespace(config_path=str(config), _paths=control.paths,
        _agent=SimpleNamespace(config=SimpleNamespace(gateway_worker_process_count=control.worker_count),
                              home_paths=SimpleNamespace(root=control.home_root)))
    return SimpleNamespace(runtime=runtime, context=run, paths=control.paths, view=view)


def test_original_terminal_writer_seals_and_is_recapturable_by_cold_supervisor(terminal, monkeypatch):
    case = terminal
    gateway._record_gateway_service_termination(case.paths, None, os.getpid(),
        gateway._GatewayServiceTermination('stopped', 'planned_stop', 'test stop', '', 0), context=case.context)
    case.runtime.stop_producers()
    case.runtime.seal_generation()
    monkeypatch.setattr(daemon_metadata, 'process_identity_is_live', lambda _: False)
    observed = sg.capture_generation(case.view)
    sg.require_closed_observation(case.view, observed)
    sg.require_startable_generation(case.view)
    state = json.loads(case.paths.state.read_bytes())
    assert state['process_identity'] == case.context.process_identity
    assert state['started_at'] == case.context.process_started_at
    assert state['worker_count'] == case.context.coordinator_worker_count
    assert state['drain_complete'] is True


@pytest.mark.parametrize('status,exit_code', [('stopped', 0), ('failed', 2)])
def test_coordinated_terminal_identity_is_original_context_not_detail_or_old_state(terminal, status, exit_code):
    case = terminal
    details = {'process_identity': {'pid': 99999}, 'started_at': -1, 'other_detail': 'retained'}
    gateway._record_gateway_service_termination(case.paths, None, os.getpid(),
        gateway._GatewayServiceTermination(status, 'test_kind', '', '', exit_code, details), context=case.context)
    state = json.loads(case.paths.state.read_bytes())
    assert state['process_identity'] == case.context.process_identity
    assert state['started_at'] == case.context.process_started_at
    assert state['pid'] == os.getpid() and state['c_run_id'] == case.runtime.scope.c_run_id
    assert state['worker_count'] == case.runtime.scope.count
    assert state['other_detail'] == 'retained' and state['exit_code'] == exit_code
    assert ('error_code' in state) is (status == 'failed')
    assert details['process_identity'] == {'pid': 99999}, 'writer cannot mutate caller data'


@pytest.mark.parametrize('status,exit_code', [('stopped', 0), ('failed', 2)])
def test_count0_terminal_projection_preserves_original_fields(tmp_path, monkeypatch, status, exit_code):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    paths = gateway_paths_from_root(tmp_path)
    identity = {'host_id': 'synthetic-host', 'pid': 1234, 'start_time': 'synthetic-birth'}
    context = GatewayRunContext(agent=None, paths=paths, config_path=tmp_path/'agent.yaml',
        process_identity=identity, process_started_at=100.0, coordinator_worker_count=0)
    events = []
    monkeypatch.setattr(gateway.time, 'time', lambda: 1234.0)
    monkeypatch.setattr(gateway, 'log_gateway_event', lambda agent, event, payload: events.append((event, dict(payload))))
    gateway._record_gateway_service_termination(paths, None, 1234,
        gateway._GatewayServiceTermination(status, 'test_kind', 'test_reason', 'test_summary', exit_code), context=context)
    expected = {'status': status, 'pid': 1234, 'stopped_at': 1234.0, 'termination_kind': 'test_kind',
        'termination_reason': 'test_reason', 'summary': 'test_summary', 'exit_code': exit_code,
        'process_identity': identity, 'started_at': 100.0, 'worker_count': 0}
    if status == 'failed':
        expected['error_code'] = 'GATEWAY_SERVICE_LOOP_UNEXPECTED_RETURN'
    assert json.loads(paths.state.read_bytes()) == expected
    assert events == [('gateway_run_failed' if status == 'failed' else 'gateway_run_stopped', expected)]
    assert not (paths.root/'workers/registry.json').exists()
