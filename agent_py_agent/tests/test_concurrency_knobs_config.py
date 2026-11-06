from __future__ import annotations

from concurrent.futures import Future
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_shutdown import BackgroundShutdown
from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.config import load_config, normalize_agent_config
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.agent.settings.user_config_capability import packaged_config_path
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli import gateway_loops
from agent_py_agent.cli.gateway_lane_retry import BackgroundLaneRetry
from agent_py_agent.cli.gateway_loops import (
    _background_owner_workers,
    _background_threads_per_owner,
)

# 防回归(T4·并发公平层): _BACKGROUND_OWNER_WORKER_COUNT=8 与 OwnerScopedAgentPool
# max_agents=64 曾是硬编码,千并发调参无入口。钉子:两值可由 config 覆盖,
# 非法/缺省值回落默认,显式入参优先；BG-ADMIN-16将全局默认提升为32。


def test_background_owner_workers_from_config():
    agent = SimpleNamespace(config=SimpleNamespace(background_owner_workers=24))
    assert _background_owner_workers(agent) == 24


def test_background_owner_workers_fallback_on_invalid():
    assert _background_owner_workers(SimpleNamespace(config=SimpleNamespace(background_owner_workers="x"))) == 32
    assert _background_owner_workers(SimpleNamespace(config=SimpleNamespace(background_owner_workers=0))) == 32
    assert _background_owner_workers(SimpleNamespace()) == 32


def test_background_threads_per_owner_from_config():
    agent = SimpleNamespace(config=SimpleNamespace(background_threads_per_owner=6))
    assert _background_threads_per_owner(agent) == 6


def test_background_threads_per_owner_fallback_on_invalid():
    assert _background_threads_per_owner(
        SimpleNamespace(config=SimpleNamespace(background_threads_per_owner="x"))
    ) == 4
    assert _background_threads_per_owner(
        SimpleNamespace(config=SimpleNamespace(background_threads_per_owner=0))
    ) == 4
    assert _background_threads_per_owner(SimpleNamespace()) == 4


def test_owner_pool_max_agents_from_config():
    config = SimpleNamespace(owner_agent_pool_max_agents=128)
    pool = OwnerScopedAgentPool(config, root=".")
    assert pool._max_agents == 128


def test_owner_pool_explicit_param_beats_config():
    config = SimpleNamespace(owner_agent_pool_max_agents=128)
    pool = OwnerScopedAgentPool(config, root=".", max_agents=4)
    assert pool._max_agents == 4


def test_owner_pool_fallback_on_missing_or_invalid():
    assert OwnerScopedAgentPool(SimpleNamespace(), root=".")._max_agents == 64
    assert OwnerScopedAgentPool(SimpleNamespace(owner_agent_pool_max_agents="junk"), root=".")._max_agents == 64
    assert OwnerScopedAgentPool(SimpleNamespace(owner_agent_pool_max_agents=-1), root=".")._max_agents == 64


def test_agent_config_declares_concurrency_fields():
    config = AgentConfig()
    assert config.background_owner_workers == 32
    assert config.background_threads_per_owner == 4
    assert config.owner_agent_pool_max_agents == 64


_BACKGROUND_DEFAULTS = {
    "background_owner_workers": 32,
    "background_threads_per_owner": 4,
    "background_admin_threads_per_owner": 16,
}


def test_admin_background_reader_uses_separate_config_and_default():
    reader = getattr(gateway_loops, "_background_admin_threads_per_owner", None)
    assert callable(reader)
    agent = SimpleNamespace(config=SimpleNamespace(
        background_threads_per_owner=2, background_admin_threads_per_owner=11,
    ))
    assert reader(agent) == 11
    assert _background_threads_per_owner(agent) == 2
    assert reader(SimpleNamespace()) == 16


@pytest.mark.parametrize("value", [None, 0, -1, "invalid"])
def test_admin_background_reader_falls_back_on_invalid(value):
    reader = getattr(gateway_loops, "_background_admin_threads_per_owner", None)
    assert callable(reader)
    agent = SimpleNamespace(config=SimpleNamespace(background_admin_threads_per_owner=value))
    assert reader(agent) == 16


