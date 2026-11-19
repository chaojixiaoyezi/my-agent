"""MAX1-C9：冷 owner 构建不再在单线程后台拍内同步执行。

一次冷构建可能耗时数十秒（首次 schema/目录导入与构造槽位排队）；后台拍只把它交给有界预热池、
等待有界秒数，热 owner 的调度器同步与本拍其余阶段不再被拖住。构建完成后下一拍 peek 命中即接上调度器。
R5（F06/F07）：受管 owner 同样以只读 peek 判冷热、冷构建走同一预热池；预热登记为停机执行片，
停机与重启暂停能看到在途构建，门关之后的预热不交接、不构建、不发布。
R2（RC3/RC4）：peek 命中后的热分支只取现成可用实例；首查后换代或会话目录缺失即 miss，重建与目录自愈
走同一预热池，调度线程不构建、不等在途构造，本拍不为已知失效实例建调度器。
"""
from __future__ import annotations

import contextvars
import shutil
import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_shutdown import BackgroundShutdown
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import managed_owner_runtime
from agent_py_agent.agent.gateway_parts.managed_owner_runtime import managed_owner_pool_for_agent
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool, _missing_owner_home
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli import gateway_loops
from agent_py_agent.cli.models import GatewayRunContext

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
    # C7：预热构建登记为停机执行片，裸调度器同样要有自己的停机控制。
    supervisor._shutdown_control = BackgroundShutdown()
    supervisor._shutdown_control.bind(supervisor)
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


# ---- R5：真实受管池 + 真实普通池（共享名额）上的冷构建调度、停机可见与清理 ----

_MANAGED_USERS = ("alice", "bob", "carol", "dave")
_RUNNING = [{"reason": "background_slice_running", "ref": "submitted_slice"}]


class _ManagedBuilds:
    """包住真实受管构造：记并发峰值；冻结指定 owner 的首次构建（真实构造前或后），可令指定 owner 构造失败。"""

    def __init__(self, monkeypatch, *, freeze=(), after_build=False, fail=()):
        self.real = managed_owner_runtime.prepare_managed_owner_runtime
        self.freeze, self.after_build, self.fail = set(freeze), after_build, set(fail)
        self.entered, self.release, self.lock = threading.Event(), threading.Event(), threading.Lock()
        self.calls, self.active, self.peak, self.frozen, self.threads = [], 0, 0, set(), []
        monkeypatch.setattr(managed_owner_runtime, "prepare_managed_owner_runtime", self)

    def __call__(self, root, owner, **kwargs):
        with self.lock:
            self.calls.append(owner.owner_id)
            self.threads.append(threading.current_thread())
            self.active += 1
            self.peak = max(self.peak, self.active)
            freeze = owner.owner_id in self.freeze and owner.owner_id not in self.frozen
            if freeze:
                self.frozen.add(owner.owner_id)
            everyone_frozen = self.frozen == self.freeze
        try:
            if owner.owner_id in self.fail:
                raise RuntimeError("managed constructor failed")
            if freeze and not self.after_build:
                self._hold(everyone_frozen)
            agent = self.real(root, owner, **kwargs)
            if freeze and self.after_build:
                self._hold(everyone_frozen)
            return agent
        finally:
            with self.lock:
                self.active -= 1

    def _hold(self, everyone_frozen):
        if everyone_frozen:
            self.entered.set()
        assert self.release.wait(15), "测试放行超时"


@pytest.fixture
def managed_host(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_loops, "_build_background_scheduler",
                        lambda scoped_agent, _channels: SimpleNamespace(agent=scoped_agent))
    root = (tmp_path / "host").resolve()
    lifecycle = LocalUserLifecycle(LocalUserStore(root))
    for name in _MANAGED_USERS:
        lifecycle.create(name, idempotency_key=f"managed_{name}_0001")
    config = AgentConfig(enable_tools=False, memory_path="memory.jsonl", my_agent_home=str(root), model_backend="echo",
                         api_key="deployment-secret", access_mode="full-access", path_access_mode="full",
                         additional_write_roots=[str(tmp_path)], gateway_per_user_owner_scoping=True,
                         owner_agent_idle_seconds=3600)
    base = SimpleAgent(config, tmp_path / "workspace", defer_mcp_connect=True)
    supervisor = gateway_loops._BackgroundMainSupervisor(
        GatewayRunContext(agent=base, paths=SimpleNamespace(), config_path=tmp_path / "config.yaml"))
    host = SimpleNamespace(lifecycle=lifecycle, supervisor=supervisor, managed=managed_owner_pool_for_agent(base),
                           owners={name: OwnerIdentity.local_user(name) for name in _MANAGED_USERS})
    try:
        yield host
    finally:
        supervisor.shutdown()


