# LLM: C2 回归：出生身份缓存的全局锁只护查找与发布，ps 在锁外执行；发布前复核内核出生身份，ps 期间 PID
#   被复用时不发布、返回未知；实例仅在 ps 期间退出时保留 ps 结果但不入缓存；旧 locale 等价核对的 ps 同样移出
#   锁外。lstart 与 #kb 两种 token 兼容不变。
# 模块用途: 核锁外 ps、PID 复用不发布、退出实例不失身份、僵尸路径复核与旧 locale 等价缓存。
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_native_host_observation as native
from agent_py_agent.agent.tooling import process_registry as registry

# 超过 Linux 默认 pid_max，/proc 下不存在，测试稳定走原生分支。
_PID = 4_194_999
_LSTART = "Wed Nov 18 10:00:00 2026"


# 函数用途: 有界轮询直到条件成立；回归时以失败结束而不是挂住整个测试进程。
def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


@pytest.fixture()
def probe(monkeypatch):
    state = SimpleNamespace(births=["1792929600:1"], ps_calls=[], ps_gate=None, ps_output=_LSTART)

    def run(argv, **kwargs):
        assert not registry._LEGACY_BIRTH_CACHE_LOCK.locked(), "ps 不得在全局出生缓存锁内执行"
        state.ps_calls.append(argv)
        if state.ps_gate is not None:
            state.ps_gate.wait(5)
        return SimpleNamespace(returncode=0, stdout=state.ps_output + "\n")

    def birth(_pid):
        return state.births.pop(0) if len(state.births) > 1 else state.births[0]

    monkeypatch.setattr(registry, "_IS_WINDOWS", False)
    monkeypatch.setattr(registry, "_native_posix_probe_enabled", lambda: True)
    monkeypatch.setattr(registry.subprocess, "run", run)
    monkeypatch.setattr(registry, "_PID_BIRTH_TOKEN_CACHE", OrderedDict())
    monkeypatch.setattr(registry, "_LEGACY_BIRTH_EQUIVALENTS", {})
    monkeypatch.setattr(native, "process_birth", birth)
    return state


def test_ps_runs_outside_lock_and_other_hits_are_not_serialized(probe):
    registry._PID_BIRTH_TOKEN_CACHE[(_PID + 1, "1792929600:9")] = "host:cached#kb=1792929600:9"
    probe.ps_gate = threading.Event()
    result = []
    worker = threading.Thread(target=lambda: result.append(registry.capture_process_birth_token(_PID)))
    worker.start()
    assert _wait_until(lambda: probe.ps_calls), "领头者未在锁外进入 ps"
    # 另一 PID 的命中不等慢 ps。
    with registry._LEGACY_BIRTH_CACHE_LOCK:
        pass
    assert registry._cached_host_birth_token(_PID + 1, "1792929600:9") == "host:cached#kb=1792929600:9"
    probe.ps_gate.set()
    worker.join(5)
    assert result == [f"host:{_LSTART}#kb=1792929600:1"]
    assert registry.capture_process_birth_token(_PID) == result[0]
    assert len(probe.ps_calls) == 1


def test_pid_reused_during_ps_is_not_published(probe):
    # 取键时是旧实例，ps 之后复核已是同 PID 的新实例：不发布、返回未知。
    probe.births = ["1792929600:1", "1792929600:2"]
    assert registry.capture_process_birth_token(_PID) == ""
    assert not registry._PID_BIRTH_TOKEN_CACHE
    # 新实例自己的首查正常发布，旧实例 token 与之不匹配。
    current = registry.capture_process_birth_token(_PID)
    assert current == f"host:{_LSTART}#kb=1792929600:2"
    assert registry._birth_tokens_match(f"host:{_LSTART}#kb=1792929600:1", current) is False
    assert set(registry._PID_BIRTH_TOKEN_CACHE) == {(_PID, "1792929600:2")}


# 回归：前台进程在 ps 期间自然退出（复核观察不到）不是身份改变；按原合同返回 ps 结果，否则原进程组清理会以
#   identity_unavailable 放过子进程（test_shell_foreground_cleanup 实测）。无法复核的结果不入缓存。
def test_instance_exiting_during_ps_keeps_token_but_is_not_cached(probe):
    probe.births = ["1792929600:1", None]
    assert registry.capture_process_birth_token(_PID) == f"host:{_LSTART}#kb=1792929600:1"
    assert not registry._PID_BIRTH_TOKEN_CACHE
    assert len(probe.ps_calls) == 1


def test_zombie_identity_is_rechecked_before_publish(probe, monkeypatch):
    probe.births = [None]
    identities = [(_PID, "1792929600:000001"), (_PID, "1792929600:000002")]
    monkeypatch.setattr(registry, "_darwin_zombie_kernel_identity",
                        lambda _pid: identities.pop(0) if identities else None)
    # ps 期间僵尸被回收且 PID 已属新实例：不发布、返回未知。
    assert registry.capture_process_birth_token(_PID) == ""
    assert not registry._PID_BIRTH_TOKEN_CACHE
    # 僵尸在 ps 期间仅被回收（复核观察不到）：返回原 ps 结果，但不入缓存。
    identities[:] = [(_PID, "1792929600:000001"), None]
    assert registry.capture_process_birth_token(_PID) == f"host:{_LSTART}#kb=1792929600:000001"
    assert not registry._PID_BIRTH_TOKEN_CACHE
    assert len(probe.ps_calls) == 2


def test_legacy_locale_equivalence_runs_ps_outside_lock(probe):
    expected = "host:mer. 18 nov. 2026 10:00:00"
    current = f"host:{_LSTART}"
    probe.ps_output = "mer.  18 nov. 2026 10:00:00"
    assert registry._birth_token_matches(_PID, expected, current) is True
    assert (_PID, expected, current) in registry._LEGACY_BIRTH_EQUIVALENTS
    assert registry._birth_token_matches(_PID, expected, current) is True
    assert len(probe.ps_calls) == 1
    probe.ps_output = "jeu. 19 nov. 2026 10:00:00"
    assert registry._birth_token_matches(_PID, "host:mar. 17 nov. 2026 09:00:00", current) is False


def test_follower_takes_over_when_leader_ps_fails(probe, monkeypatch):
    outputs = ["", _LSTART]
    probe.ps_gate = threading.Event()
    original = registry.subprocess.run

    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        return SimpleNamespace(returncode=0, stdout=outputs.pop(0))

    monkeypatch.setattr(registry.subprocess, "run", run)
    results = []
    leader = threading.Thread(target=lambda: results.append(("leader", registry.capture_process_birth_token(_PID))))
    leader.start()
    assert _wait_until(lambda: probe.ps_calls), "领头者未在锁外进入 ps"
    follower = threading.Thread(target=lambda: results.append(("follower", registry.capture_process_birth_token(_PID))))
    follower.start()
    assert _wait_until(lambda: (_PID, "1792929600:1") in getattr(registry, "_PID_BIRTH_TOKEN_FLIGHTS", {}))
    time.sleep(0.1)
    probe.ps_gate.set()
    leader.join(5)
    follower.join(5)
    # 领头者 ps 空响应只得未知且不入缓存；跟随者醒来接手重采，同原锁内串行语义。
    assert dict(results) == {"leader": "", "follower": f"host:{_LSTART}#kb=1792929600:1"}
    assert len(probe.ps_calls) == 2
    assert not registry._PID_BIRTH_TOKEN_FLIGHTS
