"""宿主前台事实、路径和两平台沙箱投影的行为回归。"""
from __future__ import annotations

import re
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.attempt.sandbox import SandboxHostPolicy
from agent_py_agent.agent.path_access_policy import (
    PathAccessPolicy,
    host_sandbox_paths,
    macos_hidden_pattern,
    retained_host_exclusive_paths,
)
from agent_py_agent.agent.tooling.pty_sessions import _pty_access_scope
from agent_py_agent.agent.tooling.shell import _subprocess_text_env
from agent_py_agent.agent.user_space.owner_access import is_foreground_admin_interactive


def _agent(root):
    return SimpleNamespace(home_paths=SimpleNamespace(root=root, owner_id='local/main',
        owner_provider='local', owner_kind='main'))


def test_foreground_fact_default_and_host_metadata():
    assert RunParams().host_foreground_interactive is False
    spec = next(item for item in fields(RunParams) if item.name == 'host_foreground_interactive')
    assert spec.kw_only and spec.metadata['host_only'] is True
    assert 'host_foreground_interactive' not in repr(RunParams(host_foreground_interactive=True))


@pytest.mark.parametrize('value', [False, None, 1, 'true'])
def test_non_boolean_host_fact_never_elevates(tmp_path, value):
    assert not is_foreground_admin_interactive(_agent(tmp_path), params=RunParams(host_foreground_interactive=value))


@pytest.mark.parametrize('source', ['scheduler', 'background', 'background_main_agent', 'system', 'plugin', 'subagent'])
def test_background_sources_cannot_inherit_foreground(tmp_path, source):
    assert not is_foreground_admin_interactive(_agent(tmp_path), params=RunParams(host_foreground_interactive=True, source=source))


def test_canonical_identity_child_and_exact_scope(tmp_path):
    agent = _agent(tmp_path)
    params = RunParams(host_foreground_interactive=True)
    assert is_foreground_admin_interactive(agent, params=params)
    agent._current_subagent_run_id = 'child'
    assert not is_foreground_admin_interactive(agent, params=params)
    agent._current_subagent_run_id = ''
    assert not is_foreground_admin_interactive(agent, params=replace(params, turn_trigger=object()))
    assert not is_foreground_admin_interactive(agent, params=replace(params, write_boundary={'effective_permissions': {'read': True}}))
    agent.home_paths.owner_id = 'local/other'
    assert not is_foreground_admin_interactive(agent, params=params)


@pytest.mark.parametrize('grant', [False, True])
def test_path_grant_preserves_business_other_owner_persona_task_records(tmp_path, grant):
    root = tmp_path / 'home'
    own = root / 'owners/local/main'
    other = root / 'owners/local/other'
    own.mkdir(parents=True)
    (own / 'AGENTS.md').write_text('persona', encoding='utf-8')
    business = own / 'memory'
    policy = PathAccessPolicy.from_values(mode='full', agent_home_root=root,
        business_aggregate_roots=(business,), foreground_admin_full_access=grant)
    assert policy.check_write(root / 'config/agent_config.yaml').allowed is grant
    assert policy.check(own / 'workspace/runtime/services/gateway/gateway.pid').allowed is grant
    assert not policy.check_write(other / 'config/capability_config.yaml').allowed
    assert not policy.check(other / 'data/plugins/secret').allowed
    assert not policy.check(business / 'memory.jsonl').allowed
    assert not policy.check_write(own / 'AGENTS.md').allowed
    assert not policy.check_write(own / 'tasks/2026-11-16/task/data/pack_verification/proof.json').allowed


def test_selective_declared_sources_preserve_custom_masks(tmp_path):
    root = tmp_path / 'home'
    config = root / 'config'
    custom = root / 'private-custom'
    paths = retained_host_exclusive_paths((config, custom), root, foreground_admin_full_access=True)
    assert paths == (custom,)
    assert retained_host_exclusive_paths((config, custom), root) == (config, custom)


