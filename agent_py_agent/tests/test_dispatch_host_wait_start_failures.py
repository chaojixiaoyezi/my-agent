"""只有原创建者等待实际child；启动异常不能丢Popen或另造成功。"""
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.background import dispatch


def case(tmp_path, monkeypatch):
    agent = SimpleNamespace(root=tmp_path / 'owner', config=SimpleNamespace(background_dispatch_log_max_bytes=0),
                            subagents=SimpleNamespace(workspace=tmp_path),
                            _background_subagent_dispatches={})
    request = dispatch._BackgroundDispatchRequest(agent, [], 'isolated-wait', None, None,
                                                 SimpleNamespace(expected_attempt_ids={}))
    monkeypatch.setattr(dispatch, '_background_dispatch_command', lambda *_: [sys.executable, '-c', 'raise SystemExit(7)'])
    real_popen = subprocess.Popen
    captured, waits = [], []
    waited = threading.Event()

    def spawn(*args, **options):
        process = real_popen(*args, **options)
        original = process.wait

        def wait(*args, **options):
            result = original(*args, **options)
            waits.append(result)
            waited.set()
            return result

        process.wait = wait
        captured.append((process, original))
        return process

    monkeypatch.setattr(dispatch.subprocess, 'Popen', spawn)
    return agent, request, captured, waits, waited


def spawn_owned(agent, request):
    with dispatch._BackgroundProcessWaitOwner(agent, request) as owner:
        return dispatch._spawn_background_dispatch_process(agent, request, wait_owner=owner)


def cleanup(captured):
    for process, original in captured:
        if process.returncode is None:
            original(timeout=5)


@pytest.mark.parametrize('close_fails', [False, True])
def test_successful_popen_metadata_error_still_has_original_wait_owner(tmp_path, monkeypatch, close_fails):
    agent, request, captured, waits, waited = case(tmp_path, monkeypatch)
    primary = OSError('isolated-after-popen')
    original_stat = dispatch.os.fstat

    def fail_after_spawn(fd):
        if captured:
            raise primary
        return original_stat(fd)

    monkeypatch.setattr(dispatch, 'os', SimpleNamespace(**{**vars(dispatch.os), 'fstat': fail_after_spawn}))
    if close_fails:
        import builtins
        original_open = builtins.open

        class FailingClose:
            def __init__(self, raw):
                self.raw = raw

            def fileno(self):
                return self.raw.fileno()

            def close(self):
                self.raw.close()
                raise RuntimeError('isolated-secondary-close')

        monkeypatch.setattr(dispatch, 'open', lambda *a, **k: FailingClose(original_open(*a, **k)), raising=False)
    try:
        with pytest.raises(OSError) as caught:
            spawn_owned(agent, request)
        assert caught.value is primary
        assert waited.wait(1), '成功Popen后的异常仍须由原拥有者wait，不能依赖下次spawn或GC'
        assert waits == [7] and captured[0][0].returncode == 7
    finally:
        cleanup(captured)


def test_reaper_thread_start_failure_happens_before_any_popen(tmp_path, monkeypatch):
    agent, request, captured, _waits, _waited = case(tmp_path, monkeypatch)
    primary = RuntimeError('isolated-thread-start')

    def fail_start(_thread):
        raise primary

    monkeypatch.setattr(dispatch.threading.Thread, 'start', fail_start)
    try:
        with pytest.raises(RuntimeError) as caught:
            spawn_owned(agent, request)
        assert caught.value is primary and captured == []
    finally:
        cleanup(captured)


def test_quick_exit_poll_before_owner_wait_keeps_exact_seven_and_registration_order(tmp_path, monkeypatch):
    import time

    agent, request, captured, waits, waited = case(tmp_path, monkeypatch)
    with dispatch._BackgroundProcessWaitOwner(agent, request) as owner:
        process = dispatch._spawn_background_dispatch_process(agent, request, wait_owner=owner)
        dispatch._remember_background_dispatch(agent, request.launch_id, [], f'pid:{process.pid}')
        deadline = time.monotonic() + 5
        while process.poll() is None:
            assert time.monotonic() < deadline
            threading.Event().wait(0.01)
        assert process.returncode == 7
        assert not waited.is_set() and waits == []
        assert agent._background_subagent_dispatches[request.launch_id]['status'] == 'running'
    owner.thread.join(5)
    assert not owner.thread.is_alive() and waits == [7]
    recorded = agent._background_subagent_dispatches[request.launch_id]
    assert recorded['status'] == 'failed' and recorded['returncode'] == 7
    assert owner._agent is owner._request is owner._process is None
    assert captured[0][0] is process


