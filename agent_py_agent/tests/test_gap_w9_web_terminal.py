"""W9 的真实 PTY、原票据交换和隔离业务入口回归；不调用外部网络。"""
from __future__ import annotations

import base64
import hashlib
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.business_spaces import gap158_w9_web_terminal as terminal
from agent_py_agent.agent.business_spaces import v13_w12_oncall_push as tickets
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts import business_api_v13 as v13
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.tooling import pty_sessions as pty
from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner, resolve_owner_home


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    paths = home_paths(tmp_path / 'home')
    paths = home_paths_with_owner(paths, resolve_owner_home(paths.root))
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'project').mkdir()
    clock = [1794048000.0]
    host = SimpleNamespace(home_paths=paths, workspace=workspace,
        config=SimpleNamespace(business_spaces_enabled=True, web_terminal_enabled=True),
        business_v13_clock=lambda: clock[0])
    shell = ShellTool(workspace, options=ShellToolOptions(owner_scope_root=paths.owner_home_dir,
        protected_persona_root=paths.owner_home_dir))
    # 测试工作根由可信宿主显式授权，不由 grant JSON 产生墙外权限。
    shell.granted_external_roots = (workspace,)
    shell._sandbox_network_access = False
    host.tools = SimpleNamespace(tools={'run_command': shell})
    registry = terminal.WebTerminalRegistry(schedule=False)
    monkeypatch.setattr(terminal, '_registry', registry)
    # 此层检验授权和真实 PTY，不检验 OS 沙箱可用性；生产适配器始终保持失败关闭。
    # 明确替换单一外部边界，不能把本测试结果外推为原生沙箱端到端通过。
    monkeypatch.setattr(pty, '_sandbox_exec', lambda command, *args, **kwargs:
                        (['/bin/sh', '-c', command], False))
    yield SimpleNamespace(host=host, clock=clock, registry=registry, root=tmp_path)
    registry.clear()
    registry.backend.clear()


def call(env, action, params=None, *, subject='alice', key=None, host=None):
    host = env.host if host is None else host
    context = api.bind_business_context(host, subject_id=subject, authorize=lambda action, facts: True)
    services = api.BusinessServices(host)
    return V13Call(action, params or {}, uuid4().hex, services.owner_id, subject, context, services,
        services.owner_home, key or 'w9-' + uuid4().hex, clock=lambda: env.clock[0])


def grant(env, capabilities=('read', 'write', 'resize'), **kwargs):
    return terminal.grant(call(env, 'web_terminal.grant',
        {'workspace_root': 'project', 'capabilities': list(capabilities)}, **kwargs))


def opened(env, granted=None):
    granted = grant(env) if granted is None else granted
    result = terminal.open_session(call(env, 'web_terminal.open',
        {'ticket': granted['ticket'], 'cols': 90, 'rows': 25}))
    return granted, result


def params(granted, result, **extra):
    return {'ticket': granted['ticket'], 'session_id': result['session_id'], **extra}


def write(env, granted, result, text):
    return terminal.write(call(env, 'web_terminal.write', params(granted, result,
        data_b64=base64.b64encode(text.encode()).decode())))


def wait_for_read(env, granted, result, predicate):
    deadline = time.monotonic() + 5
    while True:
        value = terminal.read(call(env, 'web_terminal.read', params(granted, result, cursor=0)))
        if predicate(value):
            return value
        assert time.monotonic() < deadline, value
        threading.Event().wait(0.01)


def posix_only():
    if os.name == 'nt':
        pytest.skip('当前平台没有标准库 PTY 后端')