def _schedule(host, *names):
    owners = [host.owners[name] for name in names]
    host.supervisor._registry = SimpleNamespace(snapshot=lambda: owners, hard_snapshot=lambda: owners)
    return host.supervisor


def _scheduled(host, name) -> bool:
    agent = host.managed.peek(host.owners[name])
    return agent is not None and id(agent) in host.supervisor._owner_schedulers


def _tick_until(supervisor, predicate, timeout=30.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        supervisor._sync_owner_schedulers()
    return True


def test_managed_cold_builds_leave_the_tick_and_share_two_slots(managed_host, monkeypatch):
    host = managed_host
    hot = host.managed.get(host.owners["bob"])  # 热 owner：装冻结钩子之前真实构建
    builds = _ManagedBuilds(monkeypatch, freeze={"alice"})
    supervisor = _schedule(host, "alice", "carol", "dave", "bob")
    try:
        started = time.monotonic()
        supervisor._sync_owner_schedulers()
        # 冻结的受管冷构建不占后台拍：本拍只等有界秒数，热 owner 在放行前已同步出调度器。
        assert time.monotonic() - started < gateway_loops._COLD_OWNER_WARMUP_WAIT_SECONDS + 5.0
        assert builds.entered.wait(10)
        assert id(hot) in supervisor._owner_schedulers and not _scheduled(host, "alice")
        # 冻结期间其余冷 owner 轮转接上：受管与普通共用两槽，同一 owner 不重复提交。
        assert _tick_until(supervisor, lambda: _scheduled(host, "carol") and _scheduled(host, "dave"))
        assert builds.calls.count("alice") == 1 and not _scheduled(host, "alice")
    finally:
        builds.release.set()
    assert _tick_until(supervisor, lambda: _scheduled(host, "alice"))
    assert sorted(builds.calls) == ["alice", "carol", "dave"] and builds.peak <= 2
    assert host.managed.gauges() == {"cached": 4, "building": 0, "pinned": 0}
    control = supervisor._shutdown_control
    assert control.blockers() == [] and control.active_slice_count() == 0 and not control.slices


@pytest.mark.parametrize("change", ["generation", "deactivated"])
def test_stale_managed_build_never_reaches_a_scheduler(managed_host, monkeypatch, change):
    host = managed_host
    builds = _ManagedBuilds(monkeypatch, freeze={"alice"}, after_build=True)
    supervisor = _schedule(host, "alice")
    try:
        supervisor._sync_owner_schedulers()
        assert builds.entered.wait(15)
        [future] = supervisor._owner_warmups.values()
        # 实例已按旧权威构造完毕、尚未发布进池时，账号停用或换代。
        host.lifecycle.disable("alice", idempotency_key="disable_alice_0001")
        if change == "generation":
            host.lifecycle.enable("alice", idempotency_key="enable_alice_00001")
        supervisor._sync_owner_schedulers()
        assert not supervisor._owner_schedulers
    finally:
        builds.release.set()
    stale = future.result(timeout=15)
    for _ in range(3):
        supervisor._sync_owner_schedulers()
        assert id(stale) not in supervisor._owner_schedulers
        assert stale not in host.managed.hard_agents()
    if change == "generation":
        assert _tick_until(supervisor, lambda: _scheduled(host, "alice"))
        fresh = host.managed.peek(host.owners["alice"])
        assert fresh is not stale
        assert fresh._managed_owner_generation.key != stale._managed_owner_generation.key
    else:
        assert not supervisor._owner_schedulers and host.managed.peek(host.owners["alice"]) is None
    assert host.managed.gauges()["building"] == 0 and host.managed.gauges()["pinned"] == 0


@pytest.mark.parametrize("gate", ["shutdown", "pause"])
def test_running_managed_warmup_is_visible_to_shutdown_and_restart_pause(managed_host, monkeypatch, gate):
    host = managed_host
    builds = _ManagedBuilds(monkeypatch, freeze={"alice"})
    supervisor = _schedule(host, "alice")
    control = supervisor._shutdown_control
    try:
        supervisor._sync_owner_schedulers()
        assert builds.entered.wait(15)
        [future] = supervisor._owner_warmups.values()
        if gate == "shutdown":
            supervisor.shutdown()  # 两秒绝对预算内等不到在途构建结束，未结如实保留
            assert control.blockers() == _RUNNING
        else:
            assert control.pause_for_restart() == _RUNNING
            assert control.restart_paused is False  # 未结即撤销暂停，不留半暂停
    finally:
        builds.release.set()
    assert future.result(timeout=15) is host.managed.peek(host.owners["alice"])
    assert control.blockers() == [] and control.active_slice_count() == 0 and not control.slices
    if gate == "pause":
        assert control.pause_for_restart() == []
        control.resume_after_restart_cancel()
    assert host.managed.gauges()["pinned"] == 0


@pytest.mark.parametrize("gate", ["shutdown", "pause"])
def test_warmup_after_gate_closes_reconciles_builds_and_publishes_nothing(managed_host, monkeypatch, gate):
    host = managed_host
    builds = _ManagedBuilds(monkeypatch)
    reconciles, errors = [], []
    real_reconcile = host.managed.reconcile
    monkeypatch.setattr(host.managed, "reconcile",
                        lambda owner, **kwargs: reconciles.append(owner.owner_id) or real_reconcile(owner, **kwargs))
    monkeypatch.setattr(gateway_loops, "_print_gateway_loop_error",
                        lambda context, label, exc: errors.append((context, label, type(exc).__name__)))
    supervisor = _schedule(host, "alice")
    control = supervisor._shutdown_control
    if gate == "shutdown":
        control.stop()
    else:
        assert control.pause_for_restart() == []
    supervisor._sync_owner_schedulers()
    assert _tick_until(supervisor, lambda: not supervisor._owner_warmups, timeout=10)
    # 门关之后预热在首个副作用前被拒：不交接、不构建、不发布，也不算构建失败。
    assert builds.calls == [] and reconciles == [] and errors == []
    assert host.managed.gauges() == {"cached": 0, "building": 0, "pinned": 0}
    assert not supervisor._owner_schedulers and not control.slices
    if gate == "pause":
        # 重启取消后 owner 仍是冷的，由后续拍重新提交。
        control.resume_after_restart_cancel()
        assert _tick_until(supervisor, lambda: _scheduled(host, "alice"))
        assert builds.calls == ["alice"] and reconciles == ["alice"]


def test_constructor_error_and_cancelled_queue_leave_no_pins_or_slices(managed_host, monkeypatch):
    host = managed_host
    errors = []
    monkeypatch.setattr(gateway_loops, "_print_gateway_loop_error",
                        lambda context, label, exc: errors.append((context, label, type(exc).__name__)))
    builds = _ManagedBuilds(monkeypatch, freeze={"alice", "bob"}, fail={"carol"})
    supervisor = _schedule(host, "carol")
    supervisor._sync_owner_schedulers()
    assert _tick_until(supervisor, lambda: errors, timeout=10)
    assert errors[0] == ("gateway_background_main.owner_build", "carol", "HostAPIError")
    control = supervisor._shutdown_control
    assert host.managed.gauges() == {"cached": 0, "building": 0, "pinned": 0} and not control.slices

    supervisor = _schedule(host, "alice", "bob")
    try:
        supervisor._sync_owner_schedulers()
        assert builds.entered.wait(15)  # 两个预热线程都被冻结的构建占住
        running = list(supervisor._owner_warmups.values())
        queued = supervisor._get_owner_warmup_executor().submit(
            supervisor._warm_owner, host.managed, host.owners["dave"], True)
        supervisor.shutdown()  # 撤销排队项；运行中的两片如实记为未结
        assert queued.cancelled()
        assert control.blockers() == _RUNNING * 2
    finally:
        builds.release.set()
    for future in running:
        future.result(timeout=15)
    assert "dave" not in builds.calls and builds.peak <= 2
    assert host.managed.gauges()["building"] == 0 and host.managed.gauges()["pinned"] == 0
    assert control.blockers() == [] and not control.slices


# ---- R2（RC3/RC4）：peek 命中后变冷或目录缺失，重建只在预热池；热 owner 不被拖住 ----

# 函数用途: 在独立线程跑一拍后台同步并有界等待；修复前该拍卡在同步构建里，回归以失败结束而不是挂住。
def _tick_off_thread(supervisor) -> threading.Thread:
    tick = threading.Thread(target=supervisor._sync_owner_schedulers, name="supervisor-tick")
    tick.start()
    tick.join(gateway_loops._COLD_OWNER_WARMUP_WAIT_SECONDS + 3.0)
    return tick


def test_managed_revision_change_after_peek_builds_on_the_warmup_pool(managed_host, monkeypatch):
    host = managed_host
    stale = host.managed.get(host.owners["alice"])
    hot = host.managed.get(host.owners["bob"])
    builds = _ManagedBuilds(monkeypatch, freeze={"alice"})
    real_peek, peeked = host.managed.peek, []

    def peek_then_revise(owner):
        found = real_peek(owner)
        if owner.owner_id == "alice" and not peeked:
            peeked.append(found)  # 首查命中旧代次后、同拍取用前，一次受支持的账号组修订使 Alice 换代
            host.lifecycle.set_audit_group_membership("alice", in_audit_group=True)
        return found

    monkeypatch.setattr(host.managed, "peek", peek_then_revise)
    supervisor = _schedule(host, "alice", "bob")
    tick = None
    try:
        tick = _tick_off_thread(supervisor)
        assert not tick.is_alive(), "首查后换代的受管构建仍在调度线程上"
        assert builds.entered.wait(10)
        # 独立热 owner Bob 在放行前已同步；已退役的旧代次不接调度器；构造只在预热线程。
        assert peeked == [stale] and id(hot) in supervisor._owner_schedulers
        assert id(stale) not in supervisor._owner_schedulers
        assert builds.threads and tick not in builds.threads
    finally:
        builds.release.set()
        if tick is not None:
            tick.join(15)
    assert _tick_until(supervisor, lambda: _scheduled(host, "alice"))
    fresh = host.managed.peek(host.owners["alice"])
    assert fresh is not stale and fresh._managed_owner_generation.key != stale._managed_owner_generation.key
    assert host.managed.gauges() == {"cached": 2, "building": 0, "pinned": 0}


# 函数用途: 用正式普通池建好两个 provider owner，再移走第一个的会话目录（合同支持的自愈场景）。
def _two_warm_provider_owners(tmp_path):
    pool = OwnerScopedAgentPool(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
                                tmp_path, max_agents=4)
    owners = [OwnerIdentity.provider_user("gw-r2", name) for name in ("alice", "bob")]
    agents = [pool.get(owner) for owner in owners]
    sessions = agents[0].conversation_store.storage.root
    shutil.move(str(sessions), str(tmp_path / "moved-sessions"))
    return pool, owners, agents, sessions


# 函数用途: 包住普通池的真实构造：记录调用与线程，按需冻结到放行。
def _freeze_pool_builder(pool, *, frozen=True):
    real = pool._builder
    state = SimpleNamespace(entered=threading.Event(), release=threading.Event(), calls=[], threads=[])
    if not frozen:
        state.release.set()

    def build(config, root, owner, roots):
        state.calls.append(owner.owner_id)
        state.threads.append(threading.current_thread())
        state.entered.set()
        assert state.release.wait(15), "测试放行超时"
        return real(config, root, owner, roots)

    pool._builder = build
    return state


def _healed(pool, owner, stale, supervisor) -> bool:
    agent = pool.peek(owner)
    return agent is not None and agent is not stale and id(agent) in supervisor._owner_schedulers


def _stub_scheduler(monkeypatch):
    monkeypatch.setattr(gateway_loops, "_build_background_scheduler",
                        lambda scoped_agent, _channels: SimpleNamespace(agent=scoped_agent))


def test_missing_session_dir_on_hot_owner_heals_on_the_warmup_pool(tmp_path, monkeypatch):
    _stub_scheduler(monkeypatch)
    pool, owners, (stale, hot), _sessions = _two_warm_provider_owners(tmp_path)
    builds = _freeze_pool_builder(pool)
    supervisor = _supervisor(pool, owners)
    tick = None
    try:
        tick = _tick_off_thread(supervisor)
        assert not tick.is_alive(), "缺目录自愈重建仍在调度线程上"
        assert builds.entered.wait(10)
        # 第二个热 owner 在重建放行前已同步；已知缺目录的旧实例本拍不接调度器。
        assert id(hot) in supervisor._owner_schedulers and id(stale) not in supervisor._owner_schedulers
        assert builds.threads and tick not in builds.threads
    finally:
        builds.release.set()
        if tick is not None:
            tick.join(15)
    assert _tick_until(supervisor, lambda: _healed(pool, owners[0], stale, supervisor))
    assert _missing_owner_home(pool.peek(owners[0])) is None and builds.calls == ["alice"]
    assert pool.pinned_count == 0 and not supervisor._owner_warmups
    supervisor._owner_warmup_executor.shutdown(wait=True)


@pytest.mark.parametrize("gate", ["shutdown", "pause"])
def test_running_missing_dir_warmup_is_visible_to_shutdown_and_restart_pause(tmp_path, monkeypatch, gate):
    _stub_scheduler(monkeypatch)
    pool, owners, (stale, _hot), _sessions = _two_warm_provider_owners(tmp_path)
    builds = _freeze_pool_builder(pool)
    supervisor = _supervisor(pool, owners)
    control = supervisor._shutdown_control
    tick = None
    try:
        tick = _tick_off_thread(supervisor)
        assert not tick.is_alive() and builds.entered.wait(10)
        [future] = supervisor._owner_warmups.values()
        if gate == "shutdown":
            control.stop()
            assert control.blockers() == _RUNNING
        else:
            assert control.pause_for_restart() == _RUNNING
            assert control.restart_paused is False
    finally:
        builds.release.set()
        if tick is not None:
            tick.join(15)
    healed = future.result(timeout=15)
    assert healed is not stale and _missing_owner_home(healed) is None
    assert control.blockers() == [] and control.active_slice_count() == 0 and not control.slices
    assert pool.pinned_count == 0
    supervisor._owner_warmup_executor.shutdown(wait=True)


@pytest.mark.parametrize("gate", ["shutdown", "pause"])
def test_missing_dir_after_gate_closes_repairs_builds_and_publishes_nothing(tmp_path, monkeypatch, gate):
    _stub_scheduler(monkeypatch)
    errors = []
    monkeypatch.setattr(gateway_loops, "_print_gateway_loop_error",
                        lambda context, label, exc: errors.append((context, label, type(exc).__name__)))
    pool, owners, (stale, hot), sessions = _two_warm_provider_owners(tmp_path)
    builds = _freeze_pool_builder(pool, frozen=False)
    supervisor = _supervisor(pool, owners)
    control = supervisor._shutdown_control
    if gate == "shutdown":
        control.stop()
    else:
        assert control.pause_for_restart() == []
    tick = _tick_off_thread(supervisor)
    assert not tick.is_alive()
    assert _tick_until(supervisor, lambda: not supervisor._owner_warmups, timeout=10)
    # 门关之后准入的预热在首个副作用前被拒：不修目录、不构造、不发布，也不算构建失败；独立热 owner 照常同步。
    assert builds.calls == [] and errors == [] and not sessions.exists()
    assert id(hot) in supervisor._owner_schedulers and id(stale) not in supervisor._owner_schedulers
    assert not control.slices and pool.pinned_count == 0
    if gate == "pause":
        control.resume_after_restart_cancel()
        assert _tick_until(supervisor, lambda: _healed(pool, owners[0], stale, supervisor))
        assert builds.calls == ["alice"] and _missing_owner_home(pool.peek(owners[0])) is None
    supervisor._owner_warmup_executor.shutdown(wait=True)


def test_managed_cached_only_acquire_never_waits_on_an_in_flight_build(managed_host, monkeypatch):
    host = managed_host
    owner = host.owners["carol"]
    builds = _ManagedBuilds(monkeypatch, freeze={"carol"})
    builder = threading.Thread(target=lambda: host.managed.get(owner))
    builder.start()
    try:
        assert builds.entered.wait(15)
        started = time.monotonic()
        # 同代次构造在途时只取缓存：立即 miss，不腾位、不构造、不等构造 Future。
        assert host.managed.get_cached_only(owner) is None
        assert time.monotonic() - started < 2.0 and host.managed.gauges()["building"] == 1
    finally:
        builds.release.set()
        builder.join(15)
    agent = host.managed.get_cached_only(owner)
    assert agent is not None and agent is host.managed.peek(owner)
    assert host.managed.gauges() == {"cached": 1, "building": 0, "pinned": 0}