@pytest.mark.parametrize('system', ['Darwin', 'Linux'])
def test_sandbox_projection_keeps_other_owner_and_exact_task_rules(tmp_path, system):
    root = tmp_path / 'home'
    own = root / 'owners/local/main'
    other = root / 'owners/local/other'
    (own / 'data/plugins').mkdir(parents=True)
    (other / 'data/plugins').mkdir(parents=True)
    paths = host_sandbox_paths(root, system=system, foreground_admin_full_access=True)
    assert own / 'data/plugins' not in paths.hidden
    assert root / 'config' not in paths.readonly
    if system == 'Linux':
        assert other / 'data/plugins' in paths.hidden
    else:
        pattern = macos_hidden_pattern(root, foreground_admin_full_access=True)
        assert not re.search(pattern, str(own / 'data/plugins/file'))
        assert re.search(pattern, str(other / 'data/plugins/file'))
        assert any(re.search(item, str(other / 'config/file')) for item in paths.patterns)
        assert not any(re.search(item, str(own / 'config/file')) for item in paths.patterns)


def test_environment_scope_and_pty_key_are_per_invocation(tmp_path, monkeypatch):
    monkeypatch.setenv('MY_AGENT_HOSTING_GATEWAY_PID', '123')
    monkeypatch.setenv('SCHEMA_SECRET', 'redacted-value')
    normal = _subprocess_text_env(sandboxed=True, host_credential_env_names=frozenset({'SCHEMA_SECRET'}))
    elevated = _subprocess_text_env(sandboxed=True, host_credential_env_names=frozenset({'SCHEMA_SECRET'}), foreground_admin_full_access=True)
    assert normal['MY_AGENT_HOST_STATE_READ_ONLY'] == '1'
    assert 'MY_AGENT_HOST_STATE_READ_ONLY' not in elevated
    assert elevated['MY_AGENT_HOSTING_GATEWAY_PID'] == '123'
    assert 'SCHEMA_SECRET' not in normal and 'SCHEMA_SECRET' not in elevated
    normal_scope = _pty_access_scope(None, None)
    elevated_scope = _pty_access_scope(None, None, foreground_admin_full_access=True, host_exclusive_paths=(tmp_path,))
    assert normal_scope != elevated_scope
    assert elevated_scope != replace(elevated_scope, host_exclusive_paths=())
    assert SandboxHostPolicy().foreground_admin_full_access is False