def test_real_pty_full_lifecycle_kernel_readback_and_exit_receipt(env):
    posix_only()
    granted, result = opened(env)
    assert (result['cols'], result['rows']) == (90, 25)
    resized = terminal.resize(call(env, 'web_terminal.resize', params(granted, result, cols=103, rows=31)))
    assert resized == {'cols': 103, 'rows': 31}
    written = write(env, granted, result, "stty size; printf 'W9_%s\\n' READY\n")
    assert written['accepted_bytes'] > 0
    value = wait_for_read(env, granted, result,
        lambda value: b'W9_READY' in base64.b64decode(value['data_b64'])
        and b'31 103' in base64.b64decode(value['data_b64']))
    assert value['overflow'] is False and value['exited'] is False
    assert value['next_cursor'] > 0
    empty = terminal.read(call(env, 'web_terminal.read', params(granted, result, cursor=value['next_cursor'])))
    assert empty['data_b64'] == '' and empty['next_cursor'] == value['next_cursor']
    write(env, granted, result, 'exit 7\n')
    value = wait_for_read(env, granted, result, lambda value: value['exited'])
    assert value['exit_code'] == 7
    closed = terminal.close(call(env, 'web_terminal.close', {'session_id': result['session_id']}))
    assert closed['closed'] is True and closed['receipt']['process_group_released'] is True
    assert closed['exit_code'] == 7
    assert terminal.list_sessions(call(env, 'web_terminal.list'))['items'][0]['exited'] is True


def test_ticket_exchanged_once_then_bound_calls_keep_working(env):
    posix_only()
    granted, result = opened(env)
    digest = hashlib.sha256(granted['ticket'].encode()).hexdigest()
    store = V13Store(env.host.home_paths.owner_home_dir, owner_id=env.host.home_paths.owner_id)
    assert store.get('realtime_tickets', digest)['used_at'] is not None
    for _ in range(2):
        assert terminal.read(call(env, 'web_terminal.read', params(granted, result, cursor=0)))['exited'] is False
    with pytest.raises(HostAPIError) as error:
        tickets.ticket_verify(call(env, 'realtime.ticket.verify', {'ticket': granted['ticket'], 'channel': 'web_terminal'}))
    assert error.value.code == 'VERIFICATION_FAILED'


@pytest.mark.parametrize('path', ['../escape', '/tmp', '-bad', 'project/../../escape', 'project\x00x', '.git'])
def test_invalid_paths_rejected_before_spawn(env, path):
    with pytest.raises(HostAPIError) as error:
        terminal.grant(call(env, 'web_terminal.grant', {'workspace_root': path, 'capabilities': ['read']}))
    assert error.value.code == 'PATH_OUTSIDE_WORKSPACE'
    assert env.registry.backend._sessions == {}


def test_symlink_escape_is_rejected(env):
    outside = env.root / 'outside'
    outside.mkdir()
    (env.host.workspace / 'link').symlink_to(outside, target_is_directory=True)
    with pytest.raises(HostAPIError) as error:
        terminal.grant(call(env, 'web_terminal.grant', {'workspace_root': 'link', 'capabilities': ['read']}))
    assert error.value.code == 'PATH_OUTSIDE_WORKSPACE'


def test_owner_subject_and_session_grant_isolation(env):
    posix_only()
    granted, result = opened(env)
    raw = home_paths(env.root / 'other')
    other = SimpleNamespace(home_paths=home_paths_with_owner(raw, resolve_owner_home(raw.root)),
        config=env.host.config, workspace=env.host.workspace)
    for kwargs in ({'host': other}, {'subject': 'bob'}):
        with pytest.raises(HostAPIError) as error:
            terminal.read(call(env, 'web_terminal.read', params(granted, result, cursor=0), **kwargs))
        assert error.value.code == 'VERIFICATION_FAILED'
        with pytest.raises(HostAPIError) as error:
            terminal.close(call(env, 'web_terminal.close', {'session_id': result['session_id']}, **kwargs))
        assert error.value.code == 'RESOURCE_NOT_FOUND'
        assert terminal.revoke(call(env, 'web_terminal.revoke', {'grant_id': granted['grant_id']}, **kwargs))['revoked'] is False
    another = grant(env)
    with pytest.raises(HostAPIError) as error:
        terminal.read(call(env, 'web_terminal.read', params(another, result, cursor=0)))
    assert error.value.code == 'RESOURCE_NOT_FOUND'


