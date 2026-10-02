"""决策调用长连接复用（J1，B 第 1 步）：严格请求带连接池时复用已建好的连接，省掉代理隧道与 TLS 握手。

背景（2026-10-01 实测）：每次 Jev 调用都经本机代理新建 CONNECT 隧道和 TLS，握手约 0.9 秒，比服务端首字节（约 0.4 秒）还慢；
代理链在空闲约 103–131 秒时直接断开隧道。本文件用本机假 CONNECT 代理、假 TLS 握手与 HTTP/1.1 保活假服务端，锁定：
1. 带池的连续请求只建一次隧道/一次握手/一条 TCP，复用那次的计时把建连、隧道、TLS 记 0 毫秒并标 connection_reused；
2. 不带池时与原来一样每次新建连接、请求头是 Connection: close；
3. 非 2xx、读正文到期中止的连接不放回池；空闲超时或对端已关的连接取出时丢弃；
4. 带池的请求只能是零重试、禁止重定向、有绝对期限的严格请求；
5. 决策服务只在配置开关打开时传池，Jev 后端经池发两次只用一条连接。
不访问任何真实服务。
"""
from __future__ import annotations

import json
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import gateway_helpers, keepalive_transport
from agent_py_agent.agent.backends.decision_protocol import DecisionRequest
from agent_py_agent.agent.backends.errors import ProviderTimeoutError
from agent_py_agent.agent.backends.gateway_helpers import (
    GatewayRequest,
    post_json,
    provider_attempt_observer,
)
from agent_py_agent.agent.backends.keepalive_transport import KeepAlivePool, KeepAliveRoute
from agent_py_agent.agent.backends.typesafe_decision import decision_backend_from_profile
from agent_py_agent.agent.conversation.decision_service import _decision_connection_pool
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.user_config_capability import packaged_config_path
from agent_py_agent.tests.test_decision_protocol import binding, questions
from agent_py_agent.tests.test_decision_protocol import response as decision_response
from agent_py_agent.tests.test_decision_transport_timing import (
    _ConnectProxy,
    _serve,
    _SlowHandshake,
)

_LOOPBACK = "127.0.0.1,localhost,::1"


# LLM: 测试替身：HTTP/1.1 保活服务端；每条 TCP 连接记一次，每个请求记下 Connection 头；状态码与正文延迟可按请求设定。
# 类用途: 本机假决策服务端（明文 HTTP，经假代理隧道或直连到达）。
class _KeepAliveTarget(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # 函数用途: 记一条新 TCP 连接。
    def setup(self) -> None:
        super().setup()
        self.server.state.tcp_connections.append(self.client_address)

    # 函数用途: 返回设定的 JSON 正文；按设定的状态码和正文延迟响应。
    def do_POST(self) -> None:
        state = self.server.state
        self.rfile.read(int(self.headers["Content-Length"]))
        state.requests.append(self.headers.get("Connection"))
        status = state.statuses.pop(0) if state.statuses else 200
        body = json.dumps(state.body).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            state.release.wait(state.body_delay)
            self.wfile.write(body)
        except OSError:
            pass

    # 函数用途: 关闭默认请求日志。
    def log_message(self, *_args) -> None:
        pass


# LLM: 测试夹具：HTTPS 请求经假 CONNECT 代理和假 TLS 到达保活假服务端；只改本测试进程的代理环境与长连接的 TLS 上下文。
# 函数用途: 提供能数隧道、握手、TCP 连接和请求头的完整链路。
@pytest.fixture
def link(monkeypatch):
    state = SimpleNamespace(proxy_delay=0.0, tls_delay=0.0, body_delay=0.0, connects=[], handshakes=[], tcp_connections=[],
                            requests=[], statuses=[], body={"ok": True}, release=threading.Event())
    target = ThreadingHTTPServer(("127.0.0.1", 0), _KeepAliveTarget)
    target.daemon_threads = True
    proxy = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _ConnectProxy)
    proxy.daemon_threads = True
    target.state = proxy.state = state
    state.target_port, state.direct_url = target.server_port, f"http://127.0.0.1:{target.server_port}"
    workers = [_serve(target), _serve(proxy)]
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.server_address[1]}")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, _LOOPBACK)
    context = _SlowHandshake(state)
    monkeypatch.setattr(keepalive_transport, "_tls_context", lambda: context)

    # 函数用途: 与产品 https_open 相同，只换成假 TLS context（不带池的原 urllib 路径用）。
    def https_open(handler, req):
        return handler.do_open(gateway_helpers._SplitTimeoutHTTPSConnection, req, context=context,
                               transport_options=handler.transport_options)

    monkeypatch.setattr(gateway_helpers._SplitTimeoutHTTPSHandler, "https_open", https_open)
    try:
        yield state
    finally:
        state.release.set()
        for server in (target, proxy):
            server.shutdown()
            server.server_close()
        for worker in workers:
            worker.join(2)


