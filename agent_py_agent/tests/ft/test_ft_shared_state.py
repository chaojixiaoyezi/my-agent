"""共享状态运行时探针；不读取设计清单，也不以源码形状代替行为。"""
from __future__ import annotations

import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from agent_py_agent.agent.common import json_io, runtime_gate_registry
from agent_py_agent.agent.memory_archive import tokens
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

STRESS_THREAD_COUNT = 8
STRESS_ITERATION_COUNT = 128
STRESS_CACHE_ENTRY_COUNT = 17
WAIT_TIMEOUT_SECONDS = 10


def _parallel(callback):
    barrier = threading.Barrier(STRESS_THREAD_COUNT)

    def run(index):
        barrier.wait(timeout=WAIT_TIMEOUT_SECONDS)
        return callback(index)

    with ThreadPoolExecutor(max_workers=STRESS_THREAD_COUNT) as executor:
        return list(executor.map(run, range(STRESS_THREAD_COUNT)))


def test_token_cache_hit_and_eviction_are_one_locked_operation(monkeypatch):
    """命中 pop/重插、发布和逐出必须持同一叶子锁；旧实现确定性失败。"""
    observed = []

    class CheckedCache(OrderedDict):
        def check(self):
            lock = getattr(tokens, "_ITEM_LENGTH_CACHE_LOCK", None)
            assert lock is not None and lock.locked(), "token LRU operation ran outside its leaf lock"
            observed.append(True)

        def pop(self, *args):
            self.check()
            return super().pop(*args)

        def __setitem__(self, key, value):
            self.check()
            return super().__setitem__(key, value)

        def popitem(self, *args, **kwargs):
            self.check()
            return super().popitem(*args, **kwargs)

    monkeypatch.setattr(tokens, "_ITEM_LENGTH_CACHE", CheckedCache())
    monkeypatch.setattr(tokens, "_ITEM_LENGTH_CACHE_MAX_ENTRY_COUNT", 1)
    expected = tokens._encode_element_lengths("中文")
    assert tokens._cached_element_lengths("中文") == expected
    assert tokens._cached_element_lengths("中文") == expected
    assert tokens._cached_element_lengths("next") == tokens._encode_element_lengths("next")
    assert list(tokens._ITEM_LENGTH_CACHE) == [tokens._item_length_fingerprint("next")]
    assert observed


def test_token_cache_computation_does_not_hold_leaf_lock(monkeypatch):
    monkeypatch.setattr(tokens, "_ITEM_LENGTH_CACHE", OrderedDict())
    encode = tokens._encode_element_lengths

    def checked_encode(item):
        lock = getattr(tokens, "_ITEM_LENGTH_CACHE_LOCK", None)
        assert lock is not None, "token LRU has no leaf lock"
        acquired = lock.acquire(blocking=False)
        assert acquired, "length computation holds the cache lock"
        lock.release()
        return encode(item)

    monkeypatch.setattr(tokens, "_encode_element_lengths", checked_encode)
    assert tokens._cached_element_lengths(["value", 23]) == encode(["value", 23])


def test_token_cache_concurrent_values_equal_serial_computation(monkeypatch):
    monkeypatch.setattr(tokens, "_ITEM_LENGTH_CACHE", OrderedDict())
    monkeypatch.setattr(tokens, "_ITEM_LENGTH_CACHE_MAX_ENTRY_COUNT", STRESS_CACHE_ENTRY_COUNT)
    items = [{"message": f"消息 {i}", "data": [i, True, None]} for i in range(STRESS_ITERATION_COUNT)]
    expected = [tokens._encode_element_lengths(item) for item in items]

    def run(index):
        for offset in range(STRESS_ITERATION_COUNT):
            slot = (offset + index) % len(items)
            assert tokens._cached_element_lengths(items[slot]) == expected[slot]
        return True

    assert all(_parallel(run))
    assert len(tokens._ITEM_LENGTH_CACHE) <= STRESS_CACHE_ENTRY_COUNT
    for fingerprint, packed in tokens._ITEM_LENGTH_CACHE.items():
        slot = next(i for i, item in enumerate(items) if tokens._item_length_fingerprint(item) == fingerprint)
        assert tokens._unpack_lengths(packed) == expected[slot]


