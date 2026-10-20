"""CLI同Agent装配与runtime单向依赖；仅原真实Agent/临时home，进程循环边界受控。"""
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.gateway_parts import supervisor as runtime
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.cli import supervisor as cli
from agent_py_agent.cli.common import make_agent


# LLM: 测试配置显式N/echo/home/工作区，实际make_agent仍走原配置/owner解析；不启服务或模型。
# 函数用途: 给唯一CLI装配测试创建原公开输入。
def selected_arguments(tmp_path, count):
    path = tmp_path / 'agent.yaml'
    path.write_text(f'model_backend: echo\nmy_agent_home: "{tmp_path / "home"}"\ngateway_worker_process_count: {count}\n', encoding='utf-8')
    return Namespace(config=str(path), workspace_root=str(tmp_path / 'home/owners/local/main/workspace'), timeout=1)


@pytest.mark.parametrize('count', [0, 2])
def test_run_cli_constructs_exactly_one_original_agent_and_runtime_reuses_it(tmp_path, monkeypatch, count):
    args = selected_arguments(tmp_path, count)
    agents = []

    def construct(selected):
        agent = make_agent(selected)
        agents.append(agent)
        return agent

    def controlled_loop(supervisor):
        assert supervisor._agent is agents[0]
        assert supervisor._agent.config.gateway_worker_process_count == count
        assert supervisor.config_path == str(Path(args.config).resolve())
        supervisor._resolve_agent_and_paths()
        assert supervisor._paths == gateway_paths(agents[0])
        assert agents[0].workspace_roots == [Path(args.workspace_root).resolve()]
        return 23

    monkeypatch.setattr(cli, 'make_agent', construct)
    monkeypatch.setattr(runtime.GatewaySupervisor, 'run', controlled_loop)
    assert cli.cmd_supervisor_run(args) == 23
    assert len(agents) == 1


@pytest.mark.parametrize('count', [0, 2])
@pytest.mark.parametrize('command', ['start', 'stop'])
def test_process_helpers_consume_paths_from_same_cli_agent_without_reconstruction(tmp_path, monkeypatch, count, command):
    args = selected_arguments(tmp_path, count)
    agents = []

    def construct(selected):
        agent = make_agent(selected)
        agents.append(agent)
        return agent

    monkeypatch.setattr(cli, 'make_agent', construct)
    monkeypatch.setattr(runtime, 'read_supervisor_process_report', lambda path:
        runtime.SupervisorPayloadReport({'alive': True, 'pid': 111, 'identity_verified': True}))
    spawn = Mock(side_effect=AssertionError('must not spawn'))
    signal = Mock(side_effect=AssertionError('must not signal'))
    monkeypatch.setattr(cli, '_spawn_daemon', spawn)
    monkeypatch.setattr(runtime, 'terminate_pid', signal)
    call = cli.cmd_supervisor_start if command == 'start' else cli.cmd_supervisor_stop
    assert call(args) == 0
    assert len(agents) == 1
    assert agents[0].config.gateway_worker_process_count == count
    assert gateway_paths(agents[0]).supervisor_pid.name == 'supervisor.pid'
    spawn.assert_not_called()
    signal.assert_not_called()


@pytest.mark.parametrize('fault', ['missing', 'other_config', 'callback'])
def test_runtime_rejects_missing_conflicting_or_callback_instead_of_original_agent(tmp_path, fault):
    args = selected_arguments(tmp_path, 0)
    agent = make_agent(args)
    config = args.config
    if fault == 'missing':
        agent = None
    elif fault == 'other_config':
        config = str(tmp_path / 'other.yaml')
    else:
        original = agent

        def agent():
            return original
    with pytest.raises(ValueError):
        runtime.GatewaySupervisor(config, agent=agent)


def test_runtime_constructor_requires_explicit_resolved_agent():
    with pytest.raises(TypeError):
        runtime.GatewaySupervisor('unresolved.yaml')


def test_path_resolution_does_not_reparse_or_construct_after_cli_binding(tmp_path):
    args = selected_arguments(tmp_path, 0)
    agent = make_agent(args)
    expected = gateway_paths(agent)
    Path(args.config).unlink()
    supervisor = runtime.GatewaySupervisor(args.config, agent=agent)
    supervisor._resolve_agent_and_paths()
    assert supervisor._agent is agent
    assert supervisor._paths == expected