# 函数用途: 构造与 Jev 后端相同限制的严格请求（零重试、绝对期限、禁止重定向），可带连接池。
def _request(base: str, seconds: float, pool: KeepAlivePool | None) -> GatewayRequest:
    return GatewayRequest(api_base=base, api_key="k", path="/v1/systemone", payload={"q": 1},
                          headers={"Content-Type": "application/json"}, timeout=seconds, connect_timeout=seconds,
                          allow_redirects=False, deadline=time.monotonic() + seconds, max_retries=0,
                          max_response_bytes=65536, connection_pool=pool)


# 函数用途: 开启分段计时发一次请求，返回（结果或异常，本次最后的计时快照）。
def _send(base: str, pool: KeepAlivePool | None, seconds: float = 5.0):
    events: list[dict] = []
    with provider_attempt_observer(events.append, transport_timing=True):
        try:
            result = post_json(_request(base, seconds, pool))
        except Exception as exc:  # noqa: BLE001 失败用例要拿到异常本身
            result = exc
    transports = [event["transport"] for event in events if "transport" in event]
    return result, (transports[-1] if transports else {})


def test_pooled_calls_share_one_tunnel_and_record_zero_connect_time_when_reused(link):
    link.proxy_delay, link.tls_delay = 0.05, 0.1
    pool = KeepAlivePool()

    results = [_send("https://jev.test", pool) for _ in range(3)]

    assert [result for result, _ in results] == [{"ok": True}] * 3
    assert len(link.connects) == 1 and len(link.handshakes) == 1 and len(link.tcp_connections) == 1
    assert link.requests == ["keep-alive"] * 3
    first, *reused = [timing for _, timing in results]
    assert "connection_reused" not in first and first["phase_ms"]["tls_handshake"] >= 90
    for timing in reused:
        # 复用那次没有建连：建连、隧道、TLS 都是 0 毫秒；后面的阶段照常走完，计时不会被上一次的包裹干扰。
        assert timing["connection_reused"] is True and timing["phase"] == ""
        assert [timing["phase_ms"][phase] for phase in ("connect", "proxy_connect", "tls_handshake")] == [0.0, 0.0, 0.0]
        assert set(timing["phase_ms"]) >= {"request_send", "first_byte", "body_read"}
    assert pool.idle_count() == 1


def test_without_a_pool_every_call_opens_a_new_connection_as_before(link):
    for _ in range(2):
        assert _send("https://jev.test", None)[0] == {"ok": True}

    assert len(link.connects) == 2 and len(link.handshakes) == 2 and len(link.tcp_connections) == 2
    assert link.requests == ["close", "close"]


def test_error_responses_and_aborted_reads_are_never_returned_to_the_pool(link):
    pool = KeepAlivePool()
    link.statuses = [503]
    error, _ = _send("https://jev.test", pool)
    assert isinstance(error, Exception) and pool.idle_count() == 0
    # 读正文时到期：守卫中止过的连接不放回池。
    link.body_delay = 1.0
    timeout, _ = _send("https://jev.test", pool, seconds=0.4)
    assert isinstance(timeout, ProviderTimeoutError) and pool.idle_count() == 0
    link.body_delay = 0.0
    assert _send("https://jev.test", pool)[0] == {"ok": True}
    assert len(link.connects) == 3, "出错与中止的连接都没被复用，第三次新建"
    assert pool.idle_count() == 1


def test_direct_loopback_calls_reuse_without_a_proxy(link):
    pool = KeepAlivePool()
    for _ in range(2):
        assert _send(link.direct_url, pool)[0] == {"ok": True}
    assert link.connects == [] and len(link.tcp_connections) == 1