def test_capabilities_expiry_and_revoke_close_real_process(env):
    posix_only()
    granted, result = opened(env, grant(env, ('read',)))
    with pytest.raises(HostAPIError) as error:
        write(env, granted, result, 'printf nope\n')
    assert error.value.code == 'VERIFICATION_FAILED'
    session = env.registry.backend.get(result['session_id'])
    env.clock[0] = granted['expires_at']
    env.registry.sweep(env.clock[0])
    assert session.termination.confirmed and session.process.poll() is not None
    with pytest.raises(HostAPIError) as error:
        terminal.read(call(env, 'web_terminal.read', params(granted, result, cursor=0)))
    assert error.value.code == 'VERIFICATION_FAILED'
    granted, result = opened(env)
    session = env.registry.backend.get(result['session_id'])
    revoked = terminal.revoke(call(env, 'web_terminal.revoke', {'grant_id': granted['grant_id']}))
    assert revoked == {'revoked': True, 'closed_sessions': [result['session_id']]}
    assert session.termination.confirmed and session.closed
    assert terminal.revoke(call(env, 'web_terminal.revoke', {'grant_id': granted['grant_id']}))['revoked'] is False


def test_ring_overflow_reconnect_and_read_bound(env):
    posix_only()
    granted, result = opened(env)
    session = env.registry.backend.get(result['session_id'])
    session.append(b'z' * (pty._MAX_BUFFER_BYTES + 101))
    value = terminal.read(call(env, 'web_terminal.read', params(granted, result, cursor=0, max_bytes=17)))
    assert value['overflow'] is True and base64.b64decode(value['data_b64']) == b'z' * 17
    assert value['next_cursor'] == session.base_cursor + 17
    next_value = terminal.read(call(env, 'web_terminal.read', params(granted, result,
        cursor=value['next_cursor'], max_bytes=17)))
    assert next_value['overflow'] is False and next_value['next_cursor'] == value['next_cursor'] + 17
    for maximum in (0, 65537, True):
        with pytest.raises(HostAPIError) as error:
            terminal.read(call(env, 'web_terminal.read', params(granted, result, cursor=0, max_bytes=maximum)))
        assert error.value.code == 'INVALID_SCHEMA'


def test_session_limit_and_non_read_idempotency(env):
    posix_only()
    granted = grant(env)
    original = call(env, 'web_terminal.open', {'ticket': granted['ticket'], 'cols': 80, 'rows': 24})
    first = terminal.open_session(original)
    assert terminal.open_session(original) == first
    assert len(env.registry.backend._sessions) == 1
    with pytest.raises(HostAPIError) as error:
        terminal.open_session(replace(original, params={**original.params, 'cols': 99}))
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'
    for _ in range(3):
        opened(env, granted)
    with pytest.raises(HostAPIError) as error:
        opened(env, granted)
    assert error.value.code == 'LIMIT_EXCEEDED'
    data = base64.b64encode(b'printf W9_ONCE\n').decode()
    original = call(env, 'web_terminal.write', params(granted, first, data_b64=data))
    assert terminal.write(original) == terminal.write(original)
    assert terminal.grant(call(env, 'web_terminal.grant', {'workspace_root': 'project', 'capabilities': []}))['ticket']


def test_resize_adds_readback_without_replacing_original_session(env, monkeypatch):
    posix_only()
    import fcntl
    import struct
    import termios
    granted, result = opened(env)
    session = env.registry.backend.get(result['session_id'])
    original = fcntl.ioctl

    def distinct_readback(fd, request, *args):
        if request == termios.TIOCGWINSZ:
            return struct.pack('HHHH', 29, 97, 0, 0)
        return original(fd, request, *args)

    monkeypatch.setattr(fcntl, 'ioctl', distinct_readback)
    resized = env.registry.backend.resize(session.session_id, 101, 33, session.access_scope)
    assert resized is session and resized.resize_readback == (97, 29)
    assert terminal.resize(call(env, 'web_terminal.resize', params(granted, result, cols=100, rows=30))) == {'cols': 97, 'rows': 29}


def test_expiry_timer_closes_without_another_request(env, monkeypatch):
    posix_only()
    callbacks = []

    class ManualTimer:
        def __init__(self, interval, callback, args):
            self.interval, self.callback, self.args = interval, callback, args
            callbacks.append(self)
        def start(self):
            pass
        def cancel(self):
            pass

    monkeypatch.setattr(terminal.threading, 'Timer', ManualTimer)
    env.registry.schedule = True
    granted, result = opened(env)
    session = env.registry.backend.get(result['session_id'])
    assert callbacks[0].interval == granted['expires_at'] - env.clock[0]
    callbacks[0].callback(*callbacks[0].args)
    assert session.termination.confirmed and session.closed
    assert not env.registry.grants


