"""参数 W2：验证默认来源唯一、显式覆盖和池容量边界，不构造真实 Agent。"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import owner_scoped_pool as owner_pool
from agent_py_agent.agent.gateway_parts import bounded_http_server as bounded
from agent_py_agent.agent.gateway_parts import http_service, request_http_client
from agent_py_agent.agent.gateway_parts import logging as gateway_logging
from agent_py_agent.agent.gateway_parts import multiprocess_curator_accounting as accounting
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli import gateway_loops


# LLM: 用原模块文件在同包中独立加载，临时模块由monkeypatch回收，不reload已服务其它测试的对象。
# 函数用途: 在不创建线程或Agent的情况下观察模块导入时捕获的配置默认值。
def _load_consumer(monkeypatch, original, suffix):
    name = original.__package__ + "._param_w2_" + suffix
    spec = importlib.util.spec_from_file_location(name, Path(original.__file__))
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


# LLM: 同时改变类默认及dataclass字段，验证消费者真正引用权威默认而非恰好等于当前字面量。
# 函数用途: 反证三处兜底若仍写死64/8/4就会忽略更新后的AgentConfig默认。
@pytest.mark.parametrize(
    ("source", "module", "constant", "replacement"),
    [
        ("owner_agent_pool_max_agents", owner_pool, "_DEFAULT_MAX_AGENT_COUNT", 17),
        ("background_owner_workers", gateway_loops, "_BACKGROUND_OWNER_WORKER_COUNT", 3),
        ("background_threads_per_owner", gateway_loops, "_BACKGROUND_PER_OWNER_THREAD_COUNT", 2),
        ("background_admin_threads_per_owner", gateway_loops, "_BACKGROUND_ADMIN_PER_OWNER_THREAD_COUNT", 19),
    ],
)
def test_fallbacks_follow_authoritative_defaults(monkeypatch, source, module, constant, replacement):
    monkeypatch.setattr(AgentConfig, source, replacement)
    monkeypatch.setattr(AgentConfig.__dataclass_fields__[source], "default", replacement)
    isolated = _load_consumer(monkeypatch, module, source)
    assert getattr(isolated, constant) == replacement
    if module is owner_pool:
        assert isolated._resolved_max_agents(None) == replacement
        assert isolated.ActiveOwnerRegistry()._max_owners == replacement
    else:
        reader = getattr(isolated, "_" + source)
        assert reader(SimpleNamespace()) == replacement


# LLM: 只核本次三个公开默认，运行时覆盖仍交原消费者，不实例化完整Agent或写用户目录。
# 函数用途: 锁住本轮新出厂值由统一配置到达实际消费者。
def test_existing_defaults_remain_compatible(tmp_path):
    config = AgentConfig()
    pool = owner_pool.OwnerScopedAgentPool(config, tmp_path)
    assert pool.gauges()["max_agents"] == config.owner_agent_pool_max_agents == 128
    agent = SimpleNamespace(config=config)
    assert gateway_loops._background_owner_workers(agent) == config.background_owner_workers == 128
    assert gateway_loops._background_threads_per_owner(agent) == config.background_threads_per_owner == 2


# LLM: 显式构造参数仍高于config，缺字段/非法值仍使用原安全默认，0不在本池定义为无限。
# 函数用途: 验证owner池容量优先级及非法输入兼容，不触碰持久任务。
@pytest.mark.parametrize(
    ("config", "override", "expected"),
    [
        (SimpleNamespace(owner_agent_pool_max_agents=7), None, 7),
        (SimpleNamespace(owner_agent_pool_max_agents=7), 2, 2),
        (SimpleNamespace(), None, 128),
        (SimpleNamespace(owner_agent_pool_max_agents=0), None, 128),
        (SimpleNamespace(owner_agent_pool_max_agents="invalid"), None, 128),
    ],
)
def test_owner_pool_capacity_precedence(tmp_path, config, override, expected):
    pool = owner_pool.OwnerScopedAgentPool(config, tmp_path, max_agents=override)
    assert pool.gauges()["max_agents"] == expected


# LLM: 工作线程与owner内线程作用域独立；缺省、0及非法值不改变已有兜底语义。
# 函数用途: 检查两条后台线程读取口各自消费配置，不混用并发预算。
@pytest.mark.parametrize("value", [None, 0, -1, "invalid", 5])
def test_background_worker_overrides_and_fallbacks(value):
    agent = SimpleNamespace(config=SimpleNamespace(
        background_owner_workers=value, background_threads_per_owner=value,
    ))
    assert gateway_loops._background_owner_workers(agent) == (5 if value == 5 else 128)
    assert gateway_loops._background_threads_per_owner(agent) == (5 if value == 5 else 2)
    assert gateway_loops._background_owner_workers(SimpleNamespace()) == 128
    assert gateway_loops._background_threads_per_owner(SimpleNamespace()) == 2


# LLM: 原池容量包括pin及构建，使用仅内存builder；满池不得逐出在途owner来伪造余量。
# 函数用途: 验证可配置容量仍守在途保护和硬软准入，退出pin后才可回收。
def test_configured_capacity_preserves_pinned_owner(tmp_path):
    config = SimpleNamespace(owner_agent_pool_max_agents=1)
    pool = owner_pool.OwnerScopedAgentPool(config, tmp_path)
    pool._builder = lambda *_args: SimpleNamespace()
    first = OwnerIdentity.provider_user("feishu", "param-w2-first")
    second = OwnerIdentity.provider_user("feishu", "param-w2-second")
    with pool.acquire_pinned(first) as agent:
        assert pool.peek(first) is agent
        assert pool.get(second, hard=False) is None
        with pytest.raises(owner_pool.PoolCapacityExceededError):
            pool.get(second)
        assert pool.gauges()["total_live_instances"] == 1
    assert pool.get(second) is not None
    assert pool.gauges()["pinned_count"] == 0
    assert pool.gauges()["total_live_instances"] == 1


# LLM: 使用原HTTP构造调用形状及唯一宿主指针，不替换线程池/监听器的实际构造。
# 函数用途: 在独立socket上检查参数在bind/listen前已经捕获，退出回收原执行器。
@contextmanager
def _http_server(monkeypatch, config):
    host = SimpleNamespace(agent=SimpleNamespace(config=config), control_plane=None)
    monkeypatch.setattr(http_service, "_server_instance", host)
    server = bounded.GatewayBoundedHTTPServer(
        ("127.0.0.1", 0), http_service.GatewayHTTPHandler, bind_and_activate=False,
    )
    try:
        yield server
    finally:
        server.server_close()


# LLM: 非默认值必须由现有宿主config传到真实执行器和信号量，不能只给一个无人调用的新接口。
# 函数用途: 锁住六项HTTP配置接线与在途容量不小于工作线程数的联合约束。
@pytest.mark.parametrize("capacity,expected", [(7, 7), (1, 3)])
def test_http_host_config_reaches_actual_resources(monkeypatch, capacity, expected):
    config = SimpleNamespace(
        gateway_http_listen_backlog=11, gateway_http_max_request_workers=3,
        gateway_http_max_outstanding_requests=capacity,
        gateway_http_socket_timeout_seconds=0.25,
        gateway_http_queue_timeout_seconds=0.05,
        gateway_http_birth_gate_timeout_seconds=0.125,
    )
    with _http_server(monkeypatch, config) as server:
        assert server.request_queue_size == 11
        assert server.max_request_workers == server._request_executor._max_workers == 3
        assert server.max_outstanding_requests == server._request_slots._initial_value == expected
        assert server.request_socket_timeout == 0.25
        assert server.request_queue_timeout == 0.05
        assert server.http_birth_gate_timeout_seconds == 0.125
        server.server_bind()
        server.server_activate()
        assert server.server_port > 0
    assert bounded.GatewayBoundedHTTPServer.request_queue_size == 128
    assert bounded.GatewayBoundedHTTPServer.max_request_workers == 32


# LLM: 同时核权威配置及缺字段调用，观察真实执行器和信号量，不预先创建工作线程。
# 函数用途: 验证新默认资源容量为32工作线程、256个在途请求槽位。
@pytest.mark.parametrize("config_factory", [AgentConfig, SimpleNamespace])
def test_http_worker_defaults_reach_actual_resources(monkeypatch, config_factory):
    with _http_server(monkeypatch, config_factory()) as server:
        assert server.max_request_workers == server._request_executor._max_workers == 32
        assert server.max_outstanding_requests == server._request_slots._initial_value == 256
        assert not server._request_executor._threads


# LLM: 仅在独立模块加载时移除类属性，直接反证getattr兜底仍残留16；monkeypatch回收原属性。
# 函数用途: 验证配置类缺少字段时也使用32线程兜底。
def test_http_worker_fallback_without_config_attribute(monkeypatch):
    monkeypatch.delattr(AgentConfig, "gateway_http_max_request_workers")
    isolated = _load_consumer(monkeypatch, bounded, "http_worker_fallback")
    assert isolated.GatewayBoundedHTTPServer.max_request_workers == 32


# LLM: 缺配置保原六默认，直接构造的测试子类覆盖仍有效，不修改类级共享状态。
# 函数用途: 验证旧调用和子类在未提供新配置时行为兼容。
def test_http_missing_config_preserves_defaults_and_subclass(monkeypatch):
    with _http_server(monkeypatch, SimpleNamespace()) as server:
        assert (server.request_queue_size, server.max_request_workers,
                server.max_outstanding_requests) == (128, 32, 256)
        assert (server.request_socket_timeout, server.request_queue_timeout,
                server.http_birth_gate_timeout_seconds) == (5.0, 2.0, 1.0)

    class SmallServer(bounded.GatewayBoundedHTTPServer):
        max_request_workers = 1
        max_outstanding_requests = 2
        request_socket_timeout = 0.05
        request_queue_timeout = 0.005
        http_birth_gate_timeout_seconds = 0.002

    from http.server import BaseHTTPRequestHandler

    server = SmallServer(("127.0.0.1", 0), BaseHTTPRequestHandler, bind_and_activate=False)
    try:
        assert server._request_executor._max_workers == 1
        assert server._request_slots._initial_value == 2
        assert (server.request_socket_timeout, server.request_queue_timeout,
                server.http_birth_gate_timeout_seconds) == (0.05, 0.005, 0.002)
    finally:
        server.server_close()


# LLM: 只控制单调时钟输入，不替换繁忙响应与关闭socket；过期工作不得进入业务handler。
# 函数用途: 用真实socket证明排队超时消费新值并仍回原503及Retry-After。
def test_http_configured_queue_timeout_keeps_busy_wire_contract(monkeypatch):
    config = SimpleNamespace(gateway_http_queue_timeout_seconds=0.05)
    with _http_server(monkeypatch, config) as server:
        client, accepted = socket.socketpair()
        entered = []
        monkeypatch.setattr(server, "finish_request", lambda *_: entered.append(True))
        try:
            server._process_request_worker(accepted, ("127.0.0.1", 1), time.monotonic() - 0.2)
            client.settimeout(0.2)
            payload = client.recv(4096)
            assert b"503 Service Unavailable" in payload
            assert b"Retry-After: 1" in payload and b"GATEWAY_HTTP_BUSY" in payload
            assert client.recv(1) == b"" and entered == []
        finally:
            client.close()
            accepted.close()


# LLM: birth门观察只代替锁等待，不改变submit失败后的槽位释放或503策略。
# 函数用途: 确认accept消费配置后的socket/gate超时，拒绝时不提交线程且归还槽位。
def test_http_configured_accept_timeouts_release_busy_slot(monkeypatch):
    config = SimpleNamespace(
        gateway_http_socket_timeout_seconds=0.25,
        gateway_http_birth_gate_timeout_seconds=0.125,
    )
    with _http_server(monkeypatch, config) as server:
        client, accepted = socket.socketpair()
        observed = []

        @contextmanager
        def busy(_runtime, *, timeout_seconds):
            observed.append((accepted.gettimeout(), timeout_seconds))
            raise bounded.HTTPBirthGateBusy("http_birth_gate_busy")
            yield

        monkeypatch.setattr(bounded, "original_http_birth", busy)
        try:
            before = server._request_slots._value
            server.process_request(accepted, ("127.0.0.1", 1))
            assert observed == [(0.25, 0.125)]
            assert server._request_slots._value == before
            assert server._request_executor._threads == set()
            assert b"GATEWAY_HTTP_BUSY" in client.recv(4096)
        finally:
            client.close()
            accepted.close()


# LLM: 仅替代网络出口与宿主凭据依赖；一次POST、完整原header/body和结果解析仍走真实客户端函数。
# 函数用途: 以非默认大/小cap反证写死30秒，结果等待阶段的配置不被改写。
@pytest.mark.parametrize("request_timeout,cap,expected", [
    (120, 7.5, 7.5), (120, 60, 60), (4, 60, 4), (0, 7.5, 0.1),
    (120, 0.05, 0.05),
    (120, None, 30.0),
])
def test_submission_timeout_is_smaller_of_cap_and_request(monkeypatch, request_timeout, cap, expected):
    values = {"gateway_port": 8420, "gateway_request_timeout": request_timeout}
    if cap is not None:
        values["gateway_submission_timeout_cap_seconds"] = cap
    agent = SimpleNamespace(config=SimpleNamespace(**values))
    calls = []
    monkeypatch.setattr(request_http_client, "gateway_client_credentials",
                        lambda _agent: SimpleNamespace(headers=lambda: {"Authorization": "test-only"}))

    @contextmanager
    def opened(request, *, timeout):
        calls.append((request, timeout))
        yield SimpleNamespace(status=202, read=lambda: b'{"delivery_status":"accepted"}')

    monkeypatch.setattr(request_http_client.urllib.request, "urlopen", opened)
    result = request_http_client.post_gateway_ask(agent, {
        "user_id": "param-w2", "channel": "chat", "metadata": {}, "prompt": "运输测试",
    })
    assert result == (202, {"delivery_status": "accepted"})
    assert len(calls) == 1 and calls[0][1] == expected
    assert agent.config.gateway_request_timeout == request_timeout


# LLM: 两条准入路径应保留整数零，不让正数helper把不限额洗回5000；非法类型仍沿缺省。
# 函数用途: 覆盖CLI策展额度的显式覆盖、零值与非法参数。
@pytest.mark.parametrize("value,expected", [(0, 0), (9, 9), (-1, 5000), (True, 5000), (None, 5000)])
def test_cli_curator_quota_keeps_zero_unlimited(value, expected):
    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = SimpleNamespace(config=SimpleNamespace(curator_daily_quota=value))
    assert supervisor._curator_daily_quota() == expected
    if value == 0:
        supervisor._curator_quota_count = 9000
        assert supervisor._curator_quota_available() is True


# LLM: 不限额仅取消准入上限，实际消耗仍通过原方法落同一date/count账，不改真实运行的调用时机。
# 函数用途: 反证CLI的零额度不能跳过真实消费计数及持久化。
def test_unlimited_curator_still_records_consumption(tmp_path):
    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = SimpleNamespace(config=SimpleNamespace(curator_daily_quota=0))
    supervisor._curator_quota_date = gateway_loops.datetime.now(gateway_loops.timezone.utc).date().isoformat()
    supervisor._curator_quota_count = 11
    supervisor._curator_quota_path = tmp_path / "quota" / "curator_daily_quota.json"
    supervisor._curator_quota_record_consumed()
    assert supervisor._curator_quota_count == 12
    saved = json.loads(supervisor._curator_quota_path.read_text())
    assert saved == {"date": supervisor._curator_quota_date, "count": 12}


# LLM: 只替代磁盘来源依赖来固定已消费/未结数，原contextmanager仍负责算准入结果与关闭guards。
# 函数用途: 验证正count零额度不拒绝，正数限额仍将未结占用计入，不改来源验证顺序。
@pytest.mark.parametrize("limit,count,pending,expected", [
    (0, 9000, 3, None), (2, 1, 1, "curator_daily_quota_exhausted"), (2, 1, 0, None),
])
def test_coordinator_curator_quota_keeps_zero_and_pending_budget(
    monkeypatch, tmp_path, limit, count, pending, expected,
):
    from agent_py_agent.agent.gateway_parts import curator_quota_index as index
    from agent_py_agent.agent.gateway_parts import multiprocess_background_request as source

    home = SimpleNamespace(root=tmp_path, global_index_dir=tmp_path)
    gate = SimpleNamespace(agent=SimpleNamespace(home_paths=home,
                           config=SimpleNamespace(curator_daily_quota=limit)), paths=object())
    request = {"kind": "background", "metadata": {"background_source": {
        "kind": "curator", "owner": asdict(OwnerIdentity.local_main()),
    }}}
    monkeypatch.setattr(source, "read_source", lambda *_: {})
    monkeypatch.setattr(accounting, "locked_json_path", lambda *_: nullcontext())
    monkeypatch.setattr(accounting, "_read_quota", lambda *_: {"count": count, "date": "2026-11-05"})
    monkeypatch.setattr(index, "ready", lambda *_: True)
    monkeypatch.setattr(index, "owner_facts", lambda *_: None)
    monkeypatch.setattr(accounting, "unsettled_curator_requests", lambda *_: pending)
    with accounting.curator_quota_admission(gate, request) as result:
        assert result == expected


# LLM: 配置集成在其它分片声明字段；动态替换权威类默认能揭示消费者是否仍维护独立5000/False。
# 函数用途: 验证策展兼容常量与镜像缺省随AgentConfig变化，不依赖当前恰好相同的值。
def test_curator_fallback_follows_config_default(monkeypatch):
    monkeypatch.setattr(AgentConfig, "curator_daily_quota", 13, raising=False)
    quota = _load_consumer(monkeypatch, accounting, "quota")
    assert quota.CURATOR_DAILY_QUOTA_DEFAULT_COUNT == 13


# LLM: 仅改变配置类缺省，不设置agent/config实例覆盖，避免与quota断言互相遮挡。
# 函数用途: 独立反证事件镜像的False是否仍是第二份硬编码默认。
def test_mirror_fallback_follows_config_default(monkeypatch):
    monkeypatch.setattr(AgentConfig, "store_event_mirror_enabled", True, raising=False)
    logger = _load_consumer(monkeypatch, gateway_logging, "mirror")
    records = []
    agent = SimpleNamespace(config=SimpleNamespace(),
                            local_store=SimpleNamespace(log_record=lambda **row: records.append(row)))
    logger.log_gateway_event(agent, "gateway_stopped", {})
    assert len(records) == 1 and records[0]["metadata"]["status"] == "stopped"


# LLM: 显式agent属性仍高于config；关闭时不触LocalStore，开启只镜像原事件不形成业务权威。
# 函数用途: 锁住镜像开关的旧优先级及默认关闭。
@pytest.mark.parametrize("agent_flag,config_flag,expected", [
    (None, None, 0), (None, True, 1), (None, False, 0),
    (False, True, 0), (True, False, 1),
])
def test_event_mirror_keeps_explicit_precedence(agent_flag, config_flag, expected):
    config = SimpleNamespace()
    if config_flag is not None:
        config.store_event_mirror_enabled = config_flag
    records = []
    agent = SimpleNamespace(config=config, local_store=SimpleNamespace(log_record=lambda **r: records.append(r)))
    if agent_flag is not None:
        agent.store_event_mirror_enabled = agent_flag
    gateway_logging.log_gateway_event(agent, "gateway_request_completed", {"id": "test"})
    assert len(records) == expected