def test_json_lock_table_returns_one_live_handle_per_path(tmp_path):
    path = tmp_path / "shared.json"
    # 强引用保持原弱表条目存活；不把 GC 释放后的新句柄误判成并发重复创建。
    original = json_io.path_thread_lock(path)
    counter = [0]

    def run(_index):
        for _ in range(STRESS_ITERATION_COUNT):
            handle = json_io.path_thread_lock(path)
            assert handle is original
            with handle.lock:
                counter[0] += 1
        return handle

    assert all(handle is original for handle in _parallel(run))
    assert counter[0] == STRESS_THREAD_COUNT * STRESS_ITERATION_COUNT
    with json_io._JSON_FILE_LOCKS_GUARD:
        assert any(handle is original for handle in json_io._JSON_FILE_LOCKS.values())


def test_runtime_gate_registry_concurrent_register_and_observe():
    class Gate:
        def __init__(self):
            self._mutex = threading.RLock()
            self._lease = SimpleNamespace(_lifecycle_gate=threading.RLock())

    owners = [Gate() for _ in range(STRESS_THREAD_COUNT)]

    def run(index):
        owner = owners[index]
        for _ in range(STRESS_ITERATION_COUNT):
            runtime_gate_registry.register_runtime_gate(owner)
            with owner._mutex:
                assert runtime_gate_registry.current_thread_holds_runtime_gate()
            with owner._lease._lifecycle_gate:
                assert runtime_gate_registry.current_thread_holds_runtime_gate()
            assert not runtime_gate_registry.current_thread_holds_runtime_gate()
        return owner

    assert _parallel(run) == owners
    with runtime_gate_registry._LIVE_LOCK:
        assert all(owner in runtime_gate_registry._LIVE for owner in owners)


def test_owner_pool_concurrent_build_pin_and_owner_isolation(tmp_path):
    config = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"))
    pool = OwnerScopedAgentPool(config, tmp_path, max_agents=STRESS_THREAD_COUNT)
    owners = [OwnerIdentity.provider_user("feishu", f"ft-owner-{i % 2}") for i in range(STRESS_THREAD_COUNT)]
    builds = []
    build_lock = threading.Lock()

    def build(_config, _root, owner, _roots):
        with build_lock:
            builds.append(owner.owner_id)
        return SimpleNamespace(owner_id=owner.owner_id)

    pool._builder = build

    def run(index):
        with pool.acquire_pinned(owners[index]) as first:
            assert first.owner_id == owners[index].owner_id
            for _ in range(STRESS_ITERATION_COUNT):
                with pool.acquire_pinned(owners[index]) as again:
                    assert again is first
            return first

    agents = _parallel(run)
    assert agents[0] is agents[2]
    assert agents[1] is agents[3]
    assert agents[0] is not agents[1]
    assert sorted(builds) == ["ft-owner-0", "ft-owner-1"]
    assert pool.gauges()["pinned_count"] == 0
    assert pool.gauges()["building_count"] == 0
    assert pool.gauges()["total_live_instances"] == 2


def test_token_cache_hit_refreshes_lru_before_eviction(monkeypatch):
    monkeypatch.setattr(tokens, "_ITEM_LENGTH_CACHE", OrderedDict())
    monkeypatch.setattr(tokens, "_ITEM_LENGTH_CACHE_MAX_ENTRY_COUNT", 2)
    for item in ("old", "other", "old", "new"):
        assert tokens._cached_element_lengths(item) == tokens._encode_element_lengths(item)
    assert list(tokens._ITEM_LENGTH_CACHE) == [
        tokens._item_length_fingerprint("old"), tokens._item_length_fingerprint("new"),
    ]


def test_token_cache_overlapping_same_key_misses_do_not_hold_lock(monkeypatch):
    monkeypatch.setattr(tokens, "_ITEM_LENGTH_CACHE", OrderedDict())
    overlap = threading.Barrier(STRESS_THREAD_COUNT)
    encode = tokens._encode_element_lengths

    def held_outside_cache(item):
        # 所有 miss 必须能同时进入编码；若编码在叶子锁内，首个线程会等不到其余线程。
        overlap.wait(timeout=WAIT_TIMEOUT_SECONDS)
        return encode(item)

    monkeypatch.setattr(tokens, "_encode_element_lengths", held_outside_cache)
    expected = encode("same-key")
    assert _parallel(lambda _index: tokens._cached_element_lengths("same-key")) == [expected] * STRESS_THREAD_COUNT
    assert list(tokens._ITEM_LENGTH_CACHE.values()) == [tokens._pack_lengths(expected)]
