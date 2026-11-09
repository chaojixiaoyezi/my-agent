"""原进程身份三态：不可观测不会变成原后台接手/死亡许可。"""
import errno
import os

import pytest

from agent_py_agent.agent.gateway_parts import daemon_metadata as metadata


@pytest.mark.parametrize("error", [errno.EPERM, errno.EIO])
def test_original_process_identity_unobservable_is_unknown_not_dead(tmp_path, monkeypatch, error):
    identity = {"host_id": metadata.process_host_id(), "pid": os.getpid(), "start_time": "observed-birth"}
    def unavailable(pid, sig):
        raise OSError(error, "original-observer-unavailable")
    monkeypatch.setattr(metadata.os, "kill", unavailable)
    assert metadata.process_identity_is_live(identity) is None


def test_original_process_identity_missing_birth_is_unknown_without_pid_probe(monkeypatch):
    identity = {"host_id": metadata.process_host_id(), "pid": os.getpid()}
    monkeypatch.setattr(metadata.os, "kill", lambda *args: pytest.fail("missing_birth_is_not_exact_identity"))
    assert metadata.process_identity_is_live(identity) is None


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("state", ["same", "different", "unavailable"])
def test_darwin_kernel_record_always_uses_same_kernel_origin(monkeypatch, managed, state):
    from agent_py_agent.agent.common import executor_registration
    from agent_py_agent.agent.gateway_parts import multiprocess_executor_identity as native

    monkeypatch.setattr(executor_registration, "managed_request_execution_required", lambda: managed)
    monkeypatch.setattr(metadata.sys, "platform", "darwin")
    monkeypatch.setattr(metadata.os, "kill", lambda *_args: None)
    monkeypatch.setattr(native, "process_birth", lambda _pid: {"same": "123:000456", "different": "124:000456", "unavailable": None}[state])
    monkeypatch.setattr(metadata, "_get_process_start_time", lambda _pid: pytest.fail("kernel origin cannot be compared with PS"))
    identity = {"host_id": metadata.process_host_id(), "pid": 123, "start_time": "123:000456"}
    assert metadata.process_identity_is_live(identity) is {"same": True, "different": False, "unavailable": None}[state]


@pytest.mark.parametrize("birth", ["old PS observation", "1:1", ":123456", "0:123456", "١:000001"])
def test_darwin_managed_legacy_or_malformed_birth_remains_unknown(monkeypatch, birth):
    from agent_py_agent.agent.common import executor_registration
    from agent_py_agent.agent.gateway_parts import multiprocess_executor_identity as native

    monkeypatch.setattr(executor_registration, "managed_request_execution_required", lambda: True)
    monkeypatch.setattr(metadata.sys, "platform", "darwin")
    monkeypatch.setattr(metadata.os, "kill", lambda *_args: None)
    monkeypatch.setattr(native, "process_birth", lambda _pid: pytest.fail("legacy PS is not a kernel observation"))
    monkeypatch.setattr(metadata, "_get_process_start_time", lambda _pid: pytest.fail("managed identity must not spawn PS"))
    assert metadata.process_identity_is_live({"host_id": metadata.process_host_id(), "pid": 123, "start_time": birth}) is None


@pytest.mark.parametrize("platform,birth", [("darwin", "123:000456"), ("linux", 1234)])
def test_managed_identity_producer_uses_original_kernel_birth_without_process_spawn(monkeypatch, platform, birth):
    from agent_py_agent.agent.common import executor_registration
    from agent_py_agent.agent.gateway_parts import multiprocess_executor_identity as native

    monkeypatch.setattr(executor_registration, "managed_request_execution_required", lambda: True)
    monkeypatch.setattr(metadata.sys, "platform", platform)
    monkeypatch.setattr(native, "process_birth", lambda pid: birth if pid == 123 else pytest.fail("foreign pid"))
    monkeypatch.setattr(metadata, "_get_process_start_time", lambda _pid: pytest.fail("managed production cannot spawn PS"))
    assert metadata.build_process_identity(123) == {"host_id": metadata.process_host_id(), "pid": 123, "start_time": birth}


