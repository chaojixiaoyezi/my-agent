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
# 非法/缺省值回落默认,显式入参优先；GW-PRESETS-12统一为全局128/管理员64/普通用户2。


def test_background_owner_workers_from_config():
    agent = SimpleNamespace(config=SimpleNamespace(background_owner_workers=24))
    assert _background_owner_workers(agent) == 24


# LLM: 沿原非法值回默认合同，仅更新出厂容量，不启动后台工作。
# 函数用途: 验证全局后台线程数缺失或无效时回落128。
def test_background_owner_workers_fallback_on_invalid():
    assert _background_owner_workers(SimpleNamespace(config=SimpleNamespace(background_owner_workers="x"))) == 128
    assert _background_owner_workers(SimpleNamespace(config=SimpleNamespace(background_owner_workers=0))) == 128
    assert _background_owner_workers(SimpleNamespace()) == 128


def test_background_threads_per_owner_from_config():
    agent = SimpleNamespace(config=SimpleNamespace(background_threads_per_owner=6))
    assert _background_threads_per_owner(agent) == 6


# LLM: 保留原缺失/零/非法值的兜底约束，不删除负向断言。
# 函数用途: 验证普通owner后台并发回落新默认2。
def test_background_threads_per_owner_fallback_on_invalid():
    assert _background_threads_per_owner(
        SimpleNamespace(config=SimpleNamespace(background_threads_per_owner="x"))
    ) == 2
    assert _background_threads_per_owner(
        SimpleNamespace(config=SimpleNamespace(background_threads_per_owner=0))
    ) == 2
    assert _background_threads_per_owner(SimpleNamespace()) == 2


def test_owner_pool_max_agents_from_config():
    config = SimpleNamespace(owner_agent_pool_max_agents=128)
    pool = OwnerScopedAgentPool(config, root=".")
    assert pool._max_agents == 128


def test_owner_pool_explicit_param_beats_config():
    config = SimpleNamespace(owner_agent_pool_max_agents=128)
    pool = OwnerScopedAgentPool(config, root=".", max_agents=4)
    assert pool._max_agents == 4


# LLM: 不构造真实Agent，保留Owner池对无效输入的容量兜底。
# 函数用途: 验证实例池缺失和非法值使用新默认128。
def test_owner_pool_fallback_on_missing_or_invalid():
    assert OwnerScopedAgentPool(SimpleNamespace(), root=".")._max_agents == 128
    assert OwnerScopedAgentPool(SimpleNamespace(owner_agent_pool_max_agents="junk"), root=".")._max_agents == 128
    assert OwnerScopedAgentPool(SimpleNamespace(owner_agent_pool_max_agents=-1), root=".")._max_agents == 128


# LLM: 直接构造配置，不依赖模板或测试环境覆盖。
# 函数用途: 检查后台与Owner池出厂容量声明。
def test_agent_config_declares_concurrency_fields():
    config = AgentConfig()
    assert config.background_owner_workers == 128
    assert config.background_threads_per_owner == 2
    assert config.owner_agent_pool_max_agents == 128


_BACKGROUND_DEFAULTS = {
    "background_owner_workers": 128,
    "background_threads_per_owner": 2,
    "background_admin_threads_per_owner": 64,
}

_GATEWAY_PRESETS = {
    "gateway_http_listen_backlog": 128,
    "gateway_http_max_request_workers": 32,
    "gateway_http_max_outstanding_requests": 256,
    "gateway_http_control_lane_workers": 10,
    "gateway_native_stream_max": 128,
    "gateway_user_inflight_limit": 128,
    "gateway_global_inflight_limit": 500,
    "owner_agent_pool_max_agents": 128,
    "max_subagents": 8,
    **_BACKGROUND_DEFAULTS,
}


# LLM: 锁定本轮十二项出厂值的三条公开来源，不修改机器配置或启动服务。
# 函数用途: 防止只改模板、只改构造或登记表展示与实际加载脱节。
@pytest.mark.parametrize("key,expected", _GATEWAY_PRESETS.items())
def test_gw12_presets_match_yaml_constructor_and_registry(key, expected):
    loaded = load_config(packaged_config_path())
    assert getattr(AgentConfig(), key, None) == expected
    assert getattr(loaded, key, None) == expected
    spec = parameter_registry()[key]
    assert spec.default == expected
    assert any("\u4e00" <= char <= "\u9fff" for char in spec.description)
    assert loaded.gateway_owner_inflight_limit == 0