@pytest.mark.parametrize("key,expected", _BACKGROUND_DEFAULTS.items())
def test_background_defaults_match_yaml_loader_and_registry(key, expected):
    config = load_config(packaged_config_path())
    assert getattr(AgentConfig(), key, None) == expected
    assert getattr(config, key, None) == expected
    spec = parameter_registry().get(key)
    assert spec is not None
    assert spec.default == expected and spec.value_type == "int"
    assert spec.category == parameter_registry()["background_threads_per_owner"].category
    assert spec.range == "≥1" and spec.reader == "cli/gateway_loops"
    assert spec.tier == "deployment" and spec.effect == "restart_gateway"
    assert not spec.writable and not spec.user_editable
    assert any("\u4e00" <= char <= "\u9fff" for char in spec.description)


@pytest.mark.parametrize("key,expected", _BACKGROUND_DEFAULTS.items())
@pytest.mark.parametrize("bad", [0, -1, "invalid", True])
def test_background_concurrency_normalization_rejects_invalid(key, expected, bad):
    out, warnings = normalize_agent_config({key: bad})
    assert out.get(key) == expected and type(out[key]) is int
    assert any(key in warning for warning in warnings)


@pytest.mark.parametrize("key", _BACKGROUND_DEFAULTS)
def test_background_concurrency_normalization_accepts_positive_strings(key):
    out, warnings = normalize_agent_config({key: "7"})
    assert out.get(key) == 7 and type(out[key]) is int
    assert not warnings


class _ReadyBackgroundScheduler:
    """只代替就绪来源；规划、扣槽、公平提交和执行器仍走产品入口。"""

    def __init__(self, config, count=40):
        self.runtime = SimpleNamespace(agent=SimpleNamespace(config=config))
        self.rows = tuple(f"thread-{index}" for index in range(count))
        self.limits = []

    def prepare_tick(self, *, now, include_orphan_supervision):
        assert include_orphan_supervision is False

    def ready_thread_ids(self, *, now, limit):
        self.limits.append(limit)
        return self.rows[:limit]


class _PendingBackgroundExecutor:
    """保留未完成future，避免测试速度替调度器释放在途槽位。"""

    def submit(self, *_args):
        return Future()


@contextmanager
def _background_lane_plan(config, owners, *, active=None, base_count=40):
    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = SimpleNamespace(config=config)
    supervisor._base_scheduler = _ReadyBackgroundScheduler(config, base_count)
    supervisor._owner_schedulers = {key: _ReadyBackgroundScheduler(config) for key in owners}
    supervisor._inflight = dict(active or {})
    supervisor._executor = _PendingBackgroundExecutor()
    supervisor._owner_pool = None
    supervisor._shutdown_control = BackgroundShutdown()
    supervisor._shutdown_control.bind(supervisor)
    supervisor._lane_retry = BackgroundLaneRetry()
    try:
        yield supervisor
    finally:
        supervisor._retain_owner_planner_scopes(set())
        for future in supervisor._inflight.values():
            future.cancel()


def test_background_lane_defaults_give_admin_16_and_each_ordinary_owner_4():
    owners = (
        OwnerIdentity.local_user("admin"),
        OwnerIdentity.provider_user("feishu", "main"),
        OwnerIdentity.provider_group("feishu", "admin"),
        OwnerIdentity.provider_user("qq", "ordinary"),
    )
    with _background_lane_plan(AgentConfig(), owners) as supervisor:
        supervisor._submit_ready_thread_ticks()
        assert len(supervisor._active_threads_for_owner(gateway_loops._BASE_SCHEDULER_KEY)) == 16
        assert [len(supervisor._active_threads_for_owner(owner)) for owner in owners] == [4, 4, 4, 4]
        assert len(supervisor._inflight) == 32
        before = dict(supervisor._inflight)
        supervisor._submit_ready_thread_ticks()
        assert supervisor._inflight == before


