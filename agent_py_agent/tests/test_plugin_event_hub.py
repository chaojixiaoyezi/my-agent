"""B3 事件中心合同测试：合并计数、单在途、跨 owner、握手门、退避分级、计数接口与 Gateway 组装。

全部用假插件客户端 + 真 PluginChannelPool（受控执行器或真线程池），不启动真实插件进程。
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_channel import (
    IDLE_CLOSE_SECONDS,
    PluginChannelPool,
    PluginChannelRevoked,
    PluginChannelTimeout,
)
from agent_py_agent.agent.plugin_events.declarations import PluginEventDeclaration
from agent_py_agent.agent.plugin_events.hub import EventHubWiring, PluginEventHub
from agent_py_agent.agent.plugin_events.protocol import EVENTS_EXTENSION, EventFact

OWNER_A = "/tmp/owner-a"


@pytest.mark.parametrize('kind', ['prompt_submitted', 'turn_started', 'turn_ended', 'tool_call_started', 'tool_call_finished', 'command_executed'])
def test_normalization_only_retains_prompt_content(kind):
    from agent_py_agent.agent.plugin_events.protocol import normalize_event_fact

    fact = normalize_event_fact(EventFact(kind, content='private-marker'))
    assert fact.content == ('private-marker' if kind == 'prompt_submitted' else '')


class _ManualExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def run_all(self):
        while self.jobs:
            fn, args = self.jobs.pop(0)
            fn(*args)

    def shutdown(self, **_kwargs):
        self.jobs.clear()


class _TrackingExecutor:
    """真线程池的薄包装：记录提交次数与完成任务数，让调度断言有确定同步点，不靠 sleep 窗口。"""

    def __init__(self, workers=4):
        self._inner = ThreadPoolExecutor(max_workers=workers)
        self._lock = threading.Lock()
        self.submits = 0
        self.completed = 0

    def submit(self, fn, *args):
        with self._lock:
            self.submits += 1

        def _wrapped():
            try:
                return fn(*args)
            finally:
                with self._lock:
                    self.completed += 1

        return self._inner.submit(_wrapped)

    def shutdown(self, **kwargs):
        self._inner.shutdown(**kwargs)


class _FakeTransport:
    def __init__(self, plugin):
        self.plugin = plugin

    def request(self, method, params, *, timeout, authority_check):
        authority_check()
        self.plugin.calls.append((method, params))
        if self.plugin.gate is not None:
            self.plugin.gate.wait(5)
        if self.plugin.fail is not None:
            raise self.plugin.fail
        return {}


class _FakePlugin:
    def __init__(self, installation, *, capable=True):
        self.installation = installation
        self.calls = []
        self.stopped = 0
        self.fail = None
        self.gate = None
        self.start_error = None
        self.revoked = False
        self.capabilities = {"experimental": {EVENTS_EXTENSION: {"versions": ["1"]}}} if capable else {}
        self.activation_ref = SimpleNamespace(require=self._require)

    def _require(self):
        if self.revoked:
            raise ValueError("revoked")
        return self.installation

    def start(self):
        if self.start_error is not None:
            raise self.start_error
        return _FakeTransport(self)

    def stop(self):
        self.stopped += 1
        return SimpleNamespace(confirmed=True)


class _RemoteError(Exception):
    code = "MCP_REMOTE_ERROR"


def _installation(plugin_id="watch", activation_id="act-1", event_types=("tool_call_finished",),
                  *, content="none"):
    events = tuple(PluginEventDeclaration(t, content if t == "prompt_submitted" else "none")
                   for t in event_types)
    return SimpleNamespace(manifest=SimpleNamespace(plugin_id=plugin_id, events=events),
                           activation=SimpleNamespace(activation_id=activation_id), enabled=True)


def _owner(home=OWNER_A):
    return SimpleNamespace(home_dir=home)


_UNSET = object()


class _Harness:
    def __init__(self, rows=_UNSET, *, capable=True, executor=None, on_create=None):
        self.now = 100.0
        self.rows = [_installation()] if rows is _UNSET else rows
        self.plugins = []
        self.capable = capable
        self.on_create = on_create
        self.executor = executor if executor is not None else _ManualExecutor()
        self.pool = PluginChannelPool(client_factory=self._factory, clock=lambda: self.now)
        self.hub = PluginEventHub(wiring=EventHubWiring(
            installations=lambda _owner: None if self.rows is None else tuple(self.rows),
            clock=lambda: self.now, pool_clock=lambda: self.now, executor=self.executor, pool=self.pool))

    def _factory(self, _owner, installation):
        plugin = _FakePlugin(installation, capable=self.capable)
        if self.on_create is not None:
            self.on_create(plugin)
        self.plugins.append(plugin)
        return plugin

    def publish(self, event_type="tool_call_finished", **kwargs):
        self.hub.publish(_owner(), EventFact(type=event_type, **kwargs))

    def drain(self):
        self.executor.run_all()

    @property
    def plugin(self):
        return self.plugins[0]

    @property
    def total_calls(self):
        return sum(len(plugin.calls) for plugin in self.plugins)

    def events(self, index=0):
        return self.plugin.calls[index][1]["events"]

    def stats(self, event_type="tool_call_finished", plugin_id="watch"):
        return self.hub.stats(OWNER_A)[plugin_id][event_type]


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ── 合并与计数 ──────────────────────────────────────────────────────────


def test_same_type_events_coalesce_to_latest_with_dropped_count():
    h = _Harness()
    h.publish(facts={"index": 0})
    h.drain()  # 槽建立并送达第一条；此后被合并的条数才计入 dropped_before
    for index in range(1, 6):
        h.publish(facts={"index": index})
    h.drain()
    assert len(h.plugin.calls) == 2
    events = h.events(1)
    assert len(events) == 1
    assert events[0]["facts"] == {"index": 5} and events[0]["dropped_before"] == 4
    assert events[0]["seq"] == 2 and events[0]["type"] == "tool_call_finished"
    assert h.stats()["delivered"] == 2 and h.stats()["coalesced"] == 4
    h.hub.close()


def test_different_types_are_batched_in_one_call():
    h = _Harness(rows=[_installation(event_types=("tool_call_finished", "turn_started", "command_executed"))])
    h.publish("tool_call_finished", facts={"n": 1})
    h.publish("turn_started", facts={"n": 2})
    h.publish("command_executed", facts={"n": 3})
    h.drain()
    assert len(h.plugin.calls) == 1
    events = h.events()
    assert {event["type"] for event in events} == {"tool_call_finished", "turn_started", "command_executed"}
    assert all(event["dropped_before"] == 0 for event in events)
    h.hub.close()


def test_seq_increases_per_plugin_across_batches():
    h = _Harness()
    h.publish(facts={"n": 1})
    h.drain()
    h.publish(facts={"n": 2})
    h.drain()
    assert h.events(0)[0]["seq"] == 1 and h.events(1)[0]["seq"] == 2
    h.hub.close()


def test_seq_is_counted_per_plugin_not_globally():
    h = _Harness(rows=[
        _installation(plugin_id="watch-a", activation_id="act-a", event_types=("tool_call_finished",)),
        _installation(plugin_id="watch-b", activation_id="act-b", event_types=("turn_started",)),
    ])
    h.publish("tool_call_finished", facts={"n": 1})
    h.drain()
    h.publish("turn_started", facts={"n": 2})
    h.drain()
    h.publish("tool_call_finished", facts={"n": 3})
    h.drain()
    by_id = {plugin.installation.manifest.plugin_id: plugin for plugin in h.plugins}
    assert [event["seq"] for event in by_id["watch-a"].calls[0][1]["events"]] == [1]
    assert [event["seq"] for event in by_id["watch-b"].calls[0][1]["events"]] == [1]
    assert [event["seq"] for event in by_id["watch-a"].calls[1][1]["events"]] == [2]
    h.hub.close()


# ── 单在途与主流程解耦 ──────────────────────────────────────────────────


def test_inflight_publish_does_not_schedule_second_drain():
    h = _Harness()
    h.publish(facts={"n": 1})
    assert len(h.executor.jobs) == 1
    h.publish(facts={"n": 2})  # 投递循环已在跑：只更新待发，不重复调度
    assert len(h.executor.jobs) == 1
    h.drain()
    assert len(h.plugin.calls) == 1
    h.hub.close()


def test_events_published_while_inflight_wait_for_receipt():
    executor = _TrackingExecutor()
    gate = threading.Event()
    h = _Harness(executor=executor, on_create=lambda plugin: setattr(plugin, "gate", gate))
    try:
        h.publish(facts={"n": 1})
        assert _wait_until(lambda: len(h.plugins) == 1 and len(h.plugins[0].calls) == 1)
        assert executor.submits == 2  # 一轮分发 + 一个发送任务
        h.publish(facts={"n": 2})  # 在途期间的新事件：只进待发
        # 确定性同步点：等 n=2 的那轮分发跑完（发送任务还卡在门闩上，不会先完成），
        # 再断言在途时没有提交新的发送任务（不靠 sleep 窗口）。
        assert _wait_until(lambda: executor.completed >= 2), "n=2 的分发没有跑完"
        assert executor.submits == 3, "在途期间重复调度了发送"
        gate.set()
        assert _wait_until(lambda: len(h.plugin.calls) == 2)
        assert h.events(1)[0]["facts"] == {"n": 2}
    finally:
        gate.set()
        executor.shutdown(wait=True)
        h.hub.close()


def test_publish_returns_immediately_even_when_plugin_is_stuck():
    executor = _TrackingExecutor()
    gate = threading.Event()
    h = _Harness(executor=executor, on_create=lambda plugin: setattr(plugin, "gate", gate))
    try:
        h.publish(facts={"n": 1})
        assert _wait_until(lambda: len(h.plugins) == 1 and len(h.plugins[0].calls) == 1)  # 插件已卡住
        start = time.monotonic()
        h.publish(facts={"n": 2})
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, f"publish 被卡住的插件拖住了：{elapsed:.3f}s"
    finally:
        gate.set()
        executor.shutdown(wait=True)
        h.hub.close()


def _calls_for(harness, plugin_id):
    return sum(len(plugin.calls) for plugin in harness.plugins
               if plugin.installation.manifest.plugin_id == plugin_id)


def test_stuck_plugin_does_not_block_sibling_plugin_same_owner():
    executor = _TrackingExecutor()
    gate = threading.Event()

    def stall_slow(plugin):
        if plugin.installation.manifest.plugin_id.startswith("slow"):
            plugin.gate = gate

    h = _Harness(rows=[
        _installation(plugin_id="slow-a", activation_id="act-a", event_types=("tool_call_finished",)),
        _installation(plugin_id="fast-b", activation_id="act-b", event_types=("turn_started",)),
    ], executor=executor, on_create=stall_slow)
    try:
        h.publish("tool_call_finished")  # slow-a 的请求会挂住
        assert _wait_until(lambda: _calls_for(h, "slow-a") == 1)
        h.publish("turn_started")  # 同 owner 的健康插件：不该被挂住的兄弟拖住
        assert _wait_until(lambda: _calls_for(h, "fast-b") == 1, timeout=1.0), "健康插件被挂住的兄弟拖住了"
    finally:
        gate.set()
        executor.shutdown(wait=True)
        h.hub.close()


def test_stuck_plugins_in_other_owners_do_not_starve_healthy_owner():
    executor = _TrackingExecutor()
    gate = threading.Event()
    tables = {
        "/tmp/owner-1": (_installation(plugin_id="slow-1", activation_id="act-1"),),
        "/tmp/owner-2": (_installation(plugin_id="slow-2", activation_id="act-2"),),
        "/tmp/owner-3": (_installation(plugin_id="fast-3", activation_id="act-3"),),
    }
    seen = {}

    def factory(owner, installation):
        plugin = _FakePlugin(installation)
        if installation.manifest.plugin_id.startswith("slow"):
            plugin.gate = gate
        seen[(str(owner.home_dir), installation.manifest.plugin_id)] = plugin
        return plugin

    def slow_calls():
        total = 0
        for key in (("/tmp/owner-1", "slow-1"), ("/tmp/owner-2", "slow-2")):
            plugin = seen.get(key)
            total += len(plugin.calls) if plugin is not None else 0
        return total

    def fast_calls():
        plugin = seen.get(("/tmp/owner-3", "fast-3"))
        return len(plugin.calls) if plugin is not None else 0

    pool = PluginChannelPool(client_factory=factory, clock=lambda: 100.0)
    hub = PluginEventHub(wiring=EventHubWiring(
        installations=lambda owner: tables[str(owner.home_dir)], clock=lambda: 100.0,
        pool_clock=lambda: 100.0, executor=executor, pool=pool))
    try:
        for home in ("/tmp/owner-1", "/tmp/owner-2"):
            hub.publish(_owner(home), EventFact(type="tool_call_finished"))
        assert _wait_until(lambda: slow_calls() == 2)
        hub.publish(_owner("/tmp/owner-3"), EventFact(type="tool_call_finished"))
        assert _wait_until(lambda: fast_calls() == 1, timeout=1.0), "健康 owner 被其它 owner 挂住的插件拖住了"
    finally:
        gate.set()
        executor.shutdown(wait=True)
        hub.close()


# ── 停用、换代与撤销 ────────────────────────────────────────────────────


def test_disabled_plugin_does_not_receive_events():
    h = _Harness()
    h.publish(facts={"n": 1})
    h.drain()
    assert h.total_calls == 1
    stopped = _installation()
    stopped.enabled = False
    h.rows = [stopped]
    h.publish(facts={"n": 2})
    h.drain()
    assert h.total_calls == 1, "停用后仍收到了事件"
    h.hub.close()


def test_activation_change_discards_pending_and_does_not_backfill():
    h = _Harness()
    h.publish(facts={"n": 1})
    h.drain()
    h.publish(facts={"n": 2})
    h.drain()
    h.publish(facts={"n": 3})
    h.publish(facts={"n": 4})  # 换代前积压两条
    h.rows = [_installation(activation_id="act-2")]
    h.drain()
    assert len(h.plugins) == 1, "换代后仍把旧代待发投了出去"
    h.publish(facts={"n": 5})
    h.drain()
    assert len(h.plugins) == 2
    events = h.plugins[1].calls[0][1]["events"]
    assert events[0]["facts"] == {"n": 5} and events[0]["dropped_before"] == 0
    assert events[0]["seq"] == 1, "换代后 seq 没有从 1 重新开始"
    h.hub.close()


def test_midstream_slot_does_not_count_history_before_activation():
    h = _Harness(rows=[])
    for index in range(100):
        h.publish(facts={"index": index})
    h.drain()  # 插件未启用：不建槽，前 100 条只进最新表
    h.rows = [_installation()]  # 中途启用
    h.publish(facts={"index": 100})
    h.drain()
    first = h.events()[0]
    assert first["facts"] == {"index": 100}
    assert first["dropped_before"] == 0, "把启用之前的发布算进了 dropped_before"
    assert first["seq"] == 1
    for index in range(101, 106):  # 槽建立后连发 5 条：合并 4 条
        h.publish(facts={"index": index})
    h.drain()
    latest = h.plugin.calls[1][1]["events"][0]
    assert latest["facts"] == {"index": 105}
    assert latest["dropped_before"] == 4
    assert h.stats()["delivered"] == 2 and h.stats()["coalesced"] == 4
    h.hub.close()


def test_revoked_activation_error_is_not_counted_as_failure():
    h = _Harness(on_create=lambda plugin: setattr(plugin, "fail", PluginChannelRevoked()))
    h.publish(facts={"n": 1})
    h.drain()
    assert h.stats()["failed"] == 0 and h.stats()["delivered"] == 0
    h.hub.close()


def test_recycle_keeps_connections_created_by_other_callers():
    executor = _ManualExecutor()
    pool = PluginChannelPool(client_factory=lambda _owner, installation: _FakePlugin(installation),
                             clock=lambda: 100.0)
    other = _installation(plugin_id="panel-x", activation_id="act-x")
    state = {"hook": False}

    def installations(_owner):
        if state["hook"]:
            # 读表期间：别的调用方（面板服务）为另一个新启用激活建了连接，不在 hub 的 rows 里
            pool.acquire(OWNER_A, other, 100.0)
        return (_installation(),)

    hub = PluginEventHub(wiring=EventHubWiring(
        installations=installations, clock=lambda: 100.0, pool_clock=lambda: 100.0,
        executor=executor, pool=pool))
    try:
        hub.publish(_owner(), EventFact(type="tool_call_finished"))
        executor.run_all()  # 第一轮：hub 建了自己的连接（managed 非空）
        state["hook"] = True
        hub.publish(_owner(), EventFact(type="tool_call_finished"))
        executor.run_all()  # 第二轮：读表期间别的调用方建了 act-x 连接
        assert (OWNER_A, "act-x") in pool.owner_keys(OWNER_A), "hub 摘掉了别的调用方刚建的连接"
    finally:
        hub.close()


def test_recycle_keeps_expired_connections_owned_by_other_callers():
    executor = _ManualExecutor()
    pool = PluginChannelPool(client_factory=lambda _owner, installation: _FakePlugin(installation),
                             clock=lambda: 100.0)
    old = _installation(plugin_id="panel-old", activation_id="act-old")
    pool.acquire(OWNER_A, old, 100.0)  # 先建：一条不归 hub 管、激活已不在 hub rows 里的连接
    hub = PluginEventHub(wiring=EventHubWiring(
        installations=lambda _owner: (_installation(),), clock=lambda: 100.0, pool_clock=lambda: 100.0,
        executor=executor, pool=pool))
    try:
        hub.publish(_owner(), EventFact(type="tool_call_finished"))
        executor.run_all()
        hub.publish(_owner(), EventFact(type="tool_call_finished"))
        executor.run_all()
        assert (OWNER_A, "act-old") in pool.owner_keys(OWNER_A), "hub 摘掉了不归它管的过期连接"
    finally:
        hub.close()


def test_disabled_plugin_connection_is_recycled():
    h = _Harness()
    h.publish()
    h.drain()  # hub 建连接并记 managed
    assert (OWNER_A, "act-1") in h.pool.owner_keys(OWNER_A)
    h.rows = []  # 停用：rows 里不再有它
    h.publish()
    h.drain()
    assert (OWNER_A, "act-1") not in h.pool.owner_keys(OWNER_A), "停用后 hub 没回收自己的连接"
    assert h.plugins[0].stopped >= 1
    h.hub.close()


def test_hub_closes_idle_connections_each_round():
    h = _Harness()
    h.publish()
    h.drain()  # 建连接并发送
    assert h.plugins[0].stopped == 0
    h.now = 100.0 + IDLE_CLOSE_SECONDS + 1.0  # 拨到空闲超时之后
    h.publish()
    h.drain()  # 新一轮顺手关空闲连接
    assert h.plugins[0].stopped >= 1, "空闲连接没有被关闭"
    h.hub.close()


def test_idle_close_follows_pool_clock_not_wall_clock():
    clocks = {"pool": 50.0, "wall": 1000.0}
    executor = _ManualExecutor()
    plugins = []

    def factory(_owner, installation):
        plugin = _FakePlugin(installation)
        plugins.append(plugin)
        return plugin

    pool = PluginChannelPool(client_factory=factory, clock=lambda: clocks["pool"])
    hub = PluginEventHub(wiring=EventHubWiring(
        installations=lambda _owner: (_installation(),), clock=lambda: clocks["wall"],
        pool_clock=lambda: clocks["pool"], executor=executor, pool=pool))
    try:
        hub.publish(_owner(), EventFact(type="tool_call_finished"))
        executor.run_all()
        assert len(plugins) == 1 and plugins[0].stopped == 0
        clocks["pool"] += 50.0  # 池时钟只走了 50 秒（< 120）：不该关
        clocks["wall"] += 5000.0  # 墙钟走很多：拿墙钟判就会误关
        hub.publish(_owner(), EventFact(type="tool_call_finished"))
        executor.run_all()
        assert plugins[0].stopped == 0, "用墙钟判空闲，把没到空闲期的连接关了"
        clocks["pool"] = 100.0 + IDLE_CLOSE_SECONDS + 10.0  # 距上次使用 130 秒：该关
        clocks["wall"] += 5000.0
        hub.publish(_owner(), EventFact(type="tool_call_finished"))
        executor.run_all()
        assert plugins[0].stopped >= 1, "池时钟已过空闲期，连接没有关"
    finally:
        hub.close()


# ── 跨 owner 隔离 ───────────────────────────────────────────────────────


def test_cross_owner_events_never_reach_other_owner():
    tables = {"/tmp/owner-a": (_installation(plugin_id="watch-a", activation_id="act-a"),),
              "/tmp/owner-b": (_installation(plugin_id="watch-b", activation_id="act-b"),)}
    plugins = []

    def factory(_owner, installation):
        plugin = _FakePlugin(installation)
        plugins.append(plugin)
        return plugin

    executor = _ManualExecutor()
    pool = PluginChannelPool(client_factory=factory, clock=lambda: 100.0)
    hub = PluginEventHub(wiring=EventHubWiring(
        installations=lambda owner: tables[str(owner.home_dir)], clock=lambda: 100.0,
        executor=executor, pool=pool))
    try:
        hub.publish(_owner("/tmp/owner-a"), EventFact(type="tool_call_finished", facts={"from": "a"}))
        hub.publish(_owner("/tmp/owner-b"), EventFact(type="tool_call_finished", facts={"from": "b"}))
        executor.run_all()
        by_plugin = {plugin.installation.manifest.plugin_id: plugin for plugin in plugins}
        assert set(by_plugin) == {"watch-a", "watch-b"}
        assert len(by_plugin["watch-a"].calls) == 1 and len(by_plugin["watch-b"].calls) == 1
        assert by_plugin["watch-a"].calls[0][1]["events"][0]["facts"] == {"from": "a"}
        assert by_plugin["watch-b"].calls[0][1]["events"][0]["facts"] == {"from": "b"}
    finally:
        hub.close()


def test_seq_does_not_leak_across_owners():
    tables = {"/tmp/owner-a": (_installation(plugin_id="watch-a", activation_id="act-a"),),
              "/tmp/owner-b": (_installation(plugin_id="watch-b", activation_id="act-b"),)}
    plugins = []

    def factory(_owner, installation):
        plugin = _FakePlugin(installation)
        plugins.append(plugin)
        return plugin

    executor = _ManualExecutor()
    pool = PluginChannelPool(client_factory=factory, clock=lambda: 100.0)
    hub = PluginEventHub(wiring=EventHubWiring(
        installations=lambda owner: tables[str(owner.home_dir)], clock=lambda: 100.0,
        executor=executor, pool=pool))
    try:
        for n in (1, 2):
            hub.publish(_owner("/tmp/owner-a"), EventFact(type="tool_call_finished", facts={"n": n}))
            executor.run_all()
        hub.publish(_owner("/tmp/owner-b"), EventFact(type="tool_call_finished", facts={"n": 3}))
        executor.run_all()
        by_id = {plugin.installation.manifest.plugin_id: plugin for plugin in plugins}
        assert [event["seq"] for event in by_id["watch-a"].calls[0][1]["events"]] == [1]
        assert [event["seq"] for event in by_id["watch-a"].calls[1][1]["events"]] == [2]
        assert [event["seq"] for event in by_id["watch-b"].calls[0][1]["events"]] == [1]
    finally:
        hub.close()


# ── 清单订阅与握手门 ────────────────────────────────────────────────────


def test_missing_handshake_capability_is_unavailable_and_not_delivered():
    h = _Harness(capable=False)
    h.publish()
    h.drain()
    assert h.plugin.calls == []
    assert h.stats()["unavailable"] == 1 and h.stats()["delivered"] == 0
    h.hub.close()


def test_unsubscribed_type_is_not_delivered():
    h = _Harness(rows=[_installation(event_types=("turn_started",))])
    h.publish("tool_call_finished")
    h.drain()
    assert h.plugins == []
    assert "tool_call_finished" not in h.hub.stats(OWNER_A).get("watch", {})
    h.hub.close()


def test_content_only_delivered_when_plugin_declared_text():
    h = _Harness(rows=[_installation(event_types=("prompt_submitted",), content="none")])
    h.publish("prompt_submitted", content="你好")
    h.drain()
    assert "content" not in h.events()[0]
    h.hub.close()


def test_prompt_content_delivered_when_declared_text():
    h = _Harness(rows=[_installation(event_types=("prompt_submitted",), content="text")])
    h.publish("prompt_submitted", content="你好")
    h.drain()
    assert h.events()[0]["content"] == "你好"
    h.hub.close()


# ── 退避分级（连接级 vs 请求级） ────────────────────────────────────────


def test_timeout_backs_off_whole_connection():
    h = _Harness(on_create=lambda plugin: setattr(plugin, "fail", PluginChannelTimeout("slow")))
    h.publish(facts={"n": 1})
    h.drain()
    assert len(h.plugin.calls) == 1
    assert h.stats()["failed"] == 1 and h.stats()["last_error_code"] == "MCP_TIMEOUT"
    h.publish(facts={"n": 2})
    h.drain()
    assert len(h.plugin.calls) == 1, "超时后没有退避，第二次请求又发到了插件"
    assert h.stats()["last_error_code"] == "PLUGIN_CHANNEL_BACKOFF"
    h.hub.close()


def test_start_failure_backs_off_whole_connection():
    h = _Harness(on_create=lambda plugin: setattr(plugin, "start_error", RuntimeError("boom")))
    h.publish(facts={"n": 1})
    h.drain()
    assert h.plugin.calls == []
    assert h.stats()["failed"] == 1 and h.stats()["last_error_code"] == "PLUGIN_CHANNEL_START_FAILED"
    h.publish(facts={"n": 2})
    h.drain()
    assert h.stats()["last_error_code"] == "PLUGIN_CHANNEL_BACKOFF"
    h.hub.close()


def test_request_level_error_does_not_back_off_connection():
    h = _Harness(on_create=lambda plugin: setattr(plugin, "fail", _RemoteError("bad request")))
    h.publish(facts={"n": 1})
    h.drain()
    assert len(h.plugin.calls) == 1
    assert h.stats()["failed"] == 1 and h.stats()["last_error_code"] == "MCP_REMOTE_ERROR"
    h.publish(facts={"n": 2})
    h.drain()
    assert len(h.plugin.calls) == 2, "请求级错误把整条连接退避了"
    h.hub.close()


# ── 计数接口 ────────────────────────────────────────────────────────────


def test_stats_exposes_all_counters():
    h = _Harness()
    h.publish()
    h.drain()
    stats = h.stats()
    assert set(stats) == {"delivered", "coalesced", "failed", "unavailable", "last_error_code",
                          "last_delivered_at"}
    assert stats["delivered"] == 1 and stats["last_delivered_at"] == 100.0
    assert h.hub.stats(OWNER_A)["watch"]["tool_call_finished"]["delivered"] == 1
    h.hub.close()


def test_publish_never_raises_on_bad_input():
    h = _Harness()
    h.hub.publish(_owner(), None)
    h.hub.publish(_owner(), SimpleNamespace(type="tool_call_finished"))
    h.hub.publish(_owner(), EventFact(type="unknown_type"))
    h.hub.publish(object(), EventFact(type="tool_call_finished"))
    h.drain()
    assert h.plugins == []
    h.hub.close()


# ── Gateway 组装 ────────────────────────────────────────────────────────


def _server():
    return SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(plugin_process_sandbox=False)))


def test_gateway_event_hub_is_single_instance_and_shares_pool_with_panels():
    from agent_py_agent.agent.gateway_parts import plugin_panels_http

    server = _server()
    hub = plugin_panels_http.plugin_event_hub(server)
    assert plugin_panels_http.plugin_event_hub(server) is hub
    service = plugin_panels_http.plugin_display_service(server)
    assert hub._pool is server.plugin_channel_pool and service._pool is server.plugin_channel_pool
    plugin_panels_http.close_plugin_channel(server)
    assert server.plugin_event_hub is None and server.plugin_channel_pool is None
    with pytest.raises(PluginChannelRevoked):
        plugin_panels_http.plugin_event_hub(server)


def test_publish_plugin_event_drops_after_channel_closed():
    from agent_py_agent.agent.gateway_parts import plugin_panels_http

    server = _server()
    plugin_panels_http.publish_plugin_event(server, _owner(), EventFact(type="tool_call_finished"))
    plugin_panels_http.close_plugin_channel(server)
    plugin_panels_http.publish_plugin_event(server, _owner(), EventFact(type="tool_call_finished"))


def test_publish_plugin_event_swallows_hub_build_errors(monkeypatch, caplog):
    from agent_py_agent.agent.gateway_parts import plugin_panels_http

    server = _server()

    def boom(_server):
        raise RuntimeError("hub build failed")

    monkeypatch.setattr(plugin_panels_http, "plugin_event_hub", boom)
    caplog.set_level(logging.WARNING, logger="agent_py_agent.agent.gateway_parts.plugin_panels_http")
    plugin_panels_http.publish_plugin_event(server, _owner(), EventFact(type="tool_call_finished"))
    assert any("插件事件中心取用失败" in record.getMessage() for record in caplog.records)


# 3a 补（ds2 初审 S2）：并发用例都注入自己的执行器，钉不住默认线程数；这里直接核对默认值。
# 默认按（owner, 激活）独立调度，线程数取 4（3a 2026-10-03 定的 a 方案），改小会让挂住的插件拖住别人。
def test_default_executor_has_four_workers() -> None:
    hub = PluginEventHub()
    try:
        assert hub._executor._max_workers == 4
    finally:
        hub.close()
