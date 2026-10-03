"""PluginChannelPool 合同测试：用假客户端与假传输验证启动超时、代次撤销、单在途与空闲关闭；不起真插件。"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_channel import (
    ERROR_BACKOFF_SECONDS,
    IDLE_CLOSE_SECONDS,
    ChannelCall,
    PluginChannelBackoff,
    PluginChannelPool,
    PluginChannelRevoked,
    PluginChannelTimeout,
)


# LLM: 模拟传输层在连接被关闭时抛出的错误（只带结构化错误码，不带文字判据）。
class _ConnectionClosed(Exception):
    code = "MCP_CONNECTION_CLOSED"


# LLM: 假传输只在调用点做三件事：发送前复核、通知假客户端（测试用它注入阻塞/失效）、按需抛错；
#   不模拟真实协议超时，超时语义由池的 deadline 测试覆盖。
class _FakeTransport:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client

    def request(self, method, params, *, timeout, authority_check):
        authority_check()
        self.client.on_request()
        if self.client.request_error is not None:
            raise self.client.request_error
        return {"ok": True, "method": method}


# LLM: 假客户端记录启动/停止次数并暴露 activation_ref；gate 用于让 start 阻塞以测启动超时。
class _FakeClient:
    def __init__(self, installation, *, gate: threading.Event | None = None,
                 after_start: threading.Event | None = None) -> None:
        self.installation = installation
        self.gate = gate
        # 启动返回之后、_finish_start 之前的窗口：调用方据此观察"启动中但传输已就绪"的状态
        self.after_start = after_start
        self.started = threading.Event()
        self.starts = 0
        self.stopped = 0
        self.stopped_after_start = False
        self.sent = 0
        self.revoked = False
        self.request_error: Exception | None = None
        self.on_request = self._count_send
        self.activation_ref = SimpleNamespace(require=self._require)

    # LLM: 默认的请求钩子就是"数发送次数"；发送前复核失败时传不会走到这里，所以它能代表真正发出的请求。
    # 函数用途: 记下一次真正发出的请求。
    def _count_send(self):
        self.sent += 1

    def _require(self):
        if self.revoked:
            raise ValueError("revoked")
        return self.installation

    def start(self):
        self.starts += 1
        if self.gate is not None:
            self.gate.wait(5)
        transport = _FakeTransport(self)
        if self.after_start is not None:
            # 让 start 一直不返回：连接与客户端都在，但这一轮启动还没收尾，正好覆盖"启动中"窗口。
            # 必须等用例放行才继续，不能设超时，否则启动会自己收尾、窗口瞬间消失。
            self.started.set()
            self.after_start.wait()
        return transport

    def stop(self):
        self.stopped += 1
        self.stopped_after_start = self.starts > 0
        return SimpleNamespace(confirmed=True)


# LLM: 假安装记录只提供池复核需要的 activation 与 installation 同一性，不构造真实安装表。
def _installation(activation_id: str = "act-1"):
    return SimpleNamespace(activation=SimpleNamespace(activation_id=activation_id), enabled=True)


class _Harness:
    def __init__(self, *, start_gate: threading.Event | None = None,
                 after_start: threading.Event | None = None) -> None:
        self.clock = [100.0]
        self.clients: list[_FakeClient] = []
        self.start_gate = start_gate
        self.after_start = after_start
        self.pool = PluginChannelPool(client_factory=self._factory, clock=lambda: self.clock[0])
        self.installation = _installation()
        self.conn = self.pool.acquire("owner", self.installation, self.clock[0])
        self._installations: dict[str, object] = {"owner": self.installation}

    def _factory(self, _owner, installation):
        client = _FakeClient(installation, gate=self.start_gate, after_start=self.after_start)
        self.clients.append(client)
        return client

    def acquire(self, owner_key: str, activation_id: str = "act-1", now: float | None = None):
        installation = _installation(activation_id)
        self._installations[owner_key] = installation
        return self.pool.acquire(owner_key, installation, self.clock[0] if now is None else now)

    def call(self, *, timeout: float | None = 1.0) -> ChannelCall:
        return ChannelCall(owner=object(), method="m", params={"x": 1}, timeout=timeout)

    def ready(self) -> _Harness:
        assert self.pool.request(self.conn, self.call()) == {"ok": True, "method": "m"}
        return self

    def request_in_background(self, connection, *, timeout: float = 5.0) -> threading.Thread:
        thread = threading.Thread(target=self.pool.request, args=(connection, self.call(timeout=timeout)),
                                  daemon=True)
        thread.start()
        return thread


# LLM: 轮询等待短条件成立，避免用固定 sleep 猜线程调度。
def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_start_timeout_is_recorded_and_background_start_is_reused():
    gate = threading.Event()
    h = _Harness(start_gate=gate)
    with pytest.raises(PluginChannelTimeout):
        h.pool.request(h.conn, h.call(timeout=0.05))
    # 启动超时后后台继续启动：客户端已创建且没有被停止，传输稍后可复用
    assert h.clients[0].starts == 1 and h.clients[0].stopped == 0
    gate.set()
    assert h.conn.start_event.wait(5) and h.conn.transport is not None
    h.clock[0] += ERROR_BACKOFF_SECONDS + 1
    assert h.pool.request(h.conn, h.call()) == {"ok": True, "method": "m"}
    assert h.clients[0].starts == 1 and len(h.clients) == 1


def test_revoked_activation_drops_result_and_closes_connection():
    h = _Harness().ready()
    h.clients[0].revoked = True
    with pytest.raises(PluginChannelRevoked):
        h.pool.request(h.conn, h.call())
    assert h.clients[0].stopped == 1
    assert h.conn.key not in h.pool.connections


def test_revocation_after_send_discards_returned_result():
    h = _Harness().ready()
    h.clients[0].on_request = lambda: setattr(h.clients[0], "revoked", True)
    with pytest.raises(PluginChannelRevoked):
        h.pool.request(h.conn, h.call())
    assert h.clients[0].stopped == 1 and h.conn.key not in h.pool.connections


def test_requests_on_one_connection_are_serialized():
    release = threading.Event()
    active = [0]
    peak = [0]
    h = _Harness().ready()

    def enter():
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        release.wait(5)
        active[0] -= 1

    h.clients[0].on_request = enter
    results: list[dict] = []
    first = threading.Thread(target=lambda: results.append(h.pool.request(h.conn, h.call(timeout=5.0))))
    first.start()
    assert _wait_until(lambda: peak[0] == 1)
    # 第一个请求在途时锁被持有，第二个请求只能排队，不会并发发送
    assert h.conn.request_lock.locked()
    second = threading.Thread(target=lambda: results.append(h.pool.request(h.conn, h.call(timeout=5.0))))
    second.start()
    time.sleep(0.2)
    assert peak[0] == 1
    release.set()
    first.join(5)
    second.join(5)
    assert peak[0] == 1 and len(results) == 2 and not first.is_alive() and not second.is_alive()


def test_idle_connection_closes_client_and_restarts_on_next_request():
    h = _Harness().ready()
    h.pool.close_idle(h.conn.key, h.clock[0] + IDLE_CLOSE_SECONDS + 1, False)
    assert h.clients[0].stopped == 1
    assert h.conn.key in h.pool.connections and h.conn.client is None and h.conn.transport is None
    assert h.pool.request(h.conn, h.call()) == {"ok": True, "method": "m"}
    assert len(h.clients) == 2 and h.clients[1].starts == 1


def test_error_backoff_blocks_next_request_until_deadline():
    h = _Harness().ready()
    h.clients[0].request_error = OSError("boom")
    with pytest.raises(OSError):
        h.pool.request(h.conn, h.call())
    with pytest.raises(PluginChannelBackoff):
        h.pool.request(h.conn, h.call())
    h.clock[0] += ERROR_BACKOFF_SECONDS + 1
    h.clients[0].request_error = None
    assert h.pool.request(h.conn, h.call()) == {"ok": True, "method": "m"}


def test_retire_stale_removes_and_stops_connection():
    h = _Harness().ready()
    h.pool.retire_stale("owner", set())
    assert h.clients[0].stopped == 1 and h.pool.connections == {}
    assert h.pool.get(h.conn.key) is None


# ── 复审回归（be，2026-10-03）：启动与停用/关闭交错时不能漏关插件进程 ──


def _start_timeout_harness() -> tuple[_Harness, threading.Event]:
    """把连接停在后台启动中：首次请求按启动超时返回，客户端已创建但还没开始启动。"""
    gate = threading.Event()
    h = _Harness(start_gate=gate)
    with pytest.raises(PluginChannelTimeout):
        h.pool.request(h.conn, h.call(timeout=0.05))
    assert h.clients[0].starts == 1 and h.clients[0].stopped == 0
    return h, gate


def test_retire_during_background_start_stops_started_client():
    h, gate = _start_timeout_harness()
    h.pool.retire_stale("owner", set())
    gate.set()
    assert h.conn.start_event.wait(5)
    # 启动完成时连接已不在池里，刚启动的客户端必须被停掉、传输不能发布
    assert h.clients[0].stopped_after_start, "停用撞上后台启动：启动完成的客户端没有被停"
    assert h.clients[0].sent == 0, "连接已摘除却仍把请求发了出去"
    assert h.conn.transport is None and h.conn.client is None


def test_close_during_background_start_stops_started_client():
    h, gate = _start_timeout_harness()
    h.pool.close()
    gate.set()
    assert h.conn.start_event.wait(5)
    assert h.clients[0].stopped_after_start, "关闭撞上后台启动：启动完成的客户端没有被停"
    assert h.conn.transport is None, "关闭后的池上不应再发布可用传输"
    assert h.pool.connections == {}


def test_request_on_connection_after_close_does_not_start_new_client():
    h = _Harness()
    h.pool.close()
    with pytest.raises(PluginChannelRevoked):
        h.pool.request(h.conn, h.call(timeout=1.0))
    assert h.clients == [], "池关闭后旧连接上的请求又拉起了新客户端"


def test_acquire_after_close_is_revoked():
    h = _Harness()
    h.pool.close()
    with pytest.raises(PluginChannelRevoked):
        h.pool.acquire("owner", h.installation, h.clock[0])


def test_detach_during_request_is_reported_as_revoked():
    sent = []
    installation = _installation()
    holder = {}

    class _Transport:
        def request(self, method, params, *, timeout, authority_check):
            authority_check()
            sent.append(method)
            # 请求在途时连接被停用摘除：发送后复核必须按撤销收场，而不是把已回收的客户端当 AttributeError
            holder["pool"].retire_stale("owner", set())
            return {"ok": True}

    class _Client:
        activation_ref = SimpleNamespace(require=lambda: installation)

        def start(self):
            return _Transport()

        def stop(self):
            pass

    pool = PluginChannelPool(client_factory=lambda _o, _i: _Client(), clock=lambda: 100.0)
    holder["pool"] = pool
    conn = pool.acquire("owner", installation, 100.0)
    with pytest.raises(PluginChannelRevoked):
        pool.request(conn, ChannelCall(owner=object(), method="m", params={}, timeout=1.0))
    assert sent == ["m"] and pool.connections == {}


# ── 复审回归：把被判为"存活"的 6 个变异逐条钉死 ──


def test_revoked_connection_sends_nothing_and_closes_client():
    """M1：发送前复核被改成空操作时，已撤销的连接会把请求发出去——一个字节都不许发。"""
    h = _Harness().ready()
    client = h.clients[0]
    sent_before = client.sent
    client.revoked = True
    with pytest.raises(PluginChannelRevoked):
        h.pool.request(h.conn, h.call())
    assert client.sent == sent_before, "已撤销的连接仍把请求发了出去"
    assert client.stopped == 1


def test_retire_stale_only_touches_its_own_owner():
    """M5：回收不看 owner 时，A 的查询会把 B 的连接一起关掉——跨 owner 的连接必须原样留着。"""
    h = _Harness().ready()
    other = h.acquire("other")
    assert h.pool.request(other, h.call()) == {"ok": True, "method": "m"}
    other_client = h.clients[1]
    h.pool.retire_stale("owner", set())
    assert h.conn.key not in h.pool.connections
    assert h.pool.connections[other.key] is other
    assert h.clients[0].stopped == 1 and other_client.stopped == 0
    assert h.pool.request(other, h.call()) == {"ok": True, "method": "m"}


def test_idle_close_keeps_connection_that_is_still_starting():
    """M6：空闲关闭不看"启动中"时，会把这一轮还没收尾的启动连接半路关掉。

    观察点：连接已挂着本轮客户端、last_used 已过期，但仍标记为 starting。
    这里手工构造这个状态而不走 request()，因为 request() 会同时持有请求锁，
    请求锁这道冗余防线会掩盖掉 starting 判断，必须让它成为唯一防线。
    """
    h = _Harness()
    client = h.pool._client_factory("owner", h.installation)
    h.conn.client = client
    h.conn.starting = True
    h.conn.last_used = h.clock[0] - IDLE_CLOSE_SECONDS - 1

    h.pool.close_idle(h.conn.key, h.clock[0], False)

    # 启动中的连接必须原样留着：既不能停客户端，也不能把这一轮启动用的客户端摘掉
    assert client.stopped == 0, "启动中的连接被空闲关闭半路停掉"
    assert h.conn.client is client, "启动中的连接被空闲关闭摘掉了客户端"


def test_idle_close_keeps_connection_with_request_in_flight():
    """M7：空闲关闭不看"在途"时，会把正在发送请求的连接关掉。"""
    release = threading.Event()
    entered = threading.Event()
    h = _Harness().ready()

    def block():
        entered.set()
        release.wait(5)

    h.clients[0].on_request = block
    thread = h.request_in_background(h.conn)
    assert entered.wait(5)

    h.pool.close_idle(h.conn.key, h.clock[0] + IDLE_CLOSE_SECONDS + 1, False)
    assert h.clients[0].stopped == 0, "在途请求期间连接被空闲关闭"

    release.set()
    thread.join(5)
    assert not thread.is_alive()


def test_start_waiting_counts_toward_request_budget():
    """M8：启动等待不计入超时时，一次慢启动会把请求预算拖成"无限等"。"""
    gate = threading.Event()
    h = _Harness(start_gate=gate)
    started = time.monotonic()
    with pytest.raises(PluginChannelTimeout):
        h.pool.request(h.conn, h.call(timeout=0.2))
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"启动等待没有计入请求超时预算（实际等了 {elapsed:.1f} 秒）"
    gate.set()
    assert h.conn.start_event.wait(5)


def test_queue_waiting_counts_toward_request_budget():
    """M9：排队等待不计入超时时，被前一个请求堵住的请求会一直等下去。"""
    release = threading.Event()
    entered = threading.Event()
    h = _Harness().ready()

    def block():
        entered.set()
        release.wait(5)

    h.clients[0].on_request = block
    first = h.request_in_background(h.conn)
    assert entered.wait(5)
    started = time.monotonic()
    with pytest.raises(PluginChannelTimeout):
        h.pool.request(h.conn, h.call(timeout=0.2))
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"排队等待没有计入请求超时预算（实际等了 {elapsed:.1f} 秒）"
    release.set()
    first.join(5)


# LLM: 退避必须是连接级故障专属。共用连接上还有别的调用方（事件投递、收紧征询），
#   一个调用方的请求级失败（远端对这一个请求回的错、调用方自己的校验拒绝）不能连坐它们 5 秒。
#   判定只看结构化类型/错误码，下面的用例分别钉住"不该退避"和"该退避"两侧。
def test_request_level_remote_error_does_not_back_off_shared_connection():
    """请求级故障：插件对某一个请求回了 JSON-RPC 错误，同连接的其它调用方必须照常可用。"""
    from agent_py_agent.agent.tooling.mcp_protocol import MCPError

    h = _Harness().ready()
    h.clients[0].request_error = MCPError("远端拒绝", code="MCP_REMOTE_ERROR")
    with pytest.raises(MCPError):
        h.pool.request(h.conn, h.call())
    # 时钟一格没走；若把请求级错误也算连接退避，这一次会直接撞上 PluginChannelBackoff
    h.clients[0].request_error = None
    assert h.pool.request(h.conn, h.call()) == {"ok": True, "method": "m"}


def test_before_send_rejection_does_not_back_off_shared_connection():
    """请求级故障：调用方 before_send 校验拒绝（如面板能力检查），不退避整条连接。"""
    h = _Harness().ready()

    def refuse(_client):
        raise ValueError("插件未声明展示能力")

    with pytest.raises(ValueError):
        h.pool.request(h.conn, ChannelCall(owner=object(), method="m", params={}, timeout=1.0, before_send=refuse))
    assert h.pool.request(h.conn, h.call()) == {"ok": True, "method": "m"}


def test_timeout_still_backs_off_whole_connection():
    """连接级故障：超时说明这条连接不可用，仍要退避整条连接。"""
    h = _Harness().ready()
    h.clients[0].request_error = PluginChannelTimeout("响应超时")
    with pytest.raises(PluginChannelTimeout):
        h.pool.request(h.conn, h.call())
    h.clients[0].request_error = None
    with pytest.raises(PluginChannelBackoff):
        h.pool.request(h.conn, h.call())


def test_start_failure_still_backs_off_whole_connection():
    """连接级故障：连接起不来（启动异常）仍要退避整条连接。"""
    from agent_py_agent.agent.plugin_channel import PluginChannelStartFailed

    h = _Harness()

    def broken_start():
        raise RuntimeError("插件进程起不来")

    client = SimpleNamespace(start=broken_start, stop=lambda: None,
                             activation_ref=SimpleNamespace(require=lambda: h.installation))
    h.pool._client_factory = lambda _owner, _inst: client
    with pytest.raises(PluginChannelStartFailed):
        h.pool.request(h.conn, h.call())
    with pytest.raises(PluginChannelBackoff):
        h.pool.request(h.conn, h.call())


def test_before_send_sees_revocation_not_none_when_connection_detached():
    """在途被摘除时：传给 before_send 的客户端必须按撤销报错，不能是 None。"""
    from agent_py_agent.agent.plugin_channel import PluginChannelRevoked

    h = _Harness().ready()
    seen = []

    def inspect_client(client):
        seen.append(client)
        h.pool.retire_stale("owner", set())  # 恰好在 before_send 期间把连接摘除

    with pytest.raises(PluginChannelRevoked):
        h.pool.request(h.conn, ChannelCall(owner=object(), method="m", params={}, timeout=1.0,
                                           before_send=inspect_client))
    assert seen and seen[0] is not None, "摘除后仍把 None 交给调用方校验"


# LLM: 池的连接表会被后台渲染撤销、以后 B3 的事件中心并发改；调用方遍历它必须拿快照，
#   直接遍历活字典会撞 RuntimeError: dictionary changed size during iteration。
#   这条用例同时钉住"池提供加锁快照"和"快照不可变"两件事。
def test_owner_keys_snapshot_survives_concurrent_removal():
    h = _Harness().ready()
    h.acquire("owner", activation_id="act-2")
    h.acquire("owner", activation_id="act-3")

    keys = h.pool.owner_keys("owner")
    assert isinstance(keys, tuple), "owner_keys 必须返回不可变快照，不能把活字典交出去"
    assert {key[1] for key in keys} >= {"act-1", "act-2", "act-3"}

    # 模拟别的调用方在遍历期间删连接：快照已经定型，遍历不受影响、不抛错
    h.pool.retire_stale("owner", {"act-1"})
    walked = [key for key in keys if key[1] in {"act-1", "act-2", "act-3"}]
    assert len(walked) == 3, "快照在遍历中途被并发删除影响"

    # 新取一次快照能看到删除结果
    assert {key[1] for key in h.pool.owner_keys("owner")} == {"act-1"}


# LLM: 9b 复审判定：请求在途时连接被停用/关闭摘除，调用方拿到的不该是"连接断开"。
#   传输层先报错很正常（对端被关了），但连接已经不在表里说明这次请求是被撤销的，
#   必须按撤销抛、且不标退避——否则 B5 会把"插件已停用"当成"连接坏了"，多弹一次审批框。
def test_inflight_retire_is_reported_as_revoked_not_connection_closed():
    from agent_py_agent.agent.plugin_channel import PluginChannelRevoked

    h = _Harness().ready()
    closed = _ConnectionClosed()
    client = h.clients[0]

    def retire_then_fail():
        h.pool.retire_stale("owner", set())  # 请求在途时停用：连接被摘除
        client.request_error = closed

    client.on_request = retire_then_fail
    with pytest.raises(PluginChannelRevoked):
        h.pool.request(h.conn, h.call())
    assert h.conn.backoff_until == 0.0, "被撤销的请求不该给连接标退避"


# LLM: 9b 的 MU4：_current_client 必须核对"连接还在表里"。少了这个核对，并发摘除会把 None
#   交给调用方的 before_send，让面板把"连接被回收"误判成"插件没声明展示能力"。
def test_before_send_gets_revoked_not_none_when_connection_already_detached():
    from agent_py_agent.agent.plugin_channel import PluginChannelRevoked

    h = _Harness().ready()
    seen = []
    real_ensure_started = h.pool._ensure_started

    def ensure_then_detach(connection, call, deadline):
        # 关键窗口：传输已经就绪、start 已经收尾，但连接在 before_send 之前被摘除。
        # 不能在请求发出前就停用（那会在 _ensure_started 里抛撤销，走不到这个窗口）。
        transport = real_ensure_started(connection, call, deadline)
        h.pool.retire_stale("owner", set())
        return transport

    h.pool._ensure_started = ensure_then_detach

    def inspect_client(client):
        seen.append(client)

    with pytest.raises(PluginChannelRevoked):
        h.pool.request(h.conn, ChannelCall(owner=object(), method="m", params={}, timeout=1.0,
                                           before_send=inspect_client))
    assert seen == [], "连接已摘除时不该把客户端（更不该是 None）交给调用方校验"


# LLM: 9b 的 MU7：retire_stale 的扫描必须在池锁内做。只断言"进过一次锁"不够——摘除那一步
#   照样要进锁。所以这里把连接表换成 dict 子类，在每次遍历（__iter__/keys/items）时记录
#   池锁是否处于持有状态：扫描被挪到锁外，遍历时锁就是释放的，用例就会失败。
def test_retire_stale_scans_under_pool_lock():
    h = _Harness().ready()
    h.acquire("owner", activation_id="act-2")
    h.acquire("owner", activation_id="act-3")
    scans = []

    class _LockWatchingDict(dict):
        def __iter__(self):
            scans.append(h.pool.lock.locked())
            return super().__iter__()

        def keys(self):
            scans.append(h.pool.lock.locked())
            return super().keys()

        def items(self):
            scans.append(h.pool.lock.locked())
            return super().items()

    h.pool._connections = _LockWatchingDict(h.pool._connections)
    h.pool.retire_stale("owner", {"act-1"})

    assert scans, "retire_stale 没有遍历连接表"
    assert all(scans), "retire_stale 在锁外遍历了连接表（扫描与摘除之间会被并发修改）"
    assert {key[1] for key in h.pool.owner_keys("owner")} == {"act-1"}
