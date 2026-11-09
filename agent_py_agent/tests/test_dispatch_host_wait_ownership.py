"""判活不抢走原Popen的waitstatus；诊断缓存不能补成原宿主的实际wait返回值。"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.background import dispatch
from agent_py_agent.agent.gateway_parts import multiprocess_drain_process as native_wait
from agent_py_agent.agent.subagents import process_control
from agent_py_agent.agent.tooling import process_registry


@contextmanager
def child():
    process = subprocess.Popen([sys.executable, '-c', 'import sys;sys.stdin.buffer.read(1);sys.exit(7)'],
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        yield process
    finally:
        if not process.stdin.closed:
            process.stdin.close()
        process.wait(timeout=3)


def wait_exit_event(pid):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if native_wait._original_waitid(pid, nowait=True) is not None:
            return
        threading.Event().wait(.001)
    pytest.fail('测试自有child没有产生内核退出事件')


@pytest.mark.skipif(sys.platform not in ('linux', 'darwin'), reason='原公开WNOWAIT ABI的平台边界')
def test_liveness_does_not_steal_original_child_exit_seven():
    with child() as process:
        process.stdin.close()
        wait_exit_event(process.pid)
        assert not process_control.is_pid_alive(process.pid)
        assert process.wait(timeout=2) == 7


@pytest.mark.skipif(sys.platform not in ('linux', 'darwin'), reason='原公开WNOWAIT ABI的平台边界')
def test_event_interleaved_liveness_preserves_the_original_reaper_status():
    with child() as process:
        entered, proceed = threading.Event(), threading.Event()
        results = []

        def reap():
            entered.set()
            assert proceed.wait(2)
            results.append(process.wait(timeout=2))

        thread = threading.Thread(target=reap)
        thread.start()
        try:
            assert entered.wait(2)
            process.stdin.close()
            wait_exit_event(process.pid)
            for _ in range(3):
                assert not process_control.is_pid_alive(process.pid)
        finally:
            proceed.set()
            thread.join(3)
        assert not thread.is_alive() and results == [7]


@pytest.mark.parametrize('platform', ['linux', 'darwin', 'win32'])
def test_liveness_never_attempts_waitpid_for_borrowed_pid(monkeypatch, platform):
    calls = []
    monkeypatch.setattr(process_control, 'sys', SimpleNamespace(platform=platform))
    monkeypatch.setattr(process_control, '_linux_process_is_zombie', lambda pid: False)
    monkeypatch.setattr(process_registry, '_process_is_zombie', lambda pid: False)
    monkeypatch.setattr(process_control.os, 'kill', lambda pid, signal: calls.append(('probe', pid, signal)))

    def unexpected_wait(*args):
        calls.append(('wait', *args))
        return args[0], 0

    monkeypatch.setattr(process_control.os, 'waitpid', unexpected_wait, raising=False)
    assert process_control.is_pid_alive(987654321)
    assert calls == [('probe', 987654321, 0)]


@pytest.mark.parametrize('error', [PermissionError(), ProcessLookupError()])
def test_read_only_liveness_keeps_original_permission_and_gone_boundaries(monkeypatch, error):
    monkeypatch.setattr(process_control, '_linux_process_is_zombie', lambda pid: False)
    monkeypatch.setattr(process_registry, '_process_is_zombie', lambda pid: False)

    def probe(pid, signum):
        assert signum == 0
        raise error

    monkeypatch.setattr(process_control.os, 'kill', probe)
    assert process_control.is_pid_alive(987654321) is isinstance(error, PermissionError)


@pytest.mark.parametrize('cached', [0, 19])
def test_wait_error_cannot_be_filled_from_popen_cache(tmp_path, monkeypatch, cached):
    logs, settled = [], []
    from agent_py_agent.agent.agent_core.orchestration.background import log_retention

    def wait():
        raise OSError('synthetic wait failure')

    process = SimpleNamespace(pid=987654321, returncode=cached, wait=wait,
                              _my_agent_background_log_identity=(1, 2))
    agent = SimpleNamespace(root=tmp_path, _background_subagent_dispatches={}, subagents=SimpleNamespace(
        runtime_db=SimpleNamespace(get_attempt=lambda attempt: {'status': 'pending'})))
    request = dispatch._BackgroundDispatchRequest(agent, ['run'], 'launch', None, None,
                                                   SimpleNamespace(expected_attempt_ids={'run': 'attempt'}))
    monkeypatch.setattr(log_retention, 'record_dispatch_log_exit', lambda *args, **kwargs: logs.append(kwargs))
    monkeypatch.setattr(log_retention, 'retire_dispatch_logs_slice', lambda *args: None)
    monkeypatch.setattr(dispatch, '_background_log_path', lambda *args: '/tmp/unused-synthetic.log')
    monkeypatch.setattr(dispatch, '_background_process_startup_failure', settled.append)
    dispatch._reap_then_settle_unactivated_exit(agent, request, process, (('run', 'attempt'),))
    recorded = agent._background_subagent_dispatches['launch']
    assert recorded['returncode'] is None and recorded['status'] == 'failed'
    assert 'OSError' in recorded['reap_error']
    assert logs == [] and settled == []


@pytest.mark.parametrize('value', [True, 7.5, '7', None])
def test_reaper_rejects_non_integer_wait_result_without_coercion(value):
    process = SimpleNamespace(wait=lambda: value)
    agent = SimpleNamespace(_background_subagent_dispatches={})
    assert dispatch._reap_background_process(agent, 'launch', process) is None
    recorded = agent._background_subagent_dispatches['launch']
    assert recorded['returncode'] is None and recorded['reap_error']


def test_reaper_returns_wait_result_without_a_registry_or_cached_code():
    process = SimpleNamespace(wait=lambda: 7, returncode=19)
    assert dispatch._reap_background_process(SimpleNamespace(), 'launch', process) == 7