# LLM: 校验构造、归一与管理员参数入口的相同范围，0是禁用而非无限；不会执行重启。
# 函数用途: 两条新容量键必须可发现、可合法保存且如实声明重启后生效。
@pytest.mark.parametrize("key,default,upper", [
    ("gateway_http_control_lane_workers", 10, 128),
    ("gateway_native_stream_max", 128, 4096),
])
def test_gw12_new_pools_share_bounds_and_restart_contract(key, default, upper):
    from agent_py_agent.agent.settings.parameter_metadata import ranges
    from agent_py_agent.agent.settings.parameter_registry import applied_value
    from agent_py_agent.agent.settings.user_config_capability import TUNABLE_KEYS

    spec = parameter_registry()[key]
    assert spec.value_type == "int" and spec.default == default
    assert spec.category == "Gateway 与调度" and spec.effect == "restart_gateway"
    assert spec.range == ranges()[key] == f"0-{upper}"
    assert spec.user_editable and key in TUNABLE_KEYS
    for valid in (0, 1, upper):
        out, warnings = normalize_agent_config({key: valid})
        assert out[key] == valid and not warnings
        config = AgentConfig(**{key: valid})
        assert applied_value(key, config)[0] == valid
        assert TUNABLE_KEYS[key].validate(str(valid)) == (True, "", valid)
    for invalid in (-1, upper + 1, True, "bad", 1.5):
        out, warnings = normalize_agent_config({key: invalid})
        assert out[key] == default and any(key in item for item in warnings)
        with pytest.raises(ValueError, match=key):
            AgentConfig(**{key: invalid})
        assert TUNABLE_KEYS[key].validate(invalid)[0] is False


# LLM: 拓扑只取宿主配置；零保留单进程，正整数原样保留，参数目录可见不等于模型或聊天可写。
# 函数用途: 锁住宿主拓扑默认、严格整数值与只读部署边界，不影响12项并发预设。
@pytest.mark.parametrize("value", [0, 1, 4, 17])
def test_gw12_process_count_is_host_topology(value):
    from agent_py_agent.agent.settings.config_io import load_simple_yaml
    from agent_py_agent.agent.settings.user_config_capability import TUNABLE_KEYS

    key = "gateway_worker_process_count"
    raw = load_simple_yaml(packaged_config_path())
    assert type(raw.get(key)) is int and raw[key] == 0
    spec = parameter_registry()[key]
    assert spec.default == 0 and spec.tier == "deployment"
    assert not spec.writable and not spec.user_editable and key not in TUNABLE_KEYS
    assert AgentConfig(gateway_worker_process_count=value).gateway_worker_process_count == value
    out, warnings = normalize_agent_config({key: value})
    assert out[key] == value and not warnings


# LLM: 构造与规范化各自拒绝非法拓扑，不能用归零或警告回退掩盖宿主配置错误。
# 函数用途: 验证布尔、负数、小数、字符串、空值及容器均不能选择部署拓扑。
@pytest.mark.parametrize("value", [True, False, -1, 1.5, "2", "invalid", None, []])
@pytest.mark.parametrize("entry", ["dataclass", "normalizer"])
def test_gw12_process_count_rejects_invalid_host_topology(value, entry):
    with pytest.raises(ValueError, match="gateway_worker_process_count"):
        if entry == "dataclass":
            AgentConfig(gateway_worker_process_count=value)
        else:
            normalize_agent_config({"gateway_worker_process_count": value})


# LLM: 显式管理员覆盖仍独立于普通owner上限，缺配置回统一默认。
# 函数用途: 验证两类后台车道不混用容量。
def test_admin_background_reader_uses_separate_config_and_default():
    reader = getattr(gateway_loops, "_background_admin_threads_per_owner", None)
    assert callable(reader)
    agent = SimpleNamespace(config=SimpleNamespace(
        background_threads_per_owner=2, background_admin_threads_per_owner=11,
    ))
    assert reader(agent) == 11
    assert _background_threads_per_owner(agent) == 2
    assert reader(SimpleNamespace()) == 64


# LLM: 非法输入不能绕过管理员线程预算，不启动线程。
# 函数用途: 检查管理员并发回退64。
@pytest.mark.parametrize("value", [None, 0, -1, "invalid"])
def test_admin_background_reader_falls_back_on_invalid(value):
    reader = getattr(gateway_loops, "_background_admin_threads_per_owner", None)
    assert callable(reader)
    agent = SimpleNamespace(config=SimpleNamespace(background_admin_threads_per_owner=value))
    assert reader(agent) == 64


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

    # LLM: 足量就绪信号覆盖64管理员新预算，只替代就绪来源。
    # 函数用途: 提供足量候选，避免错误的测试数据天花板。
    def __init__(self, config, count=80):
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


# LLM: 规划/扣槽仍使用产品代码，退出回收测试Future；足量候选覆盖新出厂值。
# 函数用途: 搭建可观察真实并发准入的后台调度器。
@contextmanager
def _background_lane_plan(config, owners, *, active=None, base_count=80):
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


