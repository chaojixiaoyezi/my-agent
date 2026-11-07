"""RES-W11：真实 owner 规范重建、并发合流与在途退役保护。"""

from __future__ import annotations

import logging
import shutil
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_py_agent.agent import core, owner_scoped_pool
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests.test_gateway_multiprocess_worker_agent import _shared_snapshot
from agent_py_agent.tests.test_gateway_multiprocess_worker_agent import (
    worker as worker,
)


def _real_pool(tmp_path, *, capacity=4):
    pool = OwnerScopedAgentPool(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
        max_agents=capacity,
    )
    owner = OwnerIdentity.provider_user("res-test", "alice")
    builder, built, mutex = pool._builder, [], threading.Lock()

    def build(config, root, identity, roots):
        with mutex:
            built.append(identity)
        return builder(config, root, identity, roots)

    pool._builder = build
    return pool, owner, built


def _owner_directories(root):
    # 仅在有界临时测试树比较布局；生产热路径不扫描目录。
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_dir()}


@pytest.mark.parametrize("hard", [True, False])
@pytest.mark.parametrize("deleted", ["home", "conversations"])
def test_deleted_owner_tree_rebuilds_canonically_and_appends(tmp_path, monkeypatch, caplog, hard, deleted):
    pool, owner, built = _real_pool(tmp_path)
    initialized, initialize = [], core.ensure_owner_home

    def ensure(root, identity=None):
        result = initialize(root, identity)
        initialized.append(result.home_dir)
        return result

    monkeypatch.setattr(core, "ensure_owner_home", ensure)
    first = pool.get(owner, hard=hard)
    home = first.home_paths.owner_home_dir
    store_root = first.conversation_store.storage.root
    assert store_root.is_relative_to(home)
    assert resolve_owner_home(first.home_paths.root, owner).home_dir == home
    sentinel = home / "memory.md"
    if deleted == "conversations":
        sentinel.write_text("surviving owner facts\n", encoding="utf-8")
    owners = first.home_paths.root / "owners"
    before = _owner_directories(owners)
    shutil.rmtree(home if deleted == "home" else store_root)

    with caplog.at_level(logging.WARNING, logger=owner_scoped_pool.__name__):
        second = pool.get(owner, hard=hard)

    assert second is not first
    assert second.home_paths.owner_home_dir == home
    assert second.conversation_store.storage.root == store_root
    assert home.is_dir() and store_root.is_dir()
    assert built == [owner, owner]
    assert initialized == [home, home]
    assert _owner_directories(owners) == before
    if deleted == "conversations":
        assert sentinel.read_text(encoding="utf-8") == "surviving owner facts\n"
    events = [record.getMessage() for record in caplog.records if record.name == owner_scoped_pool.__name__]
    assert events == [f"OWNER_HOME_RECREATED owner={(owner.provider, owner.owner_kind, owner.owner_id)} path={home}"]

    thread = second.conversation_store.threads.create(canonical_user_id="alice")
    message = second.conversation_store.messages.append(
        {"thread_id": thread.thread_id, "role": "user", "content": "after rebuild"}
    )
    rows, errors = second.conversation_store.messages.recent_report(thread.thread_id)
    assert errors == []
    assert [row.message_id for row in rows] == [message.message_id]
    assert rows[0].content == "after rebuild"
    assert pool.get(owner, hard=hard) is second
    assert built == [owner, owner]


@pytest.mark.parametrize("hard", [True, False])
def test_two_lookups_after_deletion_share_one_real_rebuild(tmp_path, monkeypatch, hard):
    pool, owner, built = _real_pool(tmp_path)
    first = pool.get(owner, hard=hard)
    shutil.rmtree(first.home_paths.owner_home_dir)
    rebuilding, waiter, publish = (threading.Event() for _ in range(3))
    builder = pool._builder

    class ObservedFuture(Future):
        def result(self, timeout=None):
            waiter.set()
            return super().result(timeout=timeout)

    def build(config, root, identity, roots):
        rebuilding.set()
        assert publish.wait(10), "rebuild publication was not released"
        return builder(config, root, identity, roots)

    monkeypatch.setattr(owner_scoped_pool, "Future", ObservedFuture)
    pool._builder = build
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(pool.get, owner, hard=hard)]
        try:
            assert rebuilding.wait(3), "deleted owner returned the stale cached agent"
            futures.append(executor.submit(pool.get, owner, hard=hard))
            assert waiter.wait(3), "second lookup did not join the existing build"
            assert pool.gauges()["building_count"] == 1
        finally:
            publish.set()
        results = [future.result(timeout=10) for future in futures]
    assert results[0] is results[1] and results[0] is not first
    assert built == [owner, owner]
    assert results[0].home_paths.owner_home_dir == first.home_paths.owner_home_dir
    assert results[0].home_paths.owner_home_dir.is_dir()
    assert not pool._building and not pool._pins and not pool._retired_agents