class _ControlledBackend:
    """只控制外部进程边界，故障回执不借真实系统清理能力充数。"""

    def __init__(self):
        self.sessions, self.starts, self.writes, self.closes = {}, [], [], []
        self.confirm_close = True
        self.resize_error = False

    def start(self, *args, **kwargs):
        self.starts.append((args, kwargs))
        identifier = 'fake-' + str(len(self.starts))
        session = SimpleNamespace(session_id=identifier, access_scope=object(), closed=False,
            termination=None, process=SimpleNamespace(poll=lambda: None), resize_readback=None,
            started_at=0)
        self.sessions[identifier] = session
        return session

    def get(self, identifier, *args):
        return self.sessions.get(identifier)

    def resize(self, identifier, cols, rows, *args):
        if self.resize_error:
            raise OSError('受控尺寸故障')
        session = self.sessions[identifier]
        session.resize_readback = cols, rows
        return session

    def write(self, identifier, data, *args):
        self.writes.append(data)
        raise pty.PtyWriteError('TOOL_TIMEOUT', 3)

    def close(self, identifier, *args):
        self.closes.append(identifier)
        session = self.sessions[identifier]
        session.termination = SimpleNamespace(confirmed=self.confirm_close, return_code=0)
        session.closed = self.confirm_close
        return session

    def clear(self):
        for identifier in self.sessions:
            self.close(identifier)


def controlled(env):
    backend = _ControlledBackend()
    env.registry.backend = backend
    return backend


def test_partial_write_unknown_is_not_resubmitted_with_same_key(env):
    backend = controlled(env)
    granted, result = opened(env)
    request = call(env, 'web_terminal.write', params(granted, result,
        data_b64=base64.b64encode(b'printf once\n').decode()))
    for _ in range(2):
        with pytest.raises(HostAPIError) as error:
            terminal.write(request)
        assert error.value.code == 'OUTCOME_UNKNOWN' and error.value.outcome == 'unknown'
    assert len(backend.writes) == 1


def test_open_cleanup_unknown_cannot_start_another_process_on_replay(env):
    backend = controlled(env)
    backend.resize_error, backend.confirm_close = True, False
    granted = grant(env)
    request = call(env, 'web_terminal.open', {'ticket': granted['ticket'], 'cols': 80, 'rows': 24})
    for _ in range(2):
        with pytest.raises(HostAPIError) as error:
            terminal.open_session(request)
        assert error.value.code == 'OUTCOME_UNKNOWN'
    assert len(backend.starts) == 1
    assert len(env.registry.grants[granted['grant_id']].sessions) == 1
    backend.confirm_close = True


def test_close_same_key_can_finish_original_unconfirmed_cleanup(env):
    backend = controlled(env)
    granted, result = opened(env)
    request = call(env, 'web_terminal.close', {'session_id': result['session_id']})
    backend.confirm_close = False
    with pytest.raises(HostAPIError) as error:
        terminal.close(request)
    assert error.value.code == 'OUTCOME_UNKNOWN'
    backend.confirm_close = True
    assert terminal.close(request)['closed'] is True
    assert backend.closes == [result['session_id']] * 2


def test_expiry_unconfirmed_cleanup_schedules_retry_without_a_request(env, monkeypatch):
    backend, callbacks = controlled(env), []

    class Timer:
        def __init__(self, delay, callback, args):
            self.delay, self.callback, self.args = delay, callback, args
            callbacks.append(self)
        def start(self):
            pass
        def cancel(self):
            pass

    monkeypatch.setattr(terminal.threading, 'Timer', Timer)
    env.registry.schedule = True
    granted, result = opened(env)
    backend.confirm_close = False
    callbacks[0].callback(*callbacks[0].args)
    assert len(callbacks) == 2 and 0 < callbacks[1].delay <= 30
    assert granted['grant_id'] in env.registry.grants
    backend.confirm_close = True
    callbacks[1].callback(*callbacks[1].args)
    assert granted['grant_id'] not in env.registry.grants


def test_missing_trusted_shell_fails_before_process_start(env):
    backend = controlled(env)
    env.host.tools = None
    with pytest.raises(HostAPIError) as error:
        opened(env)
    assert error.value.code == 'HOST_NOT_READY'
    assert backend.starts == []