# LLM: 保留跨多个普通身份的精确计数和重复提交不增量断言。
# 函数用途: 验证管理员64与四个普通owner各2可并发。
def test_background_lane_defaults_give_admin_64_and_each_ordinary_owner_2():
    owners = (
        OwnerIdentity.local_user("admin"),
        OwnerIdentity.provider_user("feishu", "main"),
        OwnerIdentity.provider_group("feishu", "admin"),
        OwnerIdentity.provider_user("qq", "ordinary"),
    )
    with _background_lane_plan(AgentConfig(), owners) as supervisor:
        supervisor._submit_ready_thread_ticks()
        assert len(supervisor._active_threads_for_owner(gateway_loops._BASE_SCHEDULER_KEY)) == 64
        assert [len(supervisor._active_threads_for_owner(owner)) for owner in owners] == [2, 2, 2, 2]
        assert len(supervisor._inflight) == 72
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


# LLM: local/main身份仍走管理员通道而非普通owner限额。
# 函数用途: 验证显式主Owner可用64个后台名额。
def test_background_lane_explicit_main_owner_uses_admin_limit():
    owner = OwnerIdentity.local_main()
    with _background_lane_plan(AgentConfig(), (owner,), base_count=0) as supervisor:
        supervisor._submit_ready_thread_ticks()
        assert len(supervisor._active_threads_for_owner(owner)) == 64
        assert supervisor._owner_schedulers[owner].limits == [64]


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


# LLM: 用真正已满的新管理员预算维持原隔离回归，不减少被测饱和度。
# 函数用途: 管理员占满64后普通owner仍可提交2个后台回合。
def test_background_lane_full_owner_does_not_prevent_sibling_owner_submission():
    owner = OwnerIdentity.local_user("ordinary")
    base = gateway_loops._BASE_SCHEDULER_KEY
    active = {(base, f"thread-{index}"): Future() for index in range(64)}
    with _background_lane_plan(AgentConfig(), (owner,), active=active) as supervisor:
        supervisor._submit_ready_thread_ticks()
        assert len(supervisor._active_threads_for_owner(base)) == 64
        assert len(supervisor._active_threads_for_owner(owner)) == 2
        assert supervisor._base_scheduler.limits == []


def test_background_lanes_do_not_submit_after_stopping():
    with _background_lane_plan(AgentConfig(), (OwnerIdentity.local_user("ordinary"),)) as supervisor:
        supervisor._shutdown_control.stop()
        supervisor._submit_ready_thread_ticks()
        assert supervisor._inflight == {}


# LLM: 原真实执行器与同步屏障保留，不用mock掩盖线程容量不足。
# 函数用途: 验证128全局池能同时承接管理员64和普通owner2。
def test_background_executor_default_can_run_admin_64_and_ordinary_2_together():
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
            assert executor._max_workers == 128
            supervisor._submit_ready_thread_ticks()
            with entered:
                assert entered.wait_for(lambda: len(started) == 66, timeout=3)
                assert sum(label == gateway_loops._BASE_SCHEDULER_KEY for label, _thread in started) == 64
                assert sum(label == str(owner) for label, _thread in started) == 2
            assert len(supervisor._inflight) == 66
        finally:
            release.set()
            executor.shutdown(wait=True, cancel_futures=True)
        assert all(future.result() == [] for future in supervisor._inflight.values())


# LLM: 临时home/随机回环端口启动真实Gateway，不调模型；保存参数不冒充热更新，finally回收资源。
# 函数用途: 检查出厂容量经Agent重建后真正启动独立车道和流池，显式零值重启后关闭。
def test_gw12_default_pools_start_and_explicit_zero_disables_after_restart(tmp_path, monkeypatch):
    from http.client import HTTPConnection

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    config = AgentConfig(model_backend="echo", enable_tools=False, memory_path="memory.jsonl")
    agent = SimpleAgent(config, tmp_path / "workspace")
    assert agent.config.gateway_http_control_lane_workers == 10
    assert agent.config.gateway_native_stream_max == 128
    service = GatewayHTTPServer(0, gateway_paths_from_root(agent.home_paths.root / "gateway"),
                                params=GatewayHTTPServerParams(agent=agent))
    try:
        service.start()
        server = service.server
        assert server._request_slots._initial_value == 256
        lane = server._lanes
        pool = service.business_stream_pool
        assert lane is not None and lane._control_executor._max_workers == 10
        # 完整普通请求的保留线程属于同一份 32 线程配额，控制池仍独立。
        assert lane._ready_executor._max_workers == 1
        assert server._request_executor._max_workers + lane._ready_executor._max_workers == server.max_request_workers == 32
        assert pool is not None and pool.max_streams == pool._executor._max_workers == 128
        for disabled in (False, True):
            connection = HTTPConnection("127.0.0.1", service.server.server_port, timeout=2)
            try:
                connection.request("GET", "/status")
                response = connection.getresponse()
                assert response.status == 200
                response.read()
            finally:
                connection.close()
            if not disabled:
                agent.config.gateway_http_control_lane_workers = 0
                agent.config.gateway_native_stream_max = 0
                assert service.server._lanes is lane and service.business_stream_pool is pool
                service.stop()
                service.start()
                assert service.server._lanes is None
                assert service.server._request_executor._max_workers == 32
                assert service.business_stream_pool is None
    finally:
        service.stop()
