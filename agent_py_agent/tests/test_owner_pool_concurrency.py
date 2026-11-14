"""#3 并发压测:OwnerScopedAgentPool 在高并发多用户下的正确性(多公司多用户飞书真实场景)。

锁外构建 + 双检那段最容易藏并发 bug。真起几十/上百线程并发 get 一批 owner(有重叠),断言:
① 同一 owner 永远拿到同一个 agent 实例(缓存一致,竞态构建被双检去重);② 不串户(owner 数 = 缓存数,
无泄漏);③ 全程不崩;④ 真 SimpleAgent 并发隔离 home/数据。
"""

from __future__ import annotations

import threading

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


def _run_concurrently(targets: list) -> None:
    threads = [threading.Thread(target=fn) for fn in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_pool_concurrent_get_is_cache_coherent_and_bounded(tmp_path) -> None:
    pool = OwnerScopedAgentPool(AgentConfig(model_backend="echo"), tmp_path, max_agents=1000)
    # 替身 builder:每次构建返回新对象(暴露竞态:若双检失效,同 owner 会缓存到不同实例)
    pool._builder = lambda base, root, owner, ws: object()

    owners = [OwnerIdentity.provider_user("feishu", f"u{i}") for i in range(20)]
    seen: dict[int, set] = {i: set() for i in range(20)}
    lock = threading.Lock()

    def worker(idx: int) -> None:
        for _ in range(15):
            agent = pool.get(owners[idx % 20])
            with lock:
                seen[idx % 20].add(id(agent))

    _run_concurrently([lambda i=i: worker(i) for i in range(200)])  # 200 线程 × 15 次,横跨 20 owner

    for idx in range(20):
        assert len(seen[idx]) == 1  # ⭐ 同一 owner 全程同一个实例(竞态构建被双检去重)
    assert pool.active_count() == 20  # 恰好 20 个 owner 缓存,无重复泄漏


def test_real_agents_concurrent_isolation(tmp_path) -> None:
    pool = OwnerScopedAgentPool(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path, max_agents=50
    )
    results: dict[str, SimpleAgent] = {}
    lock = threading.Lock()

    def worker(uid: str) -> None:
        agent = pool.get(OwnerIdentity.provider_user("feishu", uid))
        with lock:
            results[uid] = agent

    _run_concurrently([lambda u=f"user{i}": worker(u) for i in range(6)])  # 6 用户并发真建 agent

    db_paths = {str(agent.local_store.db_path) for agent in results.values()}
    assert len(db_paths) == 6  # 6 个用户 → 6 条互不相同的数据库路径(并发下也不串户)
    assert all("user" in p for p in db_paths)


def test_same_owner_initialization_runs_once_under_contention(tmp_path):
    import time
    from concurrent.futures import ThreadPoolExecutor

    pool = OwnerScopedAgentPool(AgentConfig(model_backend="echo"), tmp_path)
    built = []

    def build(*_args):
        built.append(object())
        time.sleep(0.02)
        return built[-1]

    pool._builder = build
    owner = OwnerIdentity.provider_user("test", "one")
    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(lambda _: pool.get(owner), range(100)))
    assert len(built) == 1
    assert all(item is built[0] for item in results)
    assert not pool._building


def test_cold_construction_slot_preserves_hot_access_and_releases_on_error(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    import pytest

    from agent_py_agent.agent import core
    from agent_py_agent.agent import owner_scoped_pool as module

    entered, contended, release = (threading.Event() for _ in range(3))
    original = threading.BoundedSemaphore(1)

    class ObservedSlot:
        def __enter__(self):
            if not original.acquire(blocking=False):
                contended.set()
                assert original.acquire(timeout=10)

        def __exit__(self, *_args):
            original.release()

    monkeypatch.setattr(module, '_OWNER_CONSTRUCTION_SLOTS', ObservedSlot())
    calls = []

    def construct(config, *_args, **_kwargs):
        calls.append(config.my_agent_owner_id)
        if config.my_agent_owner_id == 'first':
            entered.set()
            assert release.wait(10)
            raise ValueError('synthetic local construction failure')
        return SimpleNamespace(config=config)

    monkeypatch.setattr(core, 'SimpleAgent', construct)
    config = AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home'))
    pool = OwnerScopedAgentPool(config, tmp_path)
    hot = OwnerIdentity.provider_user('web', 'hot')
    pool._builder = lambda *_args: object()
    cached = pool.get(hot)
    def owner(name):
        return OwnerIdentity.provider_user('web', name)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(module.build_owner_scoped_agent, config, tmp_path, owner('first'), None)
        assert entered.wait(10)
        second = executor.submit(module.build_owner_scoped_agent, config, tmp_path, owner('second'), None)
        try:
            assert contended.wait(10)
            assert calls == ['first']
            assert pool.get(hot) is cached
            assert not second.done()
        finally:
            release.set()
        with pytest.raises(ValueError, match='synthetic local construction failure'):
            first.result(timeout=10)
        assert second.result(timeout=10).config.my_agent_owner_id == 'second'
    assert calls == ['first', 'second']
    assert original.acquire(blocking=False)
    original.release()