def test_count_zero_identity_producer_keeps_original_observer(monkeypatch):
    from agent_py_agent.agent.common import executor_registration
    from agent_py_agent.agent.gateway_parts import multiprocess_executor_identity as native

    monkeypatch.setattr(executor_registration, "managed_request_execution_required", lambda: False)
    monkeypatch.setattr(metadata, "_get_process_start_time", lambda pid: "original-count-zero" if pid == 123 else pytest.fail("foreign pid"))
    monkeypatch.setattr(native, "process_birth", lambda _pid: pytest.fail("Count0 production must retain original observer"))
    assert metadata.build_process_identity(123)["start_time"] == "original-count-zero"


@pytest.mark.parametrize("damage", ["bool_pid", "string_pid", "float_pid", "missing_birth", "bool_birth", "empty_birth", "foreign_host"])
def test_invalid_or_foreign_original_identity_does_not_probe_pid(monkeypatch, damage):
    identity = {"host_id": metadata.process_host_id(), "pid": 123, "start_time": 42}
    if damage == "bool_pid":
        identity["pid"] = True
    elif damage == "string_pid":
        identity["pid"] = "123"
    elif damage == "float_pid":
        identity["pid"] = 123.0
    elif damage == "missing_birth":
        identity.pop("start_time")
    elif damage == "bool_birth":
        identity["start_time"] = True
    elif damage == "empty_birth":
        identity["start_time"] = ""
    else:
        identity["host_id"] = "foreign-host"
    monkeypatch.setattr(metadata.os, "kill", lambda *_args: pytest.fail("invalid identity is not a process observation"))
    assert metadata.process_identity_is_live(identity) is None


def test_only_exact_esrch_proves_original_pid_missing(monkeypatch):
    identity = {"host_id": metadata.process_host_id(), "pid": 123, "start_time": 42}
    def missing(*_args):
        raise OSError(errno.ESRCH, "synthetic process missing")
    monkeypatch.setattr(metadata.os, "kill", missing)
    assert metadata.process_identity_is_live(identity) is False


@pytest.fixture
def darwin_binding():
    from agent_py_agent.agent.gateway_parts import multiprocess_executor_identity as native

    native._darwin_proc_pidinfo.cache_clear()
    try:
        yield native
    finally:
        native._darwin_proc_pidinfo.cache_clear()


