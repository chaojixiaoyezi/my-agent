"""Windows 进程观察不发信号、不回收他人退出状态；原生行为由 Windows CI 验证。"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from agent.common import heartbeat


@pytest.fixture(autouse=True)
def reset_native_bindings():
    clear = heartbeat._windows_process_api.cache_clear
    clear()
    yield
    clear()


class NativeCall:
    def __init__(self, fn):
        self.fn = fn

    def __call__(self, *args):
        return self.fn(*args)


@pytest.mark.parametrize('result,expected', [(0, False), (258, True), (0xFFFFFFFF, None)])
def test_native_handle_is_pointer_width_and_closed(monkeypatch, result, expected):
    handle = (1 << 40) + 17
    closed = []
    kernel = SimpleNamespace(
        OpenProcess=NativeCall(lambda access, inherit, pid: handle if (access, inherit, pid) == (0x100000, False, 42) else None),
        WaitForSingleObject=NativeCall(lambda actual, timeout: result if (actual, timeout) == (handle, 0) else 123),
        CloseHandle=NativeCall(lambda actual: closed.append(actual) or 1),
    )
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **k: kernel, raising=False)
    assert heartbeat.windows_process_alive(42) is expected
    assert kernel.OpenProcess.restype is ctypes.c_void_p
    assert closed == [handle]


@pytest.mark.parametrize('error,expected', [(5, None), (87, False), (6, None)])
def test_open_failure_is_not_death_except_missing_pid(monkeypatch, error, expected):
    kernel = SimpleNamespace(OpenProcess=NativeCall(lambda *a: None), WaitForSingleObject=NativeCall(lambda *a: pytest.fail('wait without handle')), CloseHandle=NativeCall(lambda *a: pytest.fail('close without handle')))
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **k: kernel, raising=False)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: error, raising=False)
    assert heartbeat.windows_process_alive(42) is expected


def test_wait_exception_closes_original_handle(monkeypatch):
    closed = []
    def fail(*args):
        raise OSError('injected')
    kernel = SimpleNamespace(OpenProcess=NativeCall(lambda *a: 123), WaitForSingleObject=NativeCall(fail), CloseHandle=NativeCall(lambda h: closed.append(h) or 1))
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **k: kernel, raising=False)
    assert heartbeat.windows_process_alive(42) is None
    assert closed == [123]


@pytest.mark.parametrize('observed', [True, False, None])
def test_windows_consumers_never_use_kill_zero(monkeypatch, observed):
    from agent.gateway_parts import process_control as gateway
    from agent.subagents import process_control as subagent
    from agent.tooling import process_registry as registry
    monkeypatch.setattr(os, 'name', 'nt')
    monkeypatch.setattr(registry, '_IS_WINDOWS', True)
    monkeypatch.setattr(heartbeat, 'windows_process_alive', lambda pid: observed)
    monkeypatch.setattr(os, 'kill', lambda *a: pytest.fail('observation must not signal'))
    monkeypatch.setattr(registry, '_process_is_zombie', lambda pid: False)
    monkeypatch.setattr(registry, 'capture_process_birth_token', lambda pid: 'same')
    assert subagent.is_pid_alive(42) is (observed is not False)
    assert gateway.is_pid_alive(42) is (observed is not False)
    assert registry._pid_alive(42) is (observed is not False)
    assert registry._process_instance_terminated(42, 'same') is (observed is False)


@pytest.mark.skipif(sys.platform != 'win32', reason='真实 Win32 句柄合同由 Windows guard 执行')
def test_windows_observation_preserves_owned_live_child_and_exit_seven():
    from agent.subagents.process_control import is_pid_alive
    child = subprocess.Popen([sys.executable, '-c', 'import sys; print("ready",flush=True); sys.stdin.readline(); sys.exit(7)'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        for _ in range(20):
            assert heartbeat.windows_process_alive(child.pid) is True
            assert is_pid_alive(child.pid)
        assert child.poll() is None
    finally:
        child.communicate('\n', timeout=10)
    assert child.returncode == 7


@pytest.mark.parametrize('observed', [True, False, None])
def test_holder_scheduler_and_identity_consumers_preserve_unknown(monkeypatch, observed):
    from agent.adapter import delivery
    from agent.gateway_parts import daemon_metadata, multiprocess_control_identity
    from agent.local_storage import tool_operations
    from agent.runtime_db import managed_operation_store, operations
    from agent.scheduler import repository

    identity = {'host_id': 'host', 'pid': 42, 'start_time': 'birth'}
    monkeypatch.setattr(os, 'name', 'nt')
    monkeypatch.setattr(os, 'kill', lambda *a: pytest.fail('observation must not signal'))
    monkeypatch.setattr(heartbeat, 'windows_process_alive', lambda pid: observed)
    monkeypatch.setattr(daemon_metadata, 'process_host_id', lambda: 'host')
    monkeypatch.setattr(daemon_metadata, '_process_start_observation', lambda *a: 'birth')
    monkeypatch.setattr(multiprocess_control_identity, 'validate_record', lambda row: row)
    monkeypatch.setattr(multiprocess_control_identity, 'process_host_id', lambda: 'host')
    monkeypatch.setattr(multiprocess_control_identity, 'process_birth', lambda pid: 'birth')
    assert heartbeat.process_alive(42) is (observed is not False)
    assert tool_operations._pid_exists(42) is (observed is not False)
    assert managed_operation_store._holder_pid_alive({'pid': 42}) is (observed is not False)
    assert operations.holder_is_alive(42) is (observed is not False)
    assert repository._process_state(42) == ('dead' if observed is False else 'alive' if observed is True else 'unverifiable')
    assert daemon_metadata.process_identity_is_live(identity) is observed
    assert delivery._dispatch_owner_live(identity) is observed
    assert multiprocess_control_identity.control_executor_live({**identity, 'schema_version': 'multiprocess_process_identity.v1'}) is observed


def test_windows_instance_identity_reuse_requires_readable_birth(monkeypatch):
    from agent.tooling import process_registry as registry
    monkeypatch.setattr(registry, '_IS_WINDOWS', True)
    monkeypatch.setattr(heartbeat, 'windows_process_alive', lambda pid: True)
    monkeypatch.setattr(registry, '_process_is_zombie', lambda pid: False)
    monkeypatch.setattr(registry, 'capture_process_birth_token', lambda pid: '')
    assert registry._process_instance_terminated(42, 'original') is False
    monkeypatch.setattr(registry, 'capture_process_birth_token', lambda pid: 'replacement')
    monkeypatch.setattr(registry, '_birth_token_matches', lambda *args: False)
    assert registry._process_instance_terminated(42, 'original') is True


def test_posix_permission_denied_never_authorizes_holder_takeover(monkeypatch):
    from agent.gateway_parts import process_control
    from agent.runtime_db import operations
    monkeypatch.setattr(os, 'name', 'posix')
    def denied(*args):
        raise PermissionError(1, 'denied')
    monkeypatch.setattr(os, 'kill', denied)
    assert process_control.is_pid_alive(42) is True
    assert operations.holder_is_alive(42, 'original') is True


@pytest.mark.parametrize('close_result', [0, OSError('close failed')])
def test_close_failure_cannot_sign_process_dead(monkeypatch, close_result):
    def close(handle):
        if isinstance(close_result, OSError):
            raise close_result
        return close_result
    kernel = SimpleNamespace(OpenProcess=NativeCall(lambda *args: 123), WaitForSingleObject=NativeCall(lambda *args: 0), CloseHandle=NativeCall(close))
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **k: kernel, raising=False)
    assert heartbeat.windows_process_alive(42) is None


def test_pid_outside_dword_never_wraps_into_another_process(monkeypatch):
    monkeypatch.setattr(heartbeat, '_windows_process_api', lambda: pytest.fail('invalid PID must not open'))
    assert heartbeat.windows_process_alive(1 << 32) is None