def test_rebuild_never_closes_an_agent_until_its_last_pin_is_released(tmp_path, monkeypatch):
    pool, owner, built = _real_pool(tmp_path)
    closed = []
    with pool.acquire_pinned(owner) as first:
        close = first.tools.close_mcp_clients

        def observe_close():
            closed.append(first)
            close()

        monkeypatch.setattr(first.tools, "close_mcp_clients", observe_close)
        with pool.pin(first):
            shutil.rmtree(first.home_paths.owner_home_dir)
            second = pool.get(owner)
            assert second is not first
            assert closed == []
            assert pool._pins[id(first)] == 2
            assert pool.retired_count == 1
            assert pool.gauges()["total_live_instances"] == 2
        assert closed == [] and pool._pins[id(first)] == 1
    assert closed == [first]
    assert built == [owner, owner]
    assert not pool._pins and not pool._retired_agents
    assert pool.get(owner) is second


def test_healthy_cache_hit_checks_both_roots_without_rebuilding(tmp_path, monkeypatch, caplog):
    pool, owner, built = _real_pool(tmp_path)
    first = pool.get(owner)
    home, store = first.home_paths.owner_home_dir, first.conversation_store.storage.root
    observed, stat = [], Path.stat

    def observe(path, *args, **kwargs):
        if path in (home, store):
            observed.append(path)
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", observe)
    with caplog.at_level(logging.WARNING, logger=owner_scoped_pool.__name__):
        assert pool.get(owner) is first
    # 续作明确改为两个独立根；固定两次stat仍是O(1)，不把外置会话根当home存在的证明。
    assert observed == [home, store]
    assert built == [owner]
    assert not [record for record in caplog.records if record.name == owner_scoped_pool.__name__]


def _assert_message_roundtrip(agent):
    store = agent.conversation_store
    thread = store.threads.create(canonical_user_id="recovered-owner")
    message = store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "recovered"})
    rows, errors = store.messages.recent_report(thread.thread_id)
    assert errors == []
    assert [(row.message_id, row.content) for row in rows] == [(message.message_id, "recovered")]


@pytest.mark.parametrize("deleted", ["home", "conversations"])
def test_external_conversation_root_does_not_hide_owner_deletion(tmp_path, deleted):
    external = tmp_path / "external-conversations"
    pool = OwnerScopedAgentPool(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), conversation_workspace=str(external)),
        tmp_path,
    )
    owner = OwnerIdentity.provider_user("res-test", "external")
    first = pool.get(owner)
    home = first.home_paths.owner_home_dir
    assert first.conversation_store.storage.root == external and not external.is_relative_to(home)
    sentinel = home / "memory.md"
    sentinel.write_text("keep surviving facts\n", encoding="utf-8")
    shutil.rmtree(home if deleted == "home" else external)
    second = pool.get(owner)
    assert second is not first
    assert second.home_paths.owner_home_dir == home and home.is_dir()
    assert second.conversation_store.storage.root == external and external.is_dir()
    if deleted == "conversations":
        assert sentinel.read_text(encoding="utf-8") == "keep surviving facts\n"
    _assert_message_roundtrip(second)


@pytest.mark.parametrize("hard", [True, False])
def test_saturated_pinned_generation_repairs_in_place_without_exceeding_capacity(tmp_path, monkeypatch, hard):
    pool, owner, built = _real_pool(tmp_path, capacity=1)
    with pool.acquire_pinned(owner, hard=hard) as first:
        closed = []
        monkeypatch.setattr(first.tools, "close_mcp_clients", lambda: closed.append(first))
        home, store = first.home_paths.owner_home_dir, first.conversation_store.storage.root
        shutil.rmtree(home)
        with pool.acquire_pinned(owner, hard=hard) as second:
            assert second is first
            assert home.is_dir() and store.is_dir()
            assert pool._pins[id(first)] == 2 and closed == []
            assert pool.gauges()["total_live_instances"] == 1
            assert built == [owner]
            _assert_message_roundtrip(second)
        assert pool._pins[id(first)] == 1 and closed == []
    assert not pool._pins and not pool._retired_agents
    assert pool.get(owner, hard=hard) is first and closed == []


def test_temporary_pin_is_released_before_reloading_an_unleased_single_slot(tmp_path):
    pool, owner, built = _real_pool(tmp_path, capacity=1)
    first = pool.get(owner)
    shutil.rmtree(first.home_paths.owner_home_dir)
    with pool.acquire_pinned(owner) as second:
        assert second is not first
        assert pool._pins == {id(second): 1}
        assert pool.gauges()["total_live_instances"] == 1
        assert built == [owner, owner]
        _assert_message_roundtrip(second)
    assert not pool._pins and not pool._retired_agents