def test_start_preserves_trusted_shell_restrictions(env):
    backend = controlled(env)
    shell = env.host.tools.tools['run_command']
    private = env.root / 'private'
    shell.host_private_roots = (str(private),)
    shell._sandbox_forbidden_write_roots = (env.host.workspace / 'project' / 'readonly',)
    opened(env)
    args, kwargs = backend.starts[0]
    assert args[3] == args[4] == (env.host.workspace / 'project',)
    assert args[5].denied_roots == shell._sandbox_forbidden_write_roots
    assert kwargs['host_policy'].network_access is False
    assert kwargs['host_policy'].private_roots == (str(private),)


@pytest.fixture
def registered_slice(monkeypatch):
    # 只在隔离测试进程模拟 W0 追加静态切片；不修改生产注册表文件。
    name = 'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w9'
    modules = tuple(dict.fromkeys((*v13.V13_SLICE_MODULES, name)))
    tables = v13._load_registry(modules)
    for field, value in zip(('_ACTIONS', 'ALIASES', 'V13_DEFS', 'JOB_KINDS', 'COLLECTIONS'), tables):
        monkeypatch.setattr(v13, field, value)


def invoke(env, action, payload=None, *, expected=None, key=None):
    context = api.bind_business_context(env.host, subject_id='alice', authorize=lambda *args: True)
    request = {'action': action, 'request_id': uuid4().hex, 'params': payload or {},
               'idempotency_key': key or 'w9-' + uuid4().hex}
    if expected is not None:
        request['expected_revision'] = expected
    return api.execute_business(request, context, api.BusinessServices(env.host))


def test_native_dispatcher_envelopes_readiness_and_dto(env, registered_slice, monkeypatch):
    from agent_py_agent.agent.business_spaces import gap158_w9_collab_presence as presence
    monkeypatch.setattr(presence, '_presence', presence._PresenceRegistry())
    backend = controlled(env)
    env.host.config.web_terminal_enabled = False
    status, body = invoke(env, 'web_terminal.grant', {'workspace_root': 'project', 'capabilities': ['read']})
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY'
    assert invoke(env, 'web_terminal.list')[1]['data'] == {'items': []}
    env.host.config.web_terminal_enabled = True
    status, body = invoke(env, 'web_terminal.grant', {'workspace_root': 'project', 'capabilities': ['read']})
    assert status == 200 and body['state'] == 'completed' and body['error'] is None
    granted = body['data']
    status, body = invoke(env, 'web_terminal.open', {'ticket': granted['ticket'], 'cols': 88, 'rows': 27})
    assert status == 200 and body['data']['cols'] == 88 and body['data']['rows'] == 27
    identifier = body['data']['session_id']
    status, body = invoke(env, 'web_terminal.close', {'session_id': identifier})
    assert status == 200 and body['data']['closed'] is True
    assert backend.sessions[identifier].termination.confirmed
    assert invoke(env, 'collab_presence.join', {'doc_id': 'doc', 'member_id': 'driver', 'role': 'driver'})[0] == 200
    proposal = {'doc_id': 'doc', 'member_id': 'driver', 'state': {'x': [True, None, '正文']}}
    status, body = invoke(env, 'collab_presence.doc_state.propose', proposal, expected=0)
    assert status == 200 and body['data'] == {'doc_id': 'doc', 'revision': 1}
    status, body = invoke(env, 'collab_presence.doc_state.get', {'doc_id': 'doc'})
    assert status == 200 and body['data']['state'] == proposal['state'] and body['data']['revision'] == 1
    status, body = invoke(env, 'collab_presence.doc_state.propose', proposal, expected=0)
    assert status == 409 and body['error']['code'] == 'REVISION_CONFLICT'


def test_failed_new_open_does_not_close_older_session(env):
    backend = controlled(env)
    granted, first = opened(env)
    backend.resize_error = True
    with pytest.raises(HostAPIError) as error:
        opened(env, granted)
    assert error.value.code == 'EXTERNAL_PROCESS_FAILED'
    assert backend.sessions[first['session_id']].closed is False
    assert backend.closes == ['fake-2']