def test_darwin_binding_is_reused_but_birth_is_always_queried(monkeypatch, darwin_binding):
    import ctypes
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import (
        multiprocess_native_host_observation as observation,
    )

    native = darwin_binding
    calls, loads = [], []

    class Query:
        def __call__(self, pid, flavor, arg, pointer, size):
            calls.append(pid)
            assert (flavor, arg, size) == (3, 0, ctypes.sizeof(native._ProcBSDInfo))
            info = ctypes.cast(pointer, ctypes.POINTER(native._ProcBSDInfo)).contents
            info.start_sec, info.start_usec = 123 + len(calls), pid
            return size

    query = Query()

    def load(*args, **kwargs):
        loads.append((args, kwargs))
        return SimpleNamespace(proc_pidinfo=query)

    monkeypatch.setattr(native.ctypes, 'CDLL', load)
    assert native._darwin_birth(10) == '124:000010'
    assert native._darwin_birth(10) == '125:000010', '同PID新出生值不能被旧缓存掩盖'
    info, error = observation._darwin_info(20)
    assert error == 0 and info.start_sec == 126 and info.start_usec == 20
    assert calls == [10, 10, 20] and len(loads) == 1
    assert query.argtypes == [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    assert query.restype is ctypes.c_int


@pytest.mark.parametrize('result,seconds', [(-1, 12), (0, 12), (135, 12), (136, 0)])
def test_cached_darwin_binding_never_accepts_short_or_zero_birth(monkeypatch, darwin_binding, result, seconds):
    import ctypes
    from types import SimpleNamespace

    native = darwin_binding

    class Query:
        def __call__(self, _pid, _flavor, _arg, pointer, _size):
            ctypes.cast(pointer, ctypes.POINTER(native._ProcBSDInfo)).contents.start_sec = seconds
            return result

    monkeypatch.setattr(native.ctypes, 'CDLL', lambda *_a, **_kw: SimpleNamespace(proc_pidinfo=Query()))
    assert native._darwin_birth(10) is None
    assert native._darwin_birth(10) is None
    assert native._darwin_proc_pidinfo.cache_info().currsize == 1


@pytest.mark.parametrize('failure', [OSError, AttributeError, OverflowError])
def test_darwin_loader_failure_remains_unknown_and_is_not_cached(monkeypatch, darwin_binding, failure):
    from types import SimpleNamespace

    native = darwin_binding
    attempts = []

    def load(*_args, **_kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise failure('synthetic loader unavailable')
        return SimpleNamespace(proc_pidinfo=SimpleNamespace())

    monkeypatch.setattr(native.ctypes, 'CDLL', load)
    assert native._darwin_birth(10) is None
    assert native._darwin_proc_pidinfo.cache_info().currsize == 0
    assert native._darwin_proc_pidinfo() is not None
    assert len(attempts) == 2


def test_shared_darwin_binding_parallel_queries_keep_separate_buffers(monkeypatch, darwin_binding):
    import ctypes
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock
    from types import SimpleNamespace

    native = darwin_binding
    loads, calls = [], []
    lock = Lock()
    barrier = Barrier(4)

    class Query:
        def __call__(self, pid, _flavor, _arg, pointer, size):
            with lock:
                calls.append(pid)
            info = ctypes.cast(pointer, ctypes.POINTER(native._ProcBSDInfo)).contents
            info.start_sec, info.start_usec = 123, pid
            return size

    def load(*_args, **_kwargs):
        loads.append(True)
        return SimpleNamespace(proc_pidinfo=Query())

    monkeypatch.setattr(native.ctypes, 'CDLL', load)
    native._darwin_proc_pidinfo()

    def work(pid):
        barrier.wait(timeout=5)
        return [native._darwin_birth(pid) for _ in range(20)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(work, pid) for pid in range(10, 14)]
        assert [future.result(timeout=5) for future in futures] == [
            [f'123:{pid:06d}'] * 20 for pid in range(10, 14)]
    assert len(loads) == 1 and len(calls) == 80


def test_native_birth_binding_survives_fork_without_caching_parent_identity():
    import subprocess
    import sys

    # 单独子进程在无活动线程时fork；只查询本测试拥有的父子PID，不接触宿主服务。
    script = '''
import json, os, sys
from agent_py_agent.agent.gateway_parts.multiprocess_executor_identity import process_birth
parent = os.getpid()
birth = process_birth(parent)
if not hasattr(os, 'fork'):
    assert birth is None
    raise SystemExit(0)
assert birth is not None
reader, writer = os.pipe()
child = os.fork()
if child == 0:
    os.close(reader)
    result = [os.getpid(), process_birth(os.getpid()), process_birth(parent)]
    os.write(writer, json.dumps(result).encode())
    os.close(writer)
    os._exit(0)
os.close(writer)
with os.fdopen(reader) as source:
    observed = json.load(source)
assert os.waitpid(child, 0)[1] == 0
assert observed[0] == child and observed[1] is not None and observed[2] == birth
assert process_birth(parent) == birth
if sys.platform == 'darwin':
    assert observed[1] != birth
'''
    subprocess.run([sys.executable, '-c', script], check=True, timeout=10, capture_output=True, text=True)


def test_shared_darwin_binding_keeps_errno_thread_local(monkeypatch, darwin_binding):
    import ctypes
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import (
        multiprocess_native_host_observation as observation,
    )

    barrier = Barrier(4)

    class Query:
        def __call__(self, pid, *_args):
            ctypes.set_errno(pid)
            barrier.wait(timeout=5)
            return 0

    monkeypatch.setattr(darwin_binding.ctypes, 'CDLL',
                        lambda *_a, **_kw: SimpleNamespace(proc_pidinfo=Query()))
    darwin_binding._darwin_proc_pidinfo()
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(observation._darwin_info, pid) for pid in range(10, 14)]
        assert [job.result(timeout=5) for job in jobs] == [(None, pid) for pid in range(10, 14)]
