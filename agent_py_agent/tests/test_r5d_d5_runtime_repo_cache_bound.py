"""P2-R5A-01/D5: the wake-discovery runtime repository cache is an LRU bounded by the resident owner slots times the
per-owner session slots, instead of keeping one repository per owner ever discovered."""
from __future__ import annotations

import os
from types import SimpleNamespace

from agent_py_agent.agent import owner_wake_discovery as discovery
from agent_py_agent.agent.common import cache_capacity
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path

# 测试登记的常驻 owner 槽数与每 owner 会话槽数（会话槽 = 用户在途上限 + 后台线程数），乘积就是缓存上限。
OWNER_SLOTS = 2
USER_INFLIGHT = 1
BACKGROUND_THREADS = 1
CACHE_BOUND = OWNER_SLOTS * (USER_INFLIGHT + BACKGROUND_THREADS)
# 发现层依次走过的 owner 数（多于上限）。
OWNER_COUNT = 10


# 函数用途: 建 count 个带权威库的 owner home。
def _homes(tmp_path, count: int):
    homes = []
    for index in range(count):
        home = tmp_path / f"owner-{index}"
        db_path = runtime_db_path(home)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        RuntimeRepository(db_path)
        homes.append(home)
    return homes


# 函数用途: 登记小容量后清空缓存；结束时恢复未登记状态。
def _small_capacity():
    cache_capacity.configure_from_config(SimpleNamespace(
        gateway_owner_inflight_limit=OWNER_SLOTS, gateway_user_inflight_limit=USER_INFLIGHT,
        background_threads_per_owner=BACKGROUND_THREADS,
    ))
    discovery._RUNTIME_REPO_CACHE.clear()


# 函数用途: 走遍多于上限的 owner 后缓存不超过上限；一直在用的 owner 不被淘汰（同一实例），被淘汰的再来只是重开。
def test_cache_is_bounded_lru_over_many_owners(tmp_path):
    homes = _homes(tmp_path, OWNER_COUNT)
    _small_capacity()
    try:
        hot = discovery._runtime_repo_for_owner(homes[0])
        for home in homes[1:]:
            assert discovery._runtime_repo_for_owner(home) is not None
            assert discovery._runtime_repo_for_owner(homes[0]) is hot, "常用 owner 不能被淘汰"
            assert len(discovery._RUNTIME_REPO_CACHE) <= CACHE_BOUND
        evicted = discovery._runtime_repo_for_owner(homes[1])
        assert evicted is not None and str(evicted.db_path) == str(runtime_db_path(homes[1]))
        assert len(discovery._RUNTIME_REPO_CACHE) <= CACHE_BOUND
    finally:
        cache_capacity.reset_for_tests()
        discovery._RUNTIME_REPO_CACHE.clear()


# 函数用途: 按 mtime 失效的旧合同不变：库被外部写后换新实例；没有库仍返回 None 且不进缓存。
def test_mtime_invalidation_and_missing_db_unchanged(tmp_path):
    (home,) = _homes(tmp_path, 1)
    _small_capacity()
    try:
        first = discovery._runtime_repo_for_owner(home)
        assert discovery._runtime_repo_for_owner(home) is first
        stat = runtime_db_path(home).stat()
        os.utime(runtime_db_path(home), ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        assert discovery._runtime_repo_for_owner(home) is not first
        assert discovery._runtime_repo_for_owner(tmp_path / "no-db-owner") is None
        assert len(discovery._RUNTIME_REPO_CACHE) == 1
    finally:
        cache_capacity.reset_for_tests()
        discovery._RUNTIME_REPO_CACHE.clear()