def test_shared_registry_concurrent_grants_and_revocation(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from agent_py_agent.agent.tooling import registry as registry_module
    from agent_py_agent.tests.test_admin_full_access_mcp import mcp_case

    _, registry, _, _ = mcp_case(tmp_path)
    snapshot = registry.runtime_snapshot(run_id='foreground')
    barrier = Barrier(2)
    observed = []

    def capture(self, request):
        barrier.wait(timeout=5)
        observed.append(request.write_boundary['foreground_admin_full_access'])
        return request.write_boundary['foreground_admin_full_access']

    monkeypatch.setattr(registry_module.ToolExecutor, 'execute', capture)
    boundary = {'host_foreground_interactive': True, 'foreground_admin_full_access': True}
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(registry.execute_tool, object(), write_boundary=boundary, runtime_snapshot=snapshot)
        second = workers.submit(registry.execute_tool, object(), write_boundary={'host_foreground_interactive': False,
            'foreground_admin_full_access': True}, runtime_snapshot=snapshot)
        assert first.result() is True and second.result() is False
    assert sorted(observed) == [False, True]
    assert boundary['foreground_admin_full_access'] is True
    monkeypatch.setattr(registry_module.ToolExecutor, 'execute', lambda self, request: request.write_boundary['foreground_admin_full_access'])
    registry._construction_params = replace(registry._construction_params,
        permission_state_reader=lambda: {'is_admin': True, 'mode': 'auto', 'approval_mode': 'auto'})
    assert registry.execute_tool(object(), write_boundary=boundary, runtime_snapshot=snapshot) is False


def test_managed_background_private_transport_and_log_redaction(tmp_path):
    import json
    import os
    import sys
    import time

    from agent_py_agent.agent.settings.secret_ref import HostCredentialSnapshot
    from agent_py_agent.agent.tooling.background_process_launch import (
        BackgroundLaunchRequest,
        start_background_process,
    )
    from agent_py_agent.agent.tooling.host_output_redaction import HostOutputRedaction
    from agent_py_agent.agent.tooling.process_scope import ProcessAccessScope, ProcessExecutionScope
    from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore

    if os.name != 'posix':
        pytest.skip('private FD transport native lane requires POSIX')
    home = (tmp_path / 'home/owners/local/main').resolve()
    home.mkdir(parents=True)
    secret = 'opaque-configured-synthetic-value-42'
    script = tmp_path / 'producer.py'
    script.write_text("import os,time\nos.write(1,b'opaque-configured-');time.sleep(.02);os.write(1,b'synthetic-value-42\\n');os.write(2,b'opaque-configured-synthetic-value-42\\n')\n", encoding='utf-8')
    log = home / 'output.log'
    log.touch()
    store = home / 'process-sessions'
    env = dict(os.environ)
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
    launched = start_background_process(BackgroundLaunchRequest(argv=[sys.executable, str(script)], command='synthetic-producer',
        cwd=tmp_path, log_path=log, env=env, max_log_bytes=100000, store_root=store,
        access_scope=ProcessAccessScope('local/main', 'thread', str(home)),
        execution_scope=ProcessExecutionScope(owner_home=str(home)),
        host_output_redaction=HostOutputRedaction(snapshot=HostCredentialSnapshot(secret_values=(secret,)), force_default=True)))
    launched.process.wait(timeout=10)
    deadline = time.monotonic() + 3
    while 'secret_values' in log.read_text() and time.monotonic() < deadline:
        time.sleep(.01)
    text = log.read_text()
    assert secret not in text and '*' in text
    assert secret not in json.dumps(ProcessSessionStore(store).load(launched.record['session_id']).record)
    assert not list((store / '.launches').glob('*.json'))


@pytest.mark.parametrize('grant', [False, True])
def test_native_sandbox_selective_masks_and_cli_environment(tmp_path, grant):
    import os
    import shlex
    import shutil
    import subprocess
    import sys

    from agent_py_agent.agent.tooling.shell import _sandbox_exec

    if sys.platform == 'darwin' and not shutil.which('sandbox-exec'):
        pytest.skip('native macOS Seatbelt sandbox-exec unavailable')
    if sys.platform.startswith('linux') and not shutil.which('bwrap'):
        pytest.skip('native Linux bwrap unavailable')
    if not (sys.platform == 'darwin' or sys.platform.startswith('linux')):
        pytest.skip('native foreground sandbox lane only macOS/Linux')
    root = tmp_path / 'home'
    own = root / 'owners/local/main'
    own.mkdir(parents=True)
    config = root / 'config/agent_config.yaml'
    config.parent.mkdir(parents=True)
    config.write_text('nonsecret: visible\n')
    hidden = own / 'private-business'
    hidden.mkdir()
    (hidden / 'payload').write_text('must-stay-hidden')
    command = 'cat ' + shlex.quote(str(config)) + '; cat ' + shlex.quote(str(hidden / 'payload'))
    argv, use_shell = _sandbox_exec(command, own, None, own,
        host_policy=SandboxHostPolicy(host_exclusive_paths=(hidden,), foreground_admin_full_access=grant))
    result = subprocess.run(argv, shell=use_shell, cwd=own, capture_output=True, text=True,
        env=_subprocess_text_env(sandboxed=True, foreground_admin_full_access=grant), timeout=10)
    if 'sandbox-exec: sandbox_apply:' in result.stderr:
        pytest.skip('native sandbox_apply unavailable: ' + result.stderr.strip())
    assert ('nonsecret: visible' in result.stdout) is grant
    assert 'must-stay-hidden' not in result.stdout
    assert os.path.exists(config)


def test_native_elevated_pty_keeps_masks_and_redacts_before_cursor(tmp_path):
    import os
    import shlex
    import time

    from agent_py_agent.agent.settings.secret_ref import HostCredentialSnapshot
    from agent_py_agent.agent.tooling.host_output_redaction import HostOutputRedaction
    from agent_py_agent.agent.tooling.pty_sessions import PtyHostPolicy, PtySessionRegistry

    if os.name != 'posix':
        pytest.skip('native PTY requires POSIX')
    root = tmp_path / 'home'
    own = root / 'owners/local/main'
    own.mkdir(parents=True)
    config = root / 'config/agent_config.yaml'
    config.parent.mkdir(parents=True)
    config.write_text('nonsecret-visible\nopaque-host-reference-secret\n')
    hidden = own / 'business'
    hidden.mkdir()
    (hidden / 'file').write_text('business-hidden')
    registry = PtySessionRegistry()
    session = registry.start('cat ' + shlex.quote(str(config)) + '; cat ' + shlex.quote(str(hidden / 'file')),
        own, host_policy=PtyHostPolicy(protected_persona_root=own, foreground_admin_full_access=True,
            host_exclusive_paths=(hidden,), host_output_redaction=HostOutputRedaction(
                snapshot=HostCredentialSnapshot(secret_values=('opaque-host-reference-secret',)), force_default=True)))
    try:
        deadline = time.monotonic() + 5
        while not session.closed and time.monotonic() < deadline:
            time.sleep(.01)
        output, _, _ = session.read(0, 100000)
        assert b'nonsecret-visible' in output
        assert b'opaque-host-reference-secret' not in output
        assert b'business-hidden' not in output
        assert session.access_scope.foreground_admin_full_access is True
        assert str(hidden) in session.access_scope.host_exclusive_paths
        assert registry.get(session.session_id, replace(session.access_scope, foreground_admin_full_access=False)) is None
    finally:
        registry.clear()


@pytest.mark.parametrize('tool_name', ['run_command', 'terminal_session'])
@pytest.mark.parametrize('grant', [False, True])
def test_request_copy_selectively_removes_config_mask_before_sandbox(tmp_path, tool_name, grant):
    from agent_py_agent.agent.tooling.pty_sessions import TerminalSessionTool
    from agent_py_agent.agent.tooling.registry_invoke import (
        RegistryToolInvokeRequest,
        _request_local_tool_for_invocation,
    )
    from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

    root = tmp_path / 'home'
    own = root / 'owners/local/main'
    own.mkdir(parents=True)
    config = root / 'config'
    source = tmp_path / 'declared-secret-ref'
    custom = own / 'custom-hidden'
    shell = ShellTool(own, options=ShellToolOptions(path_access_mode='full', access_mode='full-access',
        protected_persona_root=str(own), host_exclusive_paths=(config, source, custom)))
    tool = TerminalSessionTool(shell) if tool_name == 'terminal_session' else shell
    boundary = {'foreground_admin_full_access': grant, 'host_foreground_config_sources': [str(source)],
        'host_foreground_custom_hidden_roots': [str(custom)]}
    request = RegistryToolInvokeRequest(tool_name=tool_name, arguments={}, tools={tool_name: tool},
        workspace_root=own, workspace_roots=[own], allowed_tools=None, write_boundary=boundary, path_access_mode='full')
    copied = _request_local_tool_for_invocation(tool, request=request, workspace_roots=[own])
    selected = getattr(copied, 'shell_tool', copied)
    assert selected is not shell
    assert (config in selected.host_exclusive_paths) is not grant
    assert (source in selected.host_exclusive_paths) is not grant
    assert custom in selected.host_exclusive_paths
    assert shell.host_exclusive_paths == (config, source, custom)
    assert selected.path_access_policy.foreground_admin_full_access is grant


def test_snapshot_reader_failure_is_partial_without_remasking_or_denial(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
        write_boundary_with_runtime_ledger,
    )
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig, secret_ref

    project = tmp_path / 'project'
    project.mkdir()
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home')),
        root=project)
    def broken(config):
        raise OSError('synthetic snapshot source reader failure')
    monkeypatch.setattr(secret_ref, 'configured_credential_snapshot', broken)
    boundary = write_boundary_with_runtime_ledger(agent, RunParams(host_foreground_interactive=True))
    redaction = boundary['_host_output_redaction']
    assert redaction.snapshot.coverage == 'partial'
    assert redaction.snapshot.snapshot_error_code == 'CREDENTIAL_SNAPSHOT_UNAVAILABLE'
    assert boundary['host_foreground_interactive'] is True
    assert boundary['foreground_admin_full_access'] is False