def test_background_lane_custom_limits_and_inflight_threads_are_independent():
    owner = OwnerIdentity.provider_user("feishu", "ordinary")
    config = SimpleNamespace(background_owner_workers=32, background_threads_per_owner=2,
                             background_admin_threads_per_owner=7)
    base = gateway_loops._BASE_SCHEDULER_KEY
    active = {(base, f"thread-{index}"): Future() for index in range(6)}
    active[(owner, "thread-0")] = Future()
    with _background_lane_plan(config, (owner,), active=active) as supervisor:
        supervisor._submit_ready_thread_ticks()
        assert len(supervisor._active_threads_for_owner(base)) == 7
        assert len(supervisor._active_threads_for_owner(owner)) == 2
        assert supervisor._base_scheduler.limits == [13]
        assert supervisor._owner_schedulers[owner].limits == [3]
        assert all(supervisor._inflight[key] is future for key, future in active.items())
        before = dict(supervisor._inflight)
        supervisor._submit_ready_thread_ticks()
        assert supervisor._inflight == before


def test_background_lane_explicit_main_owner_uses_admin_limit():
    owner = OwnerIdentity.local_main()
    with _background_lane_plan(AgentConfig(), (owner,), base_count=0) as supervisor:
        supervisor._submit_ready_thread_ticks()
        assert len(supervisor._active_threads_for_owner(owner)) == 16
        assert supervisor._owner_schedulers[owner].limits == [16]


@pytest.mark.parametrize("workers,expected_admin,expected_user", [(1, 1, 0), (6, 3, 3)])
def test_background_lanes_keep_global_limit_and_round_robin(workers, expected_admin, expected_user):
    owner = OwnerIdentity.local_user("ordinary")
    config = SimpleNamespace(background_owner_workers=workers, background_threads_per_owner=4,
                             background_admin_threads_per_owner=16)
    with _background_lane_plan(config, (owner,)) as supervisor:
        supervisor._submit_ready_thread_ticks()
        assert len(supervisor._inflight) == workers
        assert len(supervisor._active_threads_for_owner(gateway_loops._BASE_SCHEDULER_KEY)) == expected_admin
        assert len(supervisor._active_threads_for_owner(owner)) == expected_user


def test_background_lane_full_owner_does_not_prevent_sibling_owner_submission():
    owner = OwnerIdentity.local_user("ordinary")
    base = gateway_loops._BASE_SCHEDULER_KEY
    active = {(base, f"thread-{index}"): Future() for index in range(16)}
    with _background_lane_plan(AgentConfig(), (owner,), active=active) as supervisor:
        supervisor._submit_ready_thread_ticks()
        assert len(supervisor._active_threads_for_owner(base)) == 16
        assert len(supervisor._active_threads_for_owner(owner)) == 4
        assert supervisor._base_scheduler.limits == []


def test_background_lanes_do_not_submit_after_stopping():
    with _background_lane_plan(AgentConfig(), (OwnerIdentity.local_user("ordinary"),)) as supervisor:
        supervisor._shutdown_control.stop()
        supervisor._submit_ready_thread_ticks()
        assert supervisor._inflight == {}


def test_background_executor_default_can_run_admin_16_and_ordinary_4_together():
    import threading

    owner = OwnerIdentity.local_user("ordinary")
    release = threading.Event()
    entered = threading.Condition()
    started = []

    def blocked_tick(_scheduler, label, thread_id):
        with entered:
            started.append((label, thread_id))
            entered.notify_all()
        assert release.wait(10)
        return []

    with _background_lane_plan(AgentConfig(), (owner,)) as supervisor:
        supervisor._executor = None
        supervisor._counted_thread_tick = blocked_tick
        executor = supervisor._get_executor()
        try:
            assert executor._max_workers == 32
            supervisor._submit_ready_thread_ticks()
            with entered:
                assert entered.wait_for(lambda: len(started) == 20, timeout=3)
                assert sum(label == gateway_loops._BASE_SCHEDULER_KEY for label, _thread in started) == 16
                assert sum(label == str(owner) for label, _thread in started) == 4
            assert len(supervisor._inflight) == 20
        finally:
            release.set()
            executor.shutdown(wait=True, cancel_futures=True)
        assert all(future.result() == [] for future in supervisor._inflight.values())
