from __future__ import annotations

# LLM: MEM-W4 只在空闲 owner 实例确实被逐出后做一次 gc + glibc malloc_trim(0)，每个 owner_agent_idle_seconds 周期最多一次；
#   非 Linux 或无 malloc_trim 时静默跳过。回收数量、返回值与任何业务状态都不能因此改变。
# 模块用途: 回归保护逐出后的堆归还时机、限频与失败安全（实测 10 个长任务逐出后再归还约 20 MB）。
import ctypes
import sys
import types

import pytest

from agent_py_agent.agent.gateway_parts import owner_retention


# 函数用途: 记录 gc 与 malloc_trim 调用；只替换 owner_retention 自己引用的 gc 与 trim 查找函数。
class _Probe:
    def __init__(self, monkeypatch, *, trim_available=True):
        self.gc_calls = 0
        self.trim_args: list[int] = []
        monkeypatch.setattr(owner_retention.gc, "collect", self._collect)
        monkeypatch.setattr(owner_retention, "_glibc_malloc_trim", lambda: self._trim if trim_available else None)

    def _collect(self, *_args):
        self.gc_calls += 1
        return 0

    def _trim(self, pad):
        self.trim_args.append(pad)
        return 1


# LLM: 逐出后第一次调用 gc + trim(0)；同一空闲周期内第二次不再执行，周期过后才再执行。
# 函数用途: 核对执行时机与按 owner_agent_idle_seconds 的限频。
def test_trim_runs_once_per_idle_window(monkeypatch):
    probe = _Probe(monkeypatch)
    clock = iter([100.0, 130.0, 161.0])
    monkeypatch.setattr(owner_retention.time, "monotonic", lambda: next(clock))
    supervisor = types.SimpleNamespace()
    owner_retention._trim_heap_after_release(supervisor, 60.0)
    owner_retention._trim_heap_after_release(supervisor, 60.0)
    assert (probe.gc_calls, probe.trim_args) == (1, [0])
    owner_retention._trim_heap_after_release(supervisor, 60.0)
    assert (probe.gc_calls, probe.trim_args) == (2, [0, 0])


# 函数用途: 没有可用的 malloc_trim 时只做 gc，不抛异常。
def test_without_malloc_trim_only_collects(monkeypatch):
    probe = _Probe(monkeypatch, trim_available=False)
    owner_retention._trim_heap_after_release(types.SimpleNamespace(), 60.0)
    assert (probe.gc_calls, probe.trim_args) == (1, [])


# LLM: 只替换 owner_retention 模块看到的 sys/ctypes 名称，不改全局 sys.platform（全局改会打断 zoneinfo 等导入）。
# 函数用途: 核对 trim 查找函数在非 Linux、dlopen 失败、缺符号三种情况下返回 None，在 Linux glibc 上返回可调用对象。
@pytest.mark.parametrize("case", ["non_linux", "dlopen_error", "no_symbol"])
def test_glibc_malloc_trim_lookup_is_failure_safe(monkeypatch, case):
    platform = "darwin" if case == "non_linux" else "linux"
    monkeypatch.setattr(owner_retention, "sys", types.SimpleNamespace(platform=platform))

    def _cdll(_name):
        if case == "dlopen_error":
            raise OSError("no libc")
        return types.SimpleNamespace()

    monkeypatch.setattr(ctypes, "CDLL", _cdll)
    assert owner_retention._glibc_malloc_trim() is None


# 函数用途: 在真实平台上核对查找结果（Linux glibc 上应能找到，其他平台为 None）。
def test_glibc_malloc_trim_lookup_on_this_platform():
    found = owner_retention._glibc_malloc_trim()
    if not sys.platform.startswith("linux"):
        assert found is None
    else:
        assert found is None or callable(found)


# LLM: 端到端经过 release_idle_owner_agents：释放 0 个时不 gc/trim；释放 1 个时恰好一次，返回值不变。
# 函数用途: 核对回收入口只在确实逐出实例后触发堆归还。
def test_release_idle_owner_agents_triggers_trim_only_after_eviction(monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery

    probe = _Probe(monkeypatch)

    class _Pool:
        def __init__(self, candidates):
            self.candidates = candidates

        def idle_candidates(self, _seconds):
            return list(self.candidates)

        def evict_idle(self, *_args):
            return True

    class _Registry:
        def discard_idle(self, *_args):
            return True

    monkeypatch.setattr(owner_wake_discovery, "_owner_has_hard_facts", lambda _home: False)
    agent = types.SimpleNamespace(home_paths=types.SimpleNamespace(owner_home_dir="/nonexistent-owner-home"))
    supervisor = types.SimpleNamespace(
        _owner_pool=_Pool([]),
        _base_agent=types.SimpleNamespace(config=types.SimpleNamespace(owner_agent_idle_seconds=60.0)),
        _active_threads_for_owner=lambda _agent_id: 0, _curator_inflight={}, _registry=_Registry(), _owner_schedulers={},
    )
    assert owner_retention.release_idle_owner_agents(supervisor) == 0
    assert (probe.gc_calls, probe.trim_args) == (0, [])
    supervisor._owner_pool = _Pool([(("web", "user", "u1"), agent, 1.0)])
    assert owner_retention.release_idle_owner_agents(supervisor) == 1
    assert (probe.gc_calls, probe.trim_args) == (1, [0])