@pytest.mark.parametrize('failure_stage', ['command', 'popen'])
def test_popen_failure_releases_reserved_thread_and_preserves_primary(tmp_path, monkeypatch, failure_stage):
    agent, request, captured, _waits, _waited = case(tmp_path, monkeypatch)
    primary = OSError('isolated-popen-failure')
    threads = []
    real_thread = threading.Thread

    def remember_thread(**options):
        thread = real_thread(**options)
        threads.append(thread)
        return thread

    def fail_spawn(*_args, **_options):
        raise primary

    monkeypatch.setattr(dispatch.threading, 'Thread', remember_thread)
    if failure_stage == 'command':
        monkeypatch.setattr(dispatch, '_background_dispatch_command', fail_spawn)
    else:
        monkeypatch.setattr(dispatch.subprocess, 'Popen', fail_spawn)
    with pytest.raises(OSError) as caught:
        spawn_owned(agent, request)
    assert caught.value is primary and captured == [] and len(threads) == 1
    threads[0].join(1)
    assert not threads[0].is_alive()


def test_thread_start_that_started_then_raised_exits_without_spawning(tmp_path, monkeypatch):
    agent, request, captured, _waits, _waited = case(tmp_path, monkeypatch)
    primary = RuntimeError('isolated-after-native-thread-start')
    real_start = threading.Thread.start
    threads = []

    def fail_after_start(thread):
        real_start(thread)
        threads.append(thread)
        raise primary

    monkeypatch.setattr(dispatch.threading.Thread, 'start', fail_after_start)
    with pytest.raises(RuntimeError) as caught:
        spawn_owned(agent, request)
    assert caught.value is primary and captured == [] and len(threads) == 1
    threads[0].join(1)
    assert not threads[0].is_alive()


@pytest.mark.parametrize('stage', ['marker', 'poll', 'registry'])
def test_post_spawn_setup_failure_reaps_without_replacing_original_error(tmp_path, monkeypatch, stage):
    from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchParams
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    agent, _request, captured, waits, waited = case(tmp_path, monkeypatch)
    agent.subagents = SubAgentManager(tmp_path / 'runs', owner_home_dir=str(tmp_path / 'owner'))
    task = agent.subagents.create_run(goal='isolated-startup-lifetime')
    primary = OSError('isolated-startup-' + stage)
    owners = []
    original_spawn = dispatch._spawn_background_dispatch_process
    original_mark = dispatch.mark_background_start

    def observe_spawn(*args, **options):
        owners.append(options['wait_owner'])
        return original_spawn(*args, **options)

    def fail(*_args, **_options):
        assert not waited.is_set(), 'wait不得越过尚未完成的原登记顺序'
        raise primary

    def mark(request, *, status, **options):
        if status == 'running' and stage == 'marker':
            return fail()
        return original_mark(request, status=status, **options)

    monkeypatch.setattr(dispatch, '_auto_start_dispatch_args', lambda *_a, **_k: (object(), object(), DispatchParams()))
    monkeypatch.setattr(dispatch, '_spawn_background_dispatch_process', observe_spawn)
    monkeypatch.setattr(dispatch, 'mark_background_start', mark)
    if stage == 'poll':
        monkeypatch.setattr(dispatch, 'process_startup_returncode', fail)
    elif stage == 'registry':
        monkeypatch.setattr(dispatch, 'process_startup_returncode', lambda _process: None)
        monkeypatch.setattr(dispatch, '_remember_background_dispatch', fail)
    try:
        with pytest.raises(OSError) as caught:
            dispatch._start_background_dispatch(agent, [task.id])
        assert caught.value is primary and waited.wait(5)
        owners[0].thread.join(5)
        assert not owners[0].thread.is_alive() and waits == [7]
        assert captured[0][0].returncode == 7
        assert agent.subagents.load(task.id).status == 'CHANNEL_ERROR'
    finally:
        cleanup(captured)


def test_log_factory_has_no_unowned_fdopen_handoff(tmp_path, monkeypatch):
    import builtins

    agent, request, captured, waits, _waited = case(tmp_path, monkeypatch)
    real_open, native_open, native_close = builtins.open, dispatch.os.open, dispatch.os.close
    descriptors, opened = [], []

    def record_descriptor(*args, **options):
        descriptor = native_open(*args, **options)
        descriptors.append(descriptor)
        return descriptor

    def forbidden_fdopen(*_args, **_options):
        raise AssertionError('禁止裸descriptor向fdopen移交的异常窗口')

    def record_file(*args, **options):
        handle = real_open(*args, **options)
        opened.append(handle)
        return handle

    monkeypatch.setattr(dispatch, 'os', SimpleNamespace(**{**vars(dispatch.os), 'open': record_descriptor, 'fdopen': forbidden_fdopen}))
    monkeypatch.setattr(dispatch, 'open', record_file, raising=False)
    try:
        with dispatch._BackgroundProcessWaitOwner(agent, request) as owner:
            process = dispatch._spawn_background_dispatch_process(agent, request, wait_owner=owner)
        owner.thread.join(5)
        assert not owner.thread.is_alive() and waits == [7] and captured[0][0] is process
        assert len(opened) == len(descriptors) == 1 and opened[0].closed
        with pytest.raises(ValueError):
            opened[0].fileno()
    finally:
        cleanup(captured)
        if not opened:
            # 旧实现红例的fdopen被本测试明确拒绝，原裸FD仍由测试拥有；不靠GC掩盖泄漏。
            for descriptor in descriptors:
                native_close(descriptor)
