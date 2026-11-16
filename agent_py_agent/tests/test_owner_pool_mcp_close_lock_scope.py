"""GWPERF-F2 回归测试: MCP 关闭/重开运行在池生命周期锁与缓存锁外，按对象身份 Event 去重并协同 pin 重开。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import owner_scoped_pool as pool_module
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


def _make_pool(
    tmp_path, *, max_agents: int = 2, on_close=None, on_reopen=None
) -> tuple[OwnerScopedAgentPool, dict[str, list[SimpleNamespace]]]:
    pool = OwnerScopedAgentPool(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
        max_agents=max_agents,
    )
    built: dict[str, list[SimpleNamespace]] = {}

    def build(_base, _root, owner, _roots):
        uid = owner.owner_id
        agent = SimpleNamespace(owner_id=uid)

        def close_mcp():
            if on_close is not None:
                on_close(agent)

        def reopen_mcp():
            if on_reopen is not None:
                on_reopen(agent)

        agent.tools = SimpleNamespace(close_mcp_clients=close_mcp, reopen_mcp_clients=reopen_mcp)
        built.setdefault(uid, []).append(agent)
        return agent

    pool._builder = build
    return pool, built


def _wrap_closing_wait(done_event: threading.Event, wait_entered: threading.Event) -> None:
    orig_wait = done_event.wait

    def tracked_wait(timeout=None):
        wait_entered.set()
        return orig_wait(timeout=timeout)

    done_event.wait = tracked_wait  # type: ignore[method-assign]


def test_unrelated_owner_acquire_pin_and_publication_do_not_wait_on_evicted_mcp_close(tmp_path) -> None:
    close_entered = threading.Event()
    allow_close = threading.Event()
    u4_done = threading.Event()
    close_calls: list[str] = []

    def on_close(agent):
        close_calls.append(agent.owner_id)
        if agent.owner_id == "u1":
            close_entered.set()
            assert allow_close.wait(10)

    pool, _ = _make_pool(tmp_path, max_agents=2, on_close=on_close)
    u1 = OwnerIdentity.provider_user("feishu", "u1")
    u2 = OwnerIdentity.provider_user("feishu", "u2")
    u3 = OwnerIdentity.provider_user("feishu", "u3")
    u4 = OwnerIdentity.provider_user("feishu", "u4")

    pool.get(u1)
    pool.get(u2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        evictor = executor.submit(pool.get, u3)
        try:
            assert close_entered.wait(5), "u1 close_mcp_clients did not start"

            # 在 u1 的 MCP 关闭仍阻塞期间，另一线程验证池锁与生命周期锁均未被持有，且无关 owner u4 立即完成冷获取/pin/发布
            def run_u4():
                assert pool._mcp_lifecycle_lock.acquire(blocking=False)
                pool._mcp_lifecycle_lock.release()
                assert pool._lock.acquire(blocking=False)
                pool._lock.release()
                with pool.acquire_pinned(u4) as agent_u4:
                    assert agent_u4.owner_id == "u4"
                    with pool.pin(agent_u4):
                        assert pool.peek(u4) is agent_u4
                u4_done.set()

            u4_future = executor.submit(run_u4)
            assert u4_done.wait(5), "u4 cold acquire/pin/publication stalled behind u1 MCP close"
            u4_future.result(timeout=5)
            # Option D-W2 = A: 在途关闭不计入容量预算，u3 与 u4 均驻留池内且不抛 PoolCapacityExceededError
            assert pool.gauges()["total_live_instances"] == 2
        finally:
            allow_close.set()
        assert evictor.result(timeout=5).owner_id == "u3"

    assert close_calls == ["u1", "u2"]
    assert not pool._mcp_closing
    assert not pool._pins


def test_repin_during_inflight_close_waits_for_close_event_and_reopens_after_close(tmp_path) -> None:
    close_entered = threading.Event()
    allow_close = threading.Event()
    pin_waiting = threading.Event()
    events: list[str] = []

    def on_close(agent):
        if agent.owner_id == "u1":
            events.append("close_start")
            if events.count("close_start") == 1:
                close_entered.set()
                assert allow_close.wait(10)
            events.append("close_end")

    def on_reopen(agent):
        if agent.owner_id == "u1":
            assert pool._mcp_lifecycle_lock.acquire(blocking=False)
            pool._mcp_lifecycle_lock.release()
            assert pool._lock.acquire(blocking=False)
            pool._lock.release()
            events.append("reopen")

    pool, _ = _make_pool(tmp_path, max_agents=2, on_close=on_close, on_reopen=on_reopen)
    u1_owner = OwnerIdentity.provider_user("feishu", "u1")
    u1_agent = pool.get(u1_owner)
    pool.get(OwnerIdentity.provider_user("feishu", "u2"))

    def repin_u1():
        with pool.pin(u1_agent):
            events.append("pin_body")

    with ThreadPoolExecutor(max_workers=2) as executor:
        evictor = executor.submit(pool.get, OwnerIdentity.provider_user("feishu", "u3"))
        try:
            assert close_entered.wait(5)
            done_event = pool._mcp_closing[id(u1_agent)]
            _wrap_closing_wait(done_event, pin_waiting)
            pinner = executor.submit(repin_u1)
            assert pin_waiting.wait(5), "pin(u1) did not wait on u1 closing Event"
            assert events == ["close_start"], f"reopen or pin_body ran before close finished: {events}"
        finally:
            allow_close.set()
        evictor.result(timeout=5)
        pinner.result(timeout=5)

    # 首次关闭完成后才 reopen -> pin_body -> 退出 pin 时再次退役关闭
    assert events == ["close_start", "close_end", "reopen", "pin_body", "close_start", "close_end"]
    assert not pool._mcp_closing
    assert not pool._pins
    assert not pool._retired_agents


def test_multiple_concurrent_repins_during_inflight_close_stay_pinned_until_last_unpin(tmp_path) -> None:
    close_entered = threading.Event()
    allow_close = threading.Event()
    waiter_count_lock = threading.Lock()
    waiters_entered = 0
    both_waiting = threading.Event()
    both_entered = threading.Barrier(2)
    events: list[str] = []

    def on_close(agent):
        if agent.owner_id == "u1":
            events.append("close")
            if len([e for e in events if e == "close"]) == 1:
                close_entered.set()
                assert allow_close.wait(10)

    def on_reopen(agent):
        if agent.owner_id == "u1":
            events.append("reopen")

    pool, _ = _make_pool(tmp_path, max_agents=2, on_close=on_close, on_reopen=on_reopen)
    u1_agent = pool.get(OwnerIdentity.provider_user("feishu", "u1"))
    pool.get(OwnerIdentity.provider_user("feishu", "u2"))

    def repin():
        with pool.pin(u1_agent):
            both_entered.wait(timeout=5)
            events.append("pin_body")

    with ThreadPoolExecutor(max_workers=3) as executor:
        evictor = executor.submit(pool.get, OwnerIdentity.provider_user("feishu", "u3"))
        try:
            assert close_entered.wait(5)
            done_event = pool._mcp_closing[id(u1_agent)]
            orig_wait = done_event.wait

            def counted_wait(timeout=None):
                nonlocal waiters_entered
                with waiter_count_lock:
                    waiters_entered += 1
                    if waiters_entered == 2:
                        both_waiting.set()
                return orig_wait(timeout=timeout)

            done_event.wait = counted_wait  # type: ignore[method-assign]
            p1 = executor.submit(repin)
            p2 = executor.submit(repin)
            assert both_waiting.wait(5)
            assert pool._pins[id(u1_agent)] == 2
            assert events == ["close"]
        finally:
            allow_close.set()
        evictor.result(timeout=5)
        p1.result(timeout=5)
        p2.result(timeout=5)

    # 首次 close 后两个 pin 都先 reopen，直到最后一次 unpin 才触发第二次 close
    assert events.count("close") == 2
    assert events[0] == "close"
    assert events[-1] == "close"
    assert events.count("reopen") == 2
    assert events.count("pin_body") == 2
    assert not pool._mcp_closing
    assert not pool._pins
    assert not pool._retired_agents


def test_concurrent_close_retired_agents_invokes_close_at_most_once(tmp_path) -> None:
    close_entered = threading.Event()
    allow_close = threading.Event()
    calls: list[str] = []

    def on_close(agent):
        calls.append(agent.owner_id)
        close_entered.set()
        assert allow_close.wait(10)

    pool, _ = _make_pool(tmp_path, max_agents=2)
    u1 = SimpleNamespace(
        owner_id="u1",
        tools=SimpleNamespace(close_mcp_clients=lambda: on_close(u1), reopen_mcp_clients=lambda: None),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(pool._close_retired_agents, [u1])
        try:
            assert close_entered.wait(5)
            second = executor.submit(pool._close_retired_agents, [u1])
            second.result(timeout=5)
            assert len(calls) == 1
        finally:
            allow_close.set()
        first.result(timeout=5)

    assert calls == ["u1"]
    assert not pool._mcp_closing


@pytest.mark.parametrize("raise_from_helper", [True, False])
def test_close_exception_clears_mcp_closing_and_sets_done_for_waiting_pin(
    tmp_path, monkeypatch, raise_from_helper: bool
) -> None:
    close_entered = threading.Event()
    allow_close = threading.Event()
    pin_waiting = threading.Event()
    pin_entered = threading.Event()
    release_pin = threading.Event()
    reopened: list[str] = []

    pool, _ = _make_pool(tmp_path, max_agents=2)

    def failing_tool_close():
        close_entered.set()
        assert allow_close.wait(10)
        raise RuntimeError("injected tool close failure")

    u1 = SimpleNamespace(
        owner_id="u1",
        tools=SimpleNamespace(
            close_mcp_clients=failing_tool_close,
            reopen_mcp_clients=lambda: reopened.append("u1"),
        ),
    )

    if raise_from_helper:
        def failing_close(_agent):
            close_entered.set()
            assert allow_close.wait(10)
            raise RuntimeError("injected helper close failure")

        monkeypatch.setattr(pool_module, "_close_agent_mcp_clients", failing_close)

    def pin_u1():
        with pool.pin(u1) as pinned:
            assert pinned is u1
            pin_entered.set()
            assert release_pin.wait(10)

    with ThreadPoolExecutor(max_workers=2) as executor:
        closer = executor.submit(pool._close_retired_agents, [u1])
        pinner = None
        try:
            assert close_entered.wait(5)
            done_event = pool._mcp_closing[id(u1)]
            _wrap_closing_wait(done_event, pin_waiting)
            pinner = executor.submit(pin_u1)
            assert pin_waiting.wait(5)
        finally:
            allow_close.set()
        try:
            if raise_from_helper:
                with pytest.raises(RuntimeError, match="injected helper close failure"):
                    closer.result(timeout=5)
            else:
                closer.result(timeout=5)
            assert done_event.is_set()
            assert id(u1) not in pool._mcp_closing
            assert pin_entered.wait(5)
            assert reopened == ["u1"]
        finally:
            monkeypatch.undo()
            release_pin.set()
            if pinner is not None:
                pinner.result(timeout=5)

    assert not pool._pins
    assert not pool._retired_agents
    assert not pool._mcp_closing


def test_force_reload_and_evict_idle_do_not_hold_mcp_lifecycle_lock_during_close(tmp_path) -> None:
    close_entered = threading.Event()
    allow_close = threading.Event()
    reloaded_once = False

    def on_close(agent):
        nonlocal reloaded_once
        if agent.owner_id == "u1" and not reloaded_once:
            reloaded_once = True
            close_entered.set()
            assert allow_close.wait(10)

    pool, _ = _make_pool(tmp_path, max_agents=3, on_close=on_close)
    u1 = OwnerIdentity.provider_user("feishu", "u1")
    u4 = OwnerIdentity.provider_user("feishu", "u4")
    pool.get(u1)

    def acquire_u4():
        with pool.acquire_pinned(u4) as agent_u4:
            return agent_u4.owner_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        reloader = executor.submit(lambda: pool.get(u1, force_reload=True))
        try:
            assert close_entered.wait(5)
            # force_reload 关闭旧 u1 时不得持有 _mcp_lifecycle_lock，无关 owner u4 必须能立即冷建并 pin
            assert executor.submit(acquire_u4).result(timeout=5) == "u4"
        finally:
            allow_close.set()
        reloader.result(timeout=5)

    # 再验证 evict_idle 同样不持锁
    close_entered.clear()
    allow_close.clear()
    reloaded_once = False
    u1_agent = pool.peek(u1)
    key_u1 = (u1.provider, u1.owner_kind, u1.owner_id)
    touched = pool._last_used[key_u1]

    with ThreadPoolExecutor(max_workers=2) as executor:
        evictor = executor.submit(pool.evict_idle, key_u1, u1_agent, touched)
        try:
            assert close_entered.wait(5)
            u5 = OwnerIdentity.provider_user("feishu", "u5")
            assert executor.submit(pool.get, u5).result(timeout=5).owner_id == "u5"
        finally:
            allow_close.set()
        assert evictor.result(timeout=5) is True
    assert not pool._mcp_closing


def test_partial_pool_without_mcp_closing_attribute_is_safe() -> None:
    closed: list[str] = []
    reopened: list[str] = []
    pool = object.__new__(OwnerScopedAgentPool)
    pool._lock = threading.Lock()
    pool._mcp_lifecycle_lock = threading.RLock()
    pool._hard_agents, pool._soft_agents = {}, {}
    pool._pins, pool._retired_agents, pool._last_used = {}, {}, {}
    pool._max_agents = 2
    pool._building = {}

    agent = SimpleNamespace(
        owner_id="partial",
        tools=SimpleNamespace(
            close_mcp_clients=lambda: closed.append("closed"),
            reopen_mcp_clients=lambda: reopened.append("reopened"),
        ),
    )

    with pool.pin(agent):
        assert pool._pins[id(agent)] == 1
    assert reopened == ["reopened"]
    assert closed == ["closed"]
    assert not pool._pins
    assert not pool._retired_agents
    assert not pool._mcp_closing
