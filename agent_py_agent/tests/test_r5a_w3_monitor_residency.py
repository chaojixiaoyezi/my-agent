"""共享监控池的生产接线、全局准入及请求 Context 的真实线程回收合同。"""
from __future__ import annotations

import gc
import os
import sys
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from contextvars import ContextVar
from functools import partial
from pathlib import Path

import pytest

from agent_py_agent.agent.business_spaces import gap158_w6_notify as notify
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.managed_owner_runtime import managed_owner_pool_for_agent
from agent_py_agent.agent.owner_scoped_pool import _close_agent_mcp_clients
from agent_py_agent.agent.scheduler import monitor_pool as monitor
from agent_py_agent.agent.scheduler.execution import _run_with_claim_interrupts
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests import test_gateway_multiprocess_worker_dispatch as worker_cases

worker_case = worker_cases.setup


@pytest.fixture(autouse=True)
def isolated_executors(monkeypatch):
    monkeypatch.setattr(monitor, "_SHARED_POOL", None, raising=False)
    monkeypatch.setattr(notify, "_executor", None)
    monkeypatch.setattr(notify, "_worker_slots", None)
    with ExitStack() as cleanup:
        try:
            yield cleanup
        finally:
            shared = getattr(monitor, "_SHARED_POOL", None)
            if shared is not None:
                shared.shutdown()
            if notify._executor is not None:
                notify._executor.shutdown(wait=True)


def _shared(workers=2, queue=2):
    factory = getattr(monitor, "shared_monitor_pool", None)
    assert callable(factory), "生产组合根需要唯一的进程监控池工厂"
    return factory(max_workers=workers, queue_limit=queue)


def test_loaded_modules_belong_to_this_worktree():
    root = Path(__file__).resolve().parents[1]
    assert Path(monitor.__file__).resolve().is_relative_to(root)
    assert Path(notify.__file__).resolve().is_relative_to(root)


