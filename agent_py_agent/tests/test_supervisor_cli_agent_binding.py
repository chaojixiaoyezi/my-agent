"""CLI健康读取使用已经解析的Agent，不生成第二配置或启动实际服务。"""
from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.cli import supervisor as cli
from agent_py_agent.tests.test_gateway_service_role_identity import health
from agent_py_agent.tests.test_supervisor_generation_recovery import healthy_generation

_FIXTURES = (health, healthy_generation)


@pytest.mark.parametrize('command', ['status', 'start_all'])
def test_cli_reuses_parsed_agent_for_real_gateway_health(health, monkeypatch, capsys, command):
    agent = health.supervisor._agent
    agent.config.config_path = '/synthetic/selected-agent.yaml'
    make = Mock(return_value=agent)
    monkeypatch.setattr(cli, 'make_agent', make)
    monkeypatch.setattr(cli, 'gateway_paths', lambda value: health.paths if value is agent else None)
    monkeypatch.setattr(cli, 'get_running_pid_report', lambda *a, **kw: cli_read_report())
    monkeypatch.setattr(cli, 'read_supervisor_process_report', lambda *a: SimpleNamespace(payload={}, load_error=None))
    monkeypatch.setattr(cli, 'is_supervisor_running', lambda *a: True)
    spawn = Mock(side_effect=AssertionError('health read must not spawn'))
    monkeypatch.setattr(cli, '_spawn_daemon', spawn)
    args = Namespace(config=agent.config.config_path, adapter_channel='none')
    before = {p: p.read_bytes() for p in (health.paths.pid, health.paths.state, health.paths.heartbeat)}
    call = cli.cmd_supervisor_status if command == 'status' else cli.cmd_start_all
    assert call(args) == 0
    assert make.call_count == 1
    assert '就绪' in capsys.readouterr().out
    assert {p: p.read_bytes() for p in before} == before
    spawn.assert_not_called()


# LLM: 仅替身OS PID读口，真实role/state/heartbeat/worker判断仍走产品，不赋予ready授权。
# 函数用途: 提供健康Count0夹具对应的固定进程投影，不向任何进程发信号。
def cli_read_report():
    from agent_py_agent.agent.gateway_parts.daemon_control import RunningPidReport

    return RunningPidReport(12345)


@pytest.mark.parametrize('command', ['status', 'start_all'])
def test_cli_keeps_actual_count2_and_full_worker_health(healthy_generation, monkeypatch, capsys, command):
    from agent_py_agent.agent.gateway_parts.daemon_control import RunningPidReport

    case = healthy_generation
    agent = case.supervisor._agent
    agent.config.config_path = case.supervisor.config_path
    make = Mock(return_value=agent)
    monkeypatch.setattr(cli, 'make_agent', make)
    monkeypatch.setattr(cli, 'gateway_paths', lambda selected: case.paths if selected is agent else None)
    monkeypatch.setattr(cli, 'get_running_pid_report', lambda *a, **kw: RunningPidReport(case.identity['pid']))
    monkeypatch.setattr(cli, 'read_supervisor_process_report', lambda *a: SimpleNamespace(payload={}, load_error=None))
    monkeypatch.setattr(cli, 'is_supervisor_running', lambda *a: True)
    spawn = Mock(side_effect=AssertionError('healthy existing Count2 must not spawn'))
    monkeypatch.setattr(cli, '_spawn_daemon', spawn)
    args = Namespace(config=agent.config.config_path, adapter_channel='none')
    call = cli.cmd_supervisor_status if command == 'status' else cli.cmd_start_all
    before = {p: p.read_bytes() for p in case.paths.root.rglob('*.json')}
    assert call(args) == 0
    assert make.call_count == 1
    assert f"pid={case.identity['pid']}" in capsys.readouterr().out
    assert all(path.read_bytes() == raw for path, raw in before.items())
    spawn.assert_not_called()
