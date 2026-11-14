"""跨类型物理额度：同一OS权威、响应生命周期、跨进程退出与原配置语义。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import request_scope
from agent_py_agent.agent.backends.errors import ProviderQueueTimeoutError
from agent_py_agent.agent.llm_scale import gateway_transport_admission as gate


def request(local=0):
    return SimpleNamespace(api_base='https://synthetic.invalid/v1', provider_max_concurrent_requests=local)


def test_zero_preserves_no_files_and_original_local_limit(tmp_path, monkeypatch):
    monkeypatch.delenv(gate._BINDING_ENV, raising=False)
    with gate.gateway_transport_binding(tmp_path, 0):
        assert not gate.configured()
        assert request_scope.acquire_provider_request(request()) is None
        lease = request_scope.acquire_provider_request(request(1))
        assert request_scope.provider_endpoint_counts(request(1)) == (1, 0)
        lease.release()
    assert list(tmp_path.iterdir()) == []


def test_capacity_same_pid_threads_response_release_and_parent_wait(tmp_path):
    with gate.gateway_transport_binding(tmp_path, 2):
        # 外层父逻辑不申请；只有实际两条运输申请，第三条可停止等待。
        first = request_scope.acquire_provider_request(request())
        second = request_scope.acquire_provider_request(request())
        assert gate.counts() == {'enabled': True, 'limit': 2, 'active': 2}
        with pytest.raises(ProviderQueueTimeoutError):
            gate.acquire(time.monotonic() + .03)
        ready, exit_child = threading.Event(), threading.Event()
        result = []

        def child():
            lease = request_scope.acquire_provider_request(request())
            result.append(gate.counts()['active'])
            ready.set()
            exit_child.wait(2)
            lease.release()

        worker = threading.Thread(target=child)
        worker.start()
        assert not ready.wait(.05)
        # 父的真实运输返回即释放；它继续等子时不持槽，无需whole-task让出协议。
        first.release()
        assert ready.wait(2)
        assert result == [2]
        response = SimpleNamespace(_provider_request_lease=second)
        request_scope.release_provider_response(response)
        request_scope.release_provider_response(response)
        assert gate.counts()['active'] == 1
        exit_child.set()
        worker.join(2)
        assert not worker.is_alive()
        assert gate.counts()['active'] == 0


def test_same_directory_restart_and_capacity_switch_cannot_bypass_live_slot(tmp_path):
    with gate.gateway_transport_binding(tmp_path, 2):
        first = gate.acquire(time.monotonic() + 1)
        with gate.gateway_transport_binding(tmp_path, 2):
            assert gate.counts()['active'] == 1
            second = gate.acquire(time.monotonic() + 1)
            with pytest.raises(ProviderQueueTimeoutError):
                gate.acquire(time.monotonic() + .02)
            second.release()
        with pytest.raises(RuntimeError, match='GATEWAY_LLM_CAPACITY_BUSY'):
            with gate.gateway_transport_binding(tmp_path, 1):
                pytest.fail('live capacity switch admitted')
        assert gate.counts()['limit'] == 2
        first.release()
        with gate.gateway_transport_binding(tmp_path, 1):
            assert gate.counts() == {'enabled': True, 'limit': 1, 'active': 0}
        # 旧绑定重新申请时读取同一新容量，不按旧env内数字放大。
        assert gate.counts()['limit'] == 1
        files = sorted(p.name for p in (tmp_path / 'llm_transport_capacity').iterdir())
        for _ in range(20):
            lease = gate.acquire(time.monotonic() + 1)
            lease.release()
        assert sorted(p.name for p in (tmp_path / 'llm_transport_capacity').iterdir()) == files


def test_process_crash_returns_os_slot_without_pid_guess_or_history_scan(tmp_path):
    with gate.gateway_transport_binding(tmp_path, 2):
        code = '''import time
from agent_py_agent.agent.llm_scale import gateway_transport_admission as g
lease=g.acquire(time.monotonic()+2)
print('ACQUIRED',flush=True)
time.sleep(60)
'''
        with subprocess.Popen([sys.executable, '-u', '-c', code], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=dict(os.environ)) as child:
            try:
                assert child.stdout.readline().strip() == 'ACQUIRED'
                parent = gate.acquire(time.monotonic() + 1)
                assert gate.counts()['active'] == 2
                child.kill()
                child.wait(3)
                assert gate.counts()['active'] == 1
                recovered = gate.acquire(time.monotonic() + 1)
                assert gate.counts()['active'] == 2
                recovered.release()
                parent.release()
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(3)
        assert gate.counts()['active'] == 0


def test_gateway_isolation_and_fail_closed_corrupt_resource(tmp_path):
    with gate.gateway_transport_binding(tmp_path / 'a', 1):
        first = gate.acquire(time.monotonic() + 1)
        with gate.gateway_transport_binding(tmp_path / 'b', 1):
            assert gate.counts()['active'] == 0
            second = gate.acquire(time.monotonic() + 1)
            second.release()
        assert gate.counts()['active'] == 1
        first.release()
        root = Path(os.environ[gate._BINDING_ENV])
        (root / 'capacity.json').write_text(json.dumps({'schema': 'wrong'}))
        with pytest.raises(ValueError, match='GATEWAY_LLM_CAPACITY_INVALID'):
            gate.acquire(time.monotonic() + 1)


def test_config_host_only_and_normalization():
    from agent_py_agent.agent.settings.config import AgentConfig, normalize_agent_config
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry
    assert AgentConfig().gateway_llm_inflight_limit == 0
    normalized, warnings = normalize_agent_config({'gateway_llm_inflight_limit': 2})
    assert not warnings and normalized['gateway_llm_inflight_limit'] == 2
    assert not parameter_registry()['gateway_llm_inflight_limit'].writable


def test_shared_only_gate_precedes_physical_attempt_and_failure_releases(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends import gateway_helpers as http
    seen = []

    def attempt(req, number, last, *, provider_lease=None):
        seen.append(gate.counts()['active'])
        assert provider_lease is not None
        raise OSError('synthetic connection failure')

    monkeypatch.setattr(http, '_gateway_request_attempt', attempt)
    req = SimpleNamespace(provider_max_concurrent_requests=0, deadline=None,
                          max_retries=0, retry_backoff_seconds=())
    with gate.gateway_transport_binding(tmp_path, 1):
        with pytest.raises(OSError, match='synthetic connection failure'):
            http._open_gateway_request(req)
        assert seen == [1]
        assert gate.counts()['active'] == 0


def test_cancellation_while_waiting_keeps_original_two_leases(tmp_path, monkeypatch):
    from agent_py_agent.agent.concurrency import interrupt
    stopped = threading.Event()
    monkeypatch.setattr(interrupt, 'is_interrupted', stopped.is_set)
    with gate.gateway_transport_binding(tmp_path, 2):
        first = gate.acquire(time.monotonic() + 1)
        second = gate.acquire(time.monotonic() + 1)
        errors, entered = [], threading.Event()

        def waiter():
            entered.set()
            try:
                gate.acquire(time.monotonic() + 2)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=waiter)
        thread.start()
        assert entered.wait(1)
        stopped.set()
        thread.join(1)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], InterruptedError)
        assert gate.counts()['active'] == 2
        first.release()
        second.release()
        assert gate.counts()['active'] == 0


def test_disable_requires_drain_then_new_calls_keep_zero_path(tmp_path):
    with gate.gateway_transport_binding(tmp_path, 1):
        lease = gate.acquire(time.monotonic() + 1)
        with pytest.raises(RuntimeError, match='GATEWAY_LLM_CAPACITY_BUSY'):
            with gate.gateway_transport_binding(tmp_path, 0):
                pass
        lease.release()
        with gate.gateway_transport_binding(tmp_path, 0):
            assert not gate.configured()
            assert request_scope.acquire_provider_request(request()) is None
        # 老子进程保留的资源定位也只会读到正式关闭记录，不留旧cap。
        assert gate.counts() == {'enabled': False, 'limit': 0, 'active': 0}


def test_no_os_lock_cannot_silently_claim_tree_capacity(tmp_path, monkeypatch):
    from agent_py_agent.agent.common import json_io
    monkeypatch.setattr(json_io, 'fcntl', None)
    monkeypatch.setattr(json_io, 'msvcrt', None)
    with pytest.raises(RuntimeError, match='GATEWAY_LLM_OS_LOCK_UNAVAILABLE'):
        with gate.gateway_transport_binding(tmp_path, 2):
            pass
    assert not list(tmp_path.iterdir())


def test_high_capacity_round_has_constant_probes_and_cached_config_outside_lock(tmp_path, monkeypatch):
    from contextlib import ExitStack, contextmanager
    with gate.gateway_transport_binding(tmp_path, 500):
        root = Path(os.environ[gate._BINDING_ENV])
        with ExitStack() as held:
            for index in range(500):
                held.enter_context(gate._locked_file_path(root / f'slot-{index}', blocking=False))
            original_slot, original_guard, original_record = gate._locked_file_path, gate.locked_json_path, gate._record
            probes, reads = [], []
            in_guard = False

            @contextmanager
            def guard(*args, **kwargs):
                nonlocal in_guard
                with original_guard(*args, **kwargs):
                    in_guard = True
                    try:
                        yield
                    finally:
                        in_guard = False

            def record(root):
                assert not in_guard, '热路径不得在配置锁内解析JSON'
                reads.append(root)
                return original_record(root)

            def slot(path, **kwargs):
                probes.append(path.name)
                return original_slot(path, **kwargs)

            monkeypatch.setattr(gate, 'locked_json_path', guard)
            monkeypatch.setattr(gate, '_locked_file_path', slot)
            monkeypatch.setattr(gate, '_record', record)
            cursor = gate._ProbeCursor(next_index=498)
            for _ in range(125):
                before = len(probes)
                assert gate._try_slot(root, cursor) is None
                assert len(probes) - before <= 4
            assert len(probes) == 500
            assert len(set(probes)) == 500
            assert len(reads) == 1


def test_rotating_cursor_discovers_persistent_free_tail_without_new_waiter_reset(tmp_path):
    from contextlib import ExitStack
    with gate.gateway_transport_binding(tmp_path, 101):
        root = Path(os.environ[gate._BINDING_ENV])
        with ExitStack() as held:
            for index in range(100):
                held.enter_context(gate._locked_file_path(root / f'slot-{index}', blocking=False))
            cursor = gate._ProbeCursor(next_index=0)
            lease = None
            for round_number in range(26):
                # 新等待者的起点分配不得重置已有等待者的进度。
                list(gate._ProbeCursor().indices(101))
                lease = gate._try_slot(root, cursor)
                if lease is not None:
                    break
            assert lease is not None and round_number == 25
            lease.release()


def test_finite_competing_waiters_all_progress_after_two_real_slots_return(tmp_path):
    with gate.gateway_transport_binding(tmp_path, 2):
        first = gate.acquire(time.monotonic() + 1)
        second = gate.acquire(time.monotonic() + 1)
        barrier = threading.Barrier(9)
        completed, failures = [], []
        guard = threading.Lock()

        def waiter(index):
            barrier.wait(2)
            try:
                lease = gate.acquire(time.monotonic() + 3)
                try:
                    assert gate.counts()['active'] <= 2
                    time.sleep(.005)
                    with guard:
                        completed.append(index)
                finally:
                    lease.release()
            except BaseException as exc:
                failures.append(exc)

        threads = [threading.Thread(target=waiter, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        barrier.wait(2)
        first.release()
        second.release()
        for thread in threads:
            thread.join(4)
        assert not any(thread.is_alive() for thread in threads)
        assert not failures
        assert sorted(completed) == list(range(8))
        assert gate.counts()['active'] == 0