def test_concurrent_factory_creation_and_repeated_use_share_workers(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as callers:
        pools = list(callers.map(lambda _: _shared(), range(32)))
    pool = pools[0]
    assert all(other is pool for other in pools)
    for index in range(20):
        assert _shared() is pool
        assert pool.submit(tmp_path, "monitor", str(index), lambda: "done").result(timeout=10) == "done"
    assert len(pool._executor._threads) <= 2
    assert pool._capacity == 4 and not pool._pending
    assert all(not thread.daemon for thread in pool._executor._threads)


@pytest.mark.parametrize("workers,queue", [(3, 2), (2, 3)])
def test_live_pool_reconfigures_in_place_and_closed_pool_can_rebuild(workers, queue):
    first = _shared()
    assert first._shared_limits == (2, 2)
    assert _shared(workers, queue) is first
    assert first._shared_limits == (workers, queue) and not first._closed
    assert first._capacity == workers + queue
    assert first._executor._max_workers == workers
    assert _shared() is first
    assert first._shared_limits == (2, 2) and first._capacity == 4
    first.shutdown()
    replacement = _shared(workers, queue)
    assert replacement is not first and not replacement._closed
    assert first._closed
    assert replacement._shared_limits == (workers, queue)
    assert replacement._capacity == workers + queue


@pytest.mark.parametrize("workers,queue", [(True, 2), (2, False), (0, 2), (2, -1)])
def test_cached_factory_still_validates_limit_types(workers, queue):
    first = _shared()
    with pytest.raises(ValueError):
        _shared(workers, queue)
    assert _shared() is first


@pytest.mark.parametrize("workers,inflight", [(2, 3), (2, 1)])
def test_production_agents_borrow_one_pool_and_owner_retirement_does_not_close_it(tmp_path, workers, inflight):
    agents = []
    try:
        for index in range(3):
            root = tmp_path / f"owner-{index}"
            agents.append(SimpleAgent(AgentConfig(
                model_backend="echo", my_agent_home=str(root / "home"), enable_plugins=False,
                my_agent_owner_provider="feishu", my_agent_owner_kind="user", my_agent_owner_id=str(index),
                background_owner_workers=workers, gateway_global_inflight_limit=inflight,
            ), root))
        pools = [agent.scheduler_service.monitor_pool for agent in agents]
        assert all(pool is pools[0] for pool in pools)
        pool = pools[0]
        assert pool._executor._max_workers == workers
        assert pool._capacity == max(workers, inflight)
        assert len({str(agent.home_paths.owner_home_dir) for agent in agents}) == 3
        _close_agent_mcp_clients(agents[0])
        del agents[0]
        gc.collect()
        assert not pool._closed
        assert agents[0].scheduler_service.monitor_pool.submit(
            agents[0].home_paths.owner_home_dir, "still-live", "run", lambda: "done",
        ).result(timeout=10) == "done"
    finally:
        for agent in agents:
            _close_agent_mcp_clients(agent)


@pytest.mark.parametrize("workers,inflight", [(64, 500), (128, 256), (128, 500)])
def test_managed_owner_preserves_deployment_monitor_capacity(tmp_path, isolated_executors, workers, inflight):
    gc.collect()
    fd_dir = next((path for path in (Path("/proc/self/fd"), Path("/dev/fd")) if path.is_dir()), None)
    before = len(os.listdir(fd_dir)) if fd_dir is not None else None

    def verify_closed_resources():
        gc.collect()
        if fd_dir is not None:
            fd_delta = len(os.listdir(fd_dir)) - before
            assert fd_delta == 0

    # 最后才测 FD：监控池由 fixture 持有，owner 退役不能提前关闭宿主的共享 worker。
    isolated_executors.callback(verify_closed_resources)
    root = tmp_path / "host"
    store = LocalUserStore(root)
    LocalUserLifecycle(store).create("alice", idempotency_key="managed_monitor_capacity_0001")
    owner = OwnerIdentity.local_user("alice")
    with ExitStack() as cleanup:
        host = SimpleAgent(AgentConfig(
            model_backend="echo", my_agent_home=str(root), enable_plugins=False,
            background_owner_workers=workers, gateway_global_inflight_limit=inflight,
        ), tmp_path / "workspace", defer_mcp_connect=True)
        cleanup.callback(_close_agent_mcp_clients, host)
        pool = host.scheduler_service.monitor_pool
        managed = managed_owner_pool_for_agent(host)
        try:
            with managed.acquire_pinned(owner) as agent:
                assert agent.scheduler_service.monitor_pool is pool
                assert agent.config.background_owner_workers == workers
                assert agent.config.gateway_global_inflight_limit == inflight
                assert pool._shared_limits == (workers, inflight - workers)
                assert pool._executor._max_workers == workers
                assert pool._capacity == inflight
                assert managed.gauges()["building"] == 0
                assert managed.gauges()["pinned"] == 1
                assert managed._shared.gauges()["total_live_instances"] == 1
                with managed.acquire_pinned(owner) as same:
                    assert same is agent
            assert managed.gauges()["pinned"] == 0
            assert managed.gauges()["cached"] == 1
        finally:
            managed.release_idle(0)
            assert managed.gauges() == {"building": 0, "pinned": 0, "cached": 0}
            assert managed._shared.gauges()["pinned_count"] == 0
            assert managed._shared.gauges()["total_live_instances"] == 0
        assert not pool._closed
        assert pool.submit(host.home_paths.owner_home_dir, "still-live", "after-retirement", lambda: "done").result(
            timeout=10) == "done"


def test_shared_admission_is_global_and_duplicate_identity_includes_owner(tmp_path):
    for owner in ("a", "b", "c", "d"):
        (tmp_path / owner).mkdir()
    pool = _shared(1, 2)
    entered, release = threading.Event(), threading.Event()

    def blocking():
        entered.set()
        assert release.wait(timeout=10)
        return "first"

    try:
        first = pool.submit(tmp_path / "a", "same", "a1", blocking)
        assert entered.wait(timeout=10)
        duplicate = pool.submit(tmp_path / "a", "same", "a2", lambda: "unexpected")
        second = pool.submit(tmp_path / "b", "same", "b1", lambda: "second")
        cancelled = pool.submit(tmp_path / "c", "same", "c1", lambda: "unexpected")
        full = pool.submit(tmp_path / "d", "same", "d1", lambda: "unexpected")
        assert duplicate.done() and duplicate.result()["reason"] == "already_running"
        assert full.done() and full.result()["reason"] == "queue_full"
        assert not second.done() and cancelled.cancel()
        replacement = pool.submit(tmp_path / "d", "same", "d2", lambda: "replacement")
        assert not replacement.done()
        release.set()
        assert first.result(timeout=10) == "first"
        assert second.result(timeout=10) == "second"
        assert replacement.result(timeout=10) == "replacement"
        pool._executor.submit(lambda: None).result(timeout=10)
        assert not pool._pending
    finally:
        release.set()


def test_early_cancellation_keeps_pending_until_worker_ack_and_does_not_poison_reuse(tmp_path):
    pool = _shared(1, 1)
    entered, release, early, ran = (threading.Event() for _ in range(4))

    def blocking():
        entered.set()
        assert release.wait(timeout=10)

    try:
        first = pool.submit(tmp_path, "held", "held", blocking)
        assert entered.wait(timeout=10)
        stopped = pool.submit(tmp_path, "cancelled", "cancelled",
            partial(_run_with_claim_interrupts, (), early, ran.set))
        early.set()
        assert not stopped.done() and (str(tmp_path.absolute()), "cancelled") in pool._pending
        release.set()
        first.result(timeout=10)
        with pytest.raises(InterruptedError):
            stopped.result(timeout=10)
        assert not ran.is_set()
        pool._executor.submit(lambda: None).result(timeout=10)
        assert not pool._pending
        assert pool.submit(tmp_path, "cancelled", "next",
            partial(_run_with_claim_interrupts, (), threading.Event(), lambda: "fresh"),
        ).result(timeout=10) == "fresh"
    finally:
        release.set()


class _Request:
    def __init__(self, name):
        self.name = name


@pytest.mark.parametrize("target", ["monitor", "notify"])
@pytest.mark.parametrize("fails", [False, True])
def test_each_job_observes_its_context_without_idle_worker_retention(tmp_path, monkeypatch, target, fails):
    request = ContextVar("r5a-request", default=None)
    seen = []
    pool = monitor.MonitorPool(max_workers=1, queue_limit=1) if target == "monitor" else None
    if target == "notify":
        monkeypatch.setattr(notify, "_limit", lambda _: 1)

    def callback():
        seen.append(request.get().name)
        if fails:
            raise ValueError("synthetic failure")
        return "done"

    try:
        for index in range(2):
            item = _Request(f"request-{index}")
            reference = weakref.ref(item)
            token = request.set(item)
            try:
                future = (pool.submit(tmp_path, "monitor", str(index), callback)
                          if pool is not None else notify._submit(callback))
            finally:
                request.reset(token)
            del item
            assert future is not None
            if fails:
                with pytest.raises(ValueError, match="synthetic failure"):
                    future.result(timeout=10)
            else:
                assert future.result(timeout=10) == "done"
            del future
            executor = pool._executor if pool is not None else notify._executor
            assert executor.submit(lambda: request.get()).result(timeout=10) is None
            gc.collect()
            assert reference() is None
        assert seen == ["request-0", "request-1"]
    finally:
        if pool is not None:
            pool.shutdown()


def test_notify_full_admission_keeps_existing_slots_and_context(monkeypatch):
    monkeypatch.setattr(notify, "_limit", lambda _: 1)
    entered, release = threading.Event(), threading.Event()
    request = ContextVar("notify-owner", default=None)

    def blocking():
        entered.set()
        assert release.wait(timeout=10)
        return request.get()

    token = request.set("first-owner")
    try:
        first = notify._submit(blocking)
    finally:
        request.reset(token)
    try:
        assert entered.wait(timeout=10)
        assert notify._submit(lambda: "unexpected") is None
        release.set()
        assert first.result(timeout=10) == "first-owner"
        notify._executor.submit(lambda: None).result(timeout=10)
        token = request.set("next-owner")
        try:
            assert notify._submit(request.get).result(timeout=10) == "next-owner"
        finally:
            request.reset(token)
        assert all(not thread.daemon for thread in notify._executor._threads)
    finally:
        release.set()


def test_worker_pumps_keep_deployment_context_and_original_birth_binding(worker_case):
    from agent_py_agent.agent.gateway_parts.multiprocess_worker_dispatch import WorkerRequestPump

    deployment = ContextVar("deployment-bootstrap", default=None)
    token = deployment.set("bootstrap")
    pump = WorkerRequestPump(worker_case.frozen, worker_case.pool, 1)
    try:
        executors = (pump._executor, pump.controls._executor, pump.controls._stop_executor)
        expected = "bootstrap" if getattr(sys.flags, "thread_inherit_context", 0) else None
        for executor in executors:
            value, thread = executor.submit(lambda: (deployment.get(), threading.current_thread())).result(timeout=10)
            assert value == expected
            resources = (pump._drain_resource, *pump.controls._drain_resources)
            resource = next(item for item in resources if item.owner is executor)
            assert resource._born_threads.get(thread) is thread
    finally:
        deployment.reset(token)
        pump.close()
    assert all(not thread.is_alive() for resource in resources for thread in resource._born_threads)
