"""公开supervisor-start首次私有设施准备；原配置/身份与真实log打开保留，进程边界受控。"""
import os
import stat
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.cli import supervisor as cli
from agent_py_agent.cli.parser import main


# LLM: YAML/原make_agent与公开parser都真实执行；仅Popen/等待回执受控，不启动服务或补造PID身份。
# 函数用途: 给首次启动与危险父链回归准备独立临时输入和可观察的原spawn出口。
def public_case(tmp_path, monkeypatch, count=0):
    config = tmp_path / 'agent.yaml'
    home = tmp_path / 'home'
    config.write_text(f'model_backend: echo\nmy_agent_home: {home}\ngateway_worker_process_count: {count}\n')
    agents, spawned = [], []
    original = cli.make_agent
    def construct(args):
        agent = original(args)
        agents.append(agent)
        return agent
    def popen(cmd, **kwargs):
        spawned.append((cmd, kwargs['stdout'].name))
        return SimpleNamespace(pid=12345)
    monkeypatch.setattr(cli, 'make_agent', construct)
    monkeypatch.setattr(cli.subprocess, 'Popen', popen)
    monkeypatch.setattr(cli, '_wait_for_supervisor_start', lambda *args: True)
    return SimpleNamespace(config=config, home=home, agents=agents, spawned=spawned,
        argv=['--config', str(config), 'gateway', 'supervisor-start'])


@pytest.mark.parametrize('count', [0, 2])
def test_fresh_public_supervisor_start_prepares_private_gateway_parent_before_log(tmp_path, monkeypatch, count):
    case = public_case(tmp_path, monkeypatch, count)
    original_umask = os.umask(0o002)
    try:
        assert main(case.argv) == 0
    finally:
        os.umask(original_umask)
    assert len(case.agents) == len(case.spawned) == 1
    agent = case.agents[0]
    paths = gateway_paths(agent)
    assert paths.log.is_file() and paths.log.stat().st_size == 0
    assert case.spawned[0][1] == str(paths.log)
    assert case.spawned[0][0][-2:] == ['gateway', 'supervisor']
    assert case.spawned[0][0][case.spawned[0][0].index('--config') + 1] == str(case.config)
    assert agent.config.gateway_worker_process_count == count
    for directory in [paths.root, paths.root.parent, paths.root.parent.parent]:
        assert directory.is_dir() and not directory.is_symlink()
        if os.name != 'nt':
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert not paths.supervisor_pid.exists() and not paths.pid.exists() and not paths.state.exists()


@pytest.mark.parametrize('component', ['runtime', 'services', 'gateway'])
def test_public_supervisor_start_refuses_symlink_parent_without_spawn_or_foreign_write(tmp_path, monkeypatch, component):
    case = public_case(tmp_path, monkeypatch)
    outside = tmp_path / 'outside'
    outside.mkdir()
    original = cli.make_agent
    def with_link(args):
        agent = original(args)
        parts = gateway_paths(agent).root.relative_to(agent.root).parts
        position = parts.index(component)
        parent = agent.root.joinpath(*parts[:position])
        parent.mkdir(parents=True, exist_ok=True)
        target = parent / component
        if target.exists():
            target.rename(parent / (component + '-original'))
        target.symlink_to(outside, target_is_directory=True)
        return agent
    monkeypatch.setattr(cli, 'make_agent', with_link)
    with pytest.raises(NoFollowPathError):
        main(case.argv)
    assert not case.spawned and list(outside.iterdir()) == []


def test_existing_gateway_parent_permissions_are_not_rewritten(tmp_path, monkeypatch):
    case = public_case(tmp_path, monkeypatch)
    original = cli.make_agent
    prepared = []
    def with_existing(args):
        agent = original(args)
        root = gateway_paths(agent).root
        root.mkdir(parents=True)
        root.chmod(0o750)
        prepared.append(root)
        return agent
    monkeypatch.setattr(cli, 'make_agent', with_existing)
    assert main(case.argv) == 0 and len(case.spawned) == 1
    if os.name != 'nt':
        assert stat.S_IMODE(prepared[0].stat().st_mode) == 0o750


def test_refused_config_never_prepares_gateway_parent_or_spawns(tmp_path, monkeypatch):
    case = public_case(tmp_path, monkeypatch)
    def reject(_args):
        raise ValueError('合成配置预核拒绝')
    monkeypatch.setattr(cli, 'make_agent', reject)
    with pytest.raises(ValueError, match='合成配置预核拒绝'):
        main(case.argv)
    assert not case.spawned and not case.home.exists()


def test_existing_unknown_supervisor_record_does_not_prepare_or_spawn(tmp_path, monkeypatch):
    case = public_case(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, 'is_supervisor_running', lambda paths: True)
    assert main(case.argv) == 0 and len(case.agents) == 1
    assert not case.spawned and not gateway_paths(case.agents[0]).root.exists()


def test_fresh_public_supervisor_start_preserves_explicit_absolute_service_override(tmp_path, monkeypatch):
    case = public_case(tmp_path, monkeypatch)
    service = tmp_path / 'outside-home' / 'explicit service'
    with case.config.open('a') as config:
        config.write(f'gateway_workspace: {service}\n')
    assert main(case.argv) == 0
    assert len(case.agents) == len(case.spawned) == 1
    paths = gateway_paths(case.agents[0])
    assert paths.root == service and paths.log.is_file()
    assert case.spawned[0][1] == str(service / 'gateway.log')
    assert not service.is_relative_to(case.agents[0].root)
    if os.name != 'nt':
        assert stat.S_IMODE(service.stat().st_mode) == 0o700


def test_explicit_absolute_service_alias_is_not_resolved_or_followed(tmp_path, monkeypatch):
    case = public_case(tmp_path, monkeypatch)
    outside = tmp_path / 'outside'
    outside.mkdir()
    alias = tmp_path / 'service-alias'
    alias.symlink_to(outside, target_is_directory=True)
    service = alias / 'gateway'
    with case.config.open('a') as config:
        config.write(f'gateway_workspace: {service}\n')
    with pytest.raises(NoFollowPathError):
        main(case.argv)
    assert gateway_paths(case.agents[0]).root == service
    assert not case.spawned and list(outside.iterdir()) == []


def test_service_link_parent_traversal_is_refused_before_preparation_or_spawn(tmp_path, monkeypatch):
    case = public_case(tmp_path, monkeypatch)
    target = tmp_path / 'elsewhere' / 'nested'
    target.mkdir(parents=True)
    alias = tmp_path / 'service-alias'
    alias.symlink_to(target, target_is_directory=True)
    service = alias / '..' / 'gateway'
    with case.config.open('a') as config:
        config.write(f'gateway_workspace: {service}\n')
    prepared = []
    original = cli.ensure_private_dir
    def prepare(*args, **kwargs):
        prepared.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(cli, 'ensure_private_dir', prepare)
    with pytest.raises(ValueError, match='规范绝对服务目录'):
        main(case.argv)
    assert not prepared and not case.spawned
    assert not (tmp_path / 'gateway').exists()
    assert list(target.parent.iterdir()) == [target] and list(target.iterdir()) == []