def test_failed_in_place_repair_releases_only_the_lookup_lease(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space import owner_resolver

    pool, owner, _ = _real_pool(tmp_path, capacity=1)
    with pool.acquire_pinned(owner) as first:
        shutil.rmtree(first.home_paths.owner_home_dir)

        def reject(*_args, **_kwargs):
            raise PermissionError("injected repair refusal")

        with monkeypatch.context() as patch:
            patch.setattr(owner_resolver, "ensure_owner_home", reject)
            with pytest.raises(PermissionError, match="injected repair refusal"):
                with pool.acquire_pinned(owner):
                    pass
        assert pool._pins == {id(first): 1}
        assert pool.gauges()["total_live_instances"] == 1
        assert not pool._building and not pool._retired_agents
    assert not pool._pins
    recovered = pool.get(owner)
    assert recovered.home_paths.owner_home_dir.is_dir()
    _assert_message_roundtrip(recovered)


def _cached_worker_pool(worker, *, capacity=4):
    from dataclasses import replace

    from agent_py_agent.agent.gateway_parts.multiprocess_worker_agent import create_worker_pool
    from agent_py_agent.agent.user_space.owner_resolver import ensure_owner_home

    frozen, config, root, owner, _ = worker
    # 原夹具先准备C物化结果；修复只处理池中已存在过的这个owner，不给冷构造新增权限。
    ensure_owner_home(frozen.launch.owner_root, owner)
    budget = capacity * frozen.launch.partition.count
    pool = create_worker_pool(frozen, replace(config, owner_agent_pool_max_agents=budget), root)
    assert pool._max_agents == capacity
    return pool, owner, frozen.launch.owner_root


@pytest.mark.parametrize("capacity", [1, 2])
def test_cached_worker_recovers_deleted_home_with_shared_resources_unchanged(worker, monkeypatch, capacity):
    pool, owner, root = _cached_worker_pool(worker, capacity=capacity)
    with pool.acquire_pinned(owner) as first:
        before = _shared_snapshot(root)
        home = first.home_paths.owner_home_dir
        closed = []
        monkeypatch.setattr(first.tools, "close_mcp_clients", lambda: closed.append(first))
        shutil.rmtree(home)
        with pool.acquire_pinned(owner) as second:
            assert (second is first) is (capacity == 1)
            assert second.home_paths.owner_home_dir == home and home.is_dir()
            assert pool.gauges()["total_live_instances"] <= capacity
            assert closed == []
            _assert_message_roundtrip(second)
            assert _shared_snapshot(root) == before
        assert closed == []
    assert closed == ([] if capacity == 1 else [first])
    assert not pool._pins and not pool._building and not pool._retired_agents


@pytest.mark.parametrize("force_reload", [False, True])
def test_unpinned_cached_worker_recovers_before_existing_only_reconstruction(worker, force_reload):
    pool, owner, root = _cached_worker_pool(worker)
    first = pool.get(owner)
    home, before = first.home_paths.owner_home_dir, _shared_snapshot(root)
    shutil.rmtree(home)
    second = pool.get(owner, force_reload=force_reload)
    assert second is not first
    assert second.home_paths.owner_home_dir == home and home.is_dir()
    _assert_message_roundtrip(second)
    assert _shared_snapshot(root) == before


def test_cached_worker_missing_shared_resource_does_not_recreate_shared_or_owner_paths(worker):
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import WorkerBootstrapError

    pool, owner, root = _cached_worker_pool(worker)
    first = pool.get(owner)
    home = first.home_paths.owner_home_dir
    shutil.rmtree(home)
    shared = root / "templates" / "memory.md"
    shared.unlink()
    with pytest.raises(WorkerBootstrapError, match="resource_missing"):
        with pool.acquire_pinned(owner):
            pass
    assert not shared.exists() and not home.exists()
    assert not pool._pins and not pool._building


def test_cached_worker_drift_rejected_before_recovery_writes(worker):
    from dataclasses import replace

    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import WorkerBootstrapError

    pool, owner, root = _cached_worker_pool(worker)
    first = pool.get(owner)
    home = first.home_paths.owner_home_dir
    shutil.rmtree(home)
    foreign = root.parent / "must-not-create"
    first.home_paths = replace(first.home_paths, root=foreign)
    with pytest.raises(WorkerBootstrapError, match="agent_home_root_mismatch"):
        with pool.acquire_pinned(owner):
            pass
    assert not foreign.exists() and not home.exists()
    assert not pool._pins and not pool._building
