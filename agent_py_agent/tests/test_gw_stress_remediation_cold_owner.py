"""MAX1-C9：冷 owner 构建不再在单线程后台拍内同步执行。

一次冷构建可能耗时数十秒（首次 schema/目录导入与构造槽位排队）；后台拍只把它交给有界预热池、
等待有界秒数，热 owner 的调度器同步与本拍其余阶段不再被拖住。构建完成后下一拍 peek 命中即接上调度器。
"""
from __future__ import annotations

import contextvars
import threading
import time
from types import SimpleNamespace

from agent_py_agent.cli import gateway_loops

_REQUEST_CONTEXT = contextvars.ContextVar("gw_stress_request_context", default=None)


class _SlowColdPool:
    """冷 owner 的 get 阻塞到放行事件，模拟真实冷构建；热 owner 立即命中。"""

    def __init__(self, hot_ids):
        self.agents = {owner_id: SimpleNamespace(owner_id=owner_id) for owner_id in hot_ids}
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.cold_calls: list[str] = []
        self.hot_calls: list[str] = []
        self.build_threads: list[str] = []
        self.build_context: list[object] = []

    def peek(self, owner):
        with self.lock:
            return self.agents.get(owner.owner_id)

    def get(self, owner, *, hard=True, touch=False):
        with self.lock:
            hit = self.agents.get(owner.owner_id)
        if hit is not None:
            self.hot_calls.append(owner.owner_id)
            return hit
        self.cold_calls.append(owner.owner_id)
        self.build_threads.append(threading.current_thread().name)
        self.build_context.append(_REQUEST_CONTEXT.get())
        assert self.release.wait(10), "测试放行超时"
        with self.lock:
            return self.agents.setdefault(owner.owner_id, SimpleNamespace(owner_id=owner.owner_id))

    def hard_agents(self):
        with self.lock:
            return list(self.agents.values())


def _supervisor(pool, owners):
    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = SimpleNamespace(config=SimpleNamespace())
    supervisor._registry = SimpleNamespace(snapshot=lambda: owners, hard_snapshot=lambda: owners)
    supervisor._owner_pool = pool
    supervisor._managed_owner_pool = None
    supervisor._ensure_managed_owner_pool = lambda: None
    supervisor._owner_schedulers = {}
    supervisor._channels = SimpleNamespace()
    supervisor._owner_build_cursor = 0
    supervisor._inflight = {}
    supervisor._drop_owner_planner_scope = lambda _key: None
    return supervisor


def test_cold_owner_burst_no_longer_serializes_inside_one_sync_tick(monkeypatch):
    monkeypatch.setattr(gateway_loops, "_build_background_scheduler",
                        lambda scoped_agent, _channels: SimpleNamespace(agent=scoped_agent))
    owners = [SimpleNamespace(provider="local", owner_kind="user", owner_id=f"cold-{index}") for index in range(5)]
    owners.append(SimpleNamespace(provider="local", owner_kind="user", owner_id="hot-0"))
    pool = _SlowColdPool(["hot-0"])
    supervisor = _supervisor(pool, owners)
    budget = gateway_loops._COLD_OWNER_BUILDS_PER_TICK_COUNT
    wait_budget = gateway_loops._COLD_OWNER_WARMUP_WAIT_SECONDS
    token = _REQUEST_CONTEXT.set("request-scoped-object-graph")
    try:
        started = time.monotonic()
        supervisor._sync_owner_schedulers()
        first_tick = time.monotonic() - started
        # 冷构建仍在阻塞，后台拍只等有界秒数；热 owner 照常同步出调度器。
        assert first_tick < wait_budget + 1.0, first_tick
        assert pool.hot_calls == ["hot-0"]
        assert [id(agent) for agent in pool.hard_agents()] == list(supervisor._owner_schedulers)
        assert len(supervisor._owner_warmups) == budget
        # 同一冷 owner 在预热期间不重复提交，在途数始终不超过每拍额度。
        supervisor._sync_owner_schedulers()
        assert len(supervisor._owner_warmups) == budget
        assert sorted(pool.cold_calls) == [f"cold-{index}" for index in range(budget)]
    finally:
        _REQUEST_CONTEXT.reset(token)
        pool.release.set()
    # 预热线程以空上下文出生，不持留触发拍的上下文对象图（3.14t 线程上下文继承）。
    assert set(pool.build_context) == {None}
    assert all(name.startswith("owner-warmup") for name in pool.build_threads)
    deadline = time.monotonic() + 10
    while len(supervisor._owner_schedulers) < len(owners):
        assert time.monotonic() < deadline, "冷 owner 预热完成后应在后续拍接上调度器"
        supervisor._sync_owner_schedulers()
    assert not supervisor._owner_warmups
    assert sorted(pool.cold_calls) == [f"cold-{index}" for index in range(5)]
    supervisor._owner_warmup_executor.shutdown(wait=True)


def test_failed_cold_warmup_is_reported_and_retried_on_later_ticks(monkeypatch):
    monkeypatch.setattr(gateway_loops, "_build_background_scheduler",
                        lambda scoped_agent, _channels: SimpleNamespace(agent=scoped_agent))
    errors = []
    monkeypatch.setattr(gateway_loops, "_print_gateway_loop_error",
                        lambda context, label, exc: errors.append((context, label, type(exc).__name__)))
    owner = SimpleNamespace(provider="local", owner_kind="user", owner_id="flaky")
    attempts = []

    class _FlakyPool(_SlowColdPool):
        def get(self, owner, *, hard=True, touch=False):
            attempts.append(owner.owner_id)
            if len(attempts) == 1:
                raise OSError("first cold build failed")
            with self.lock:
                return self.agents.setdefault(owner.owner_id, SimpleNamespace(owner_id=owner.owner_id))

    pool = _FlakyPool([])
    supervisor = _supervisor(pool, [owner])
    supervisor._sync_owner_schedulers()
    assert errors == [("gateway_background_main.owner_build", "flaky", "OSError")]
    assert not supervisor._owner_warmups and not supervisor._owner_schedulers
    supervisor._sync_owner_schedulers()
    assert attempts == ["flaky", "flaky"] and len(supervisor._owner_schedulers) == 1
    supervisor._owner_warmup_executor.shutdown(wait=True)