# LLM: 测试替身：只有 sock 与 close 的连接，用 socketpair 的一端模拟“对端关闭”。
# 类用途: 池管理单测用的假连接。
class _FakeConnection:
    # 函数用途: 绑定一端 socket。
    def __init__(self, sock: socket.socket) -> None:
        self.sock, self.closed = sock, False

    # 函数用途: 记录被关闭。
    def close(self) -> None:
        self.closed = True


def test_pool_drops_expired_or_peer_closed_connections_and_caps_idle_per_route():
    now = [100.0]
    pool = KeepAlivePool(max_idle_seconds=60.0, max_idle_per_key=2, clock=lambda: now[0])
    route = KeepAliveRoute("https", "jev.test", 443, "127.0.0.1", 7890)
    pairs = [socket.socketpair() for _ in range(4)]
    try:
        expired, closed_peer, fresh = (_FakeConnection(left) for left, _ in pairs[:3])
        pool.checkin(route, expired)
        now[0] += 61.0
        assert pool.checkout(route) is None and expired.closed
        pool.checkin(route, closed_peer)
        pairs[1][1].close()
        assert pool.checkout(route) is None and closed_peer.closed
        pool.checkin(route, fresh)
        assert pool.checkout(route) is fresh and not fresh.closed
        extra = [_FakeConnection(left) for left, _ in pairs[:3]]
        for item in extra:
            pool.checkin(route, item)
        assert pool.idle_count() == 2 and extra[0].closed, "超过每组上限时关掉最旧的"
        pool.close_all()
        assert pool.idle_count() == 0 and all(item.closed for item in extra)
    finally:
        for left, right in pairs:
            left.close()
            right.close()


def test_a_pool_is_only_allowed_on_strict_requests():
    pool = KeepAlivePool()
    with pytest.raises(ValueError):
        GatewayRequest(api_base="https://jev.test", api_key="k", path="/x", payload={}, headers={}, timeout=1.0,
                       connection_pool=pool)
    with pytest.raises(ValueError):
        GatewayRequest(api_base="https://jev.test", api_key="k", path="/x", payload={}, headers={}, timeout=1.0,
                       allow_redirects=False, deadline=time.monotonic() + 1, max_retries=1, connection_pool=pool)


def test_routes_skip_proxies_with_credentials_and_plain_http_through_a_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@127.0.0.1:7890")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", _LOOPBACK)
    assert keepalive_transport.keepalive_route("https://jev.test/v1") is None
    assert keepalive_transport.keepalive_route("http://jev.test/v1") is None
    assert keepalive_transport.keepalive_route("http://127.0.0.1:9/v1") == KeepAliveRoute("http", "127.0.0.1", 9)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    assert keepalive_transport.keepalive_route("https://jev.test/v1") == KeepAliveRoute(
        "https", "jev.test", 443, "127.0.0.1", 7890)


def test_the_decision_service_passes_the_pool_only_when_the_switch_is_on():
    assert AgentConfig().decision_connection_reuse_enabled is True
    assert load_config(packaged_config_path()).decision_connection_reuse_enabled is True
    on = SimpleNamespace(config=SimpleNamespace(decision_connection_reuse_enabled=True))
    off = SimpleNamespace(config=SimpleNamespace(decision_connection_reuse_enabled=False))
    assert _decision_connection_pool(on) is keepalive_transport.DECISION_CONNECTION_POOL
    assert _decision_connection_pool(off) is None


def test_the_jev_backend_sends_two_decisions_over_one_connection(link):
    link.body = decision_response()
    pool = KeepAlivePool()
    backend = decision_backend_from_profile({"api_base": link.direct_url, "api_key": "k", "model_name": "jev-test",
                                             "model_context_window_tokens": 32768, "model_custom_headers": {},
                                             "model_session_header": ""}, connection_pool=pool)
    request = DecisionRequest(binding(), {"lane": "a"}, questions())

    first = backend.decide(request, deadline=time.monotonic() + 5)
    second = backend.decide(request, deadline=time.monotonic() + 5)

    assert first.model == second.model == "jev-resolved-test"
    assert len(link.tcp_connections) == 1 and link.requests == ["keep-alive", "keep-alive"]
