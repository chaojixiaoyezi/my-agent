# LLM: 长连接复用只服务显式带连接池的严格请求（GatewayRequest.connection_pool 非空：零重试、禁止重定向、有绝对期限），
#   目前只有决策调用传池（配置 decision_connection_reuse_enabled）。没有池的请求完全走原 urllib 路径，字节与行为不变。
#   打开语义与 urllib.request.AbstractHTTPHandler.do_open 对齐：同一代理解析（环境/系统代理、回环直连、绕过表）、同一
#   双超时连接类与中断守卫、同一请求头（只把 Connection: close 换成 keep-alive）、非 2xx 一律抛 urllib HTTPError；
#   不跟随重定向、不重试——复用连接上的发送失败照原样上抛，不换新连接重发（避免同一请求被供应商收两次）。
#   连接只在“正文完整读完、响应已关闭、服务端没要求关闭、守卫没中止过”时由 release_pooled_response 放回池；其余一律关闭。
#   池按（协议, 主机, 端口, 代理）分组，凭据不进身份：同一 TLS 连接上的每个请求自带 Authorization。
#   空闲超过 KEEPALIVE_MAX_IDLE_SECONDS 或对端已关（套接字可读）的连接取出时直接丢弃。改动须同步 test_keepalive_transport.py。
# 模块用途: 让决策这类短小的严格请求复用已经建好的 HTTPS 长连接，省掉每次的代理隧道与 TLS 握手（真实约 0.9 秒）。
from __future__ import annotations

import select
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .gateway_request_limits import remaining_deadline_seconds
from .transport_timing import instrument_connection

# 2026-10-01 经本机代理实测：代理链（FlClash 或其上游节点）在空闲约 103–131 秒时直接断开隧道（无 TLS 关闭通知，
# Google、cloudflare.com、Jev 都一样），Jev 空闲 120 秒内复用成功、240 秒失败；DeepSeek 60 秒、MiniMax 120 秒由服务端主动关。
# 空闲长连接最长保留秒数：取最早断开（103 秒）以下留 40 秒余量；安全兜底，不进配置。
KEEPALIVE_MAX_IDLE_SECONDS = 60.0
# 每组（协议, 主机, 端口, 代理）最多留几条空闲连接：前台点位、后台单 worker 与多个用户偶尔并发，4 条够用且有界。
KEEPALIVE_IDLE_PER_ROUTE_COUNT = 4
# 响应对象上挂“这条连接从哪个池借来”的属性名；只有保活路径设置它。
_LEASE_ATTR = "_my_agent_keepalive_lease"
_DEFAULT_PORTS = {"http": 80, "https": 443}


# LLM: 冻结值对象；proxy_host 为空表示直连。凭据不在这里。
# 类用途: 一条长连接的身份：协议、目标主机与端口、经过的代理。
@dataclass(frozen=True)
class KeepAliveRoute:
    scheme: str
    host: str
    port: int
    proxy_host: str = ""
    proxy_port: int = 0


# LLM: 线程安全；取出的连接归调用方独占，直到 checkin 或关闭。不做网络 I/O（存活检查只是零超时 select）。
# 类用途: 按连接身份保存有界的空闲长连接，供下一次严格请求复用。
@dataclass
class KeepAlivePool:
    max_idle_seconds: float = KEEPALIVE_MAX_IDLE_SECONDS
    max_idle_per_key: int = KEEPALIVE_IDLE_PER_ROUTE_COUNT
    clock: Any = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _idle: dict[KeepAliveRoute, list[tuple[float, Any]]] = field(default_factory=dict, init=False, repr=False)

    # LLM: 先取最近放回的；空闲超时或对端已关闭的丢弃（关闭在锁外做）。
    # 函数用途: 取出一条可复用的空闲连接，没有就返回 None。
    def checkout(self, route: KeepAliveRoute) -> Any | None:
        while (entry := self._pop_idle(route)) is not None:
            idle_since, connection = entry
            if self.clock() - idle_since < self.max_idle_seconds and _connection_quiet(connection):
                return connection
            _close_quietly(connection)
        return None

    # LLM: 只在锁内改空闲表，不碰连接本身。
    # 函数用途: 弹出某个身份下最近放回的一条空闲连接（带放回时间），没有就返回 None。
    def _pop_idle(self, route: KeepAliveRoute) -> tuple[float, Any] | None:
        with self._lock:
            entries = self._idle.get(route)
            return entries.pop() if entries else None

    # LLM: 超过每组上限时关掉最旧的；关闭在锁外做。
    # 函数用途: 把一条刚干净用完的连接放回池里等下次复用。
    def checkin(self, route: KeepAliveRoute, connection: Any) -> None:
        evicted = []
        with self._lock:
            entries = self._idle.setdefault(route, [])
            entries.append((self.clock(), connection))
            while len(entries) > max(0, self.max_idle_per_key):
                evicted.append(entries.pop(0)[1])
        for item in evicted:
            _close_quietly(item)

    # LLM: 只关空闲连接，正在使用的连接归各自请求收尾；可重复调用。
    # 函数用途: 关闭并清空池里所有空闲连接（停机或测试收尾用）。
    def close_all(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, {}
        for entries in idle.values():
            for _since, connection in entries:
                _close_quietly(connection)

    # 函数用途: 返回池里当前空闲连接条数（测试与诊断用）。
    def idle_count(self) -> int:
        with self._lock:
            return sum(len(entries) for entries in self._idle.values())


# 决策调用共用的进程级连接池；只有配置打开 decision_connection_reuse_enabled 时才传给请求。
DECISION_CONNECTION_POOL = KeepAlivePool()


# LLM: 冻结值对象，由 keepalive_target 算出；pool 只要求有 checkout/checkin（GatewayRequest 校验过）。
# 类用途: 一次严格请求要借用的连接池与连接身份。
@dataclass(frozen=True)
class KeepAliveTarget:
    pool: Any
    route: KeepAliveRoute


# LLM: 冻结值对象，只由 pooled_urlopen 挂到成功响应上；收尾按类型识别，其它对象（如测试替身自动生成的属性）一律不当租约。
# 类用途: 记一条长连接从哪个池、哪个身份借出，供请求结束时归还或关闭。
@dataclass(frozen=True)
class _Lease:
    target: KeepAliveTarget
    connection: Any


# LLM: pool 为空或目标不能安全复用时返回 None，调用方走原 urllib 路径。
# 函数用途: 为一次请求算出要借用的连接池与连接身份。
def keepalive_target(pool: Any, url: str) -> KeepAliveTarget | None:
    if pool is None:
        return None
    route = keepalive_route(url)
    return KeepAliveTarget(pool, route) if route is not None else None


# LLM: 与 gateway_helpers._provider_proxy_handler + urllib ProxyHandler 同口径：回环主机直连；其余按 getproxies() 与
#   proxy_bypass；代理带用户名密码、或明文 HTTP 目标要经代理（urllib 用绝对 URI 而不是隧道）时返回 None，调用方改走原 urllib 路径。
# 函数用途: 算出一个 URL 的长连接身份；不能安全复用时返回 None。
def keepalive_route(url: str) -> KeepAliveRoute | None:
    from .gateway_helpers import _is_loopback_host

    parts = urllib.parse.urlsplit(url)
    scheme, host = parts.scheme.lower(), str(parts.hostname or "").lower().rstrip(".")
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    port = parts.port or _DEFAULT_PORTS[scheme]
    proxy = "" if _is_loopback_host(host) else urllib.request.getproxies().get(scheme, "")
    if not proxy or urllib.request.proxy_bypass(host):
        return KeepAliveRoute(scheme, host, port)
    proxy_parts = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
    if scheme != "https" or proxy_parts.username or proxy_parts.password or not proxy_parts.hostname:
        return None
    return KeepAliveRoute(scheme, host, port, proxy_parts.hostname, proxy_parts.port or 80)


# LLM: 由 gateway_helpers._gateway_urlopen 在打开守卫与期限作用域内调用，options 是本次尝试的 _SplitTimeoutOptions。
#   新连接用原双超时连接类（构造即挂守卫、装计时）；复用连接改挂本次守卫、按本次剩余期限设读超时、计时记“跳过建连”。
#   发送 OSError 照 urllib 包成 URLError；发送或取响应失败都关闭连接并原样上抛，不重试。非 2xx 抛 HTTPError（连接不放回）。
# 函数用途: 经长连接池发出一次严格请求并返回标准库响应，成功响应带上归还池所需的租约。
def pooled_urlopen(target: KeepAliveTarget, req: urllib.request.Request, options: Any):
    route = target.route
    connection = target.pool.checkout(route)
    try:
        if connection is None:
            connection = _new_connection(route, options)
        else:
            _prepare_reused(connection, route, options)
        try:
            connection.request(req.get_method(), _selector(req.full_url), req.data, _request_headers(req))
        except OSError as err:
            raise urllib.error.URLError(err) from err
        response = connection.getresponse()
    except BaseException:
        if connection is not None:
            _close_quietly(connection)
        raise
    response.url = req.full_url
    response.msg = response.reason
    if not 200 <= response.status < 300:
        raise urllib.error.HTTPError(req.full_url, response.status, response.reason, response.headers, response)
    setattr(response, _LEASE_ATTR, _Lease(target, connection))
    return response


# LLM: 只在响应作用域收尾时调用（gateway_helpers._gateway_response_scope）；没有租约的响应什么都不做。
#   aborted 来自响应守卫：被中断、到期或读失败中止过的连接一律关闭，绝不放回。
# 函数用途: 请求结束后把干净用完的长连接还给池，其余情况关闭它。
def release_pooled_response(response: Any, *, aborted: bool) -> None:
    lease = getattr(response, _LEASE_ATTR, None)
    if not isinstance(lease, _Lease):
        return
    setattr(response, _LEASE_ATTR, None)
    if not aborted and response.isclosed() and not response.will_close and lease.connection.sock is not None:
        lease.target.pool.checkin(lease.target.route, lease.connection)
    else:
        _close_quietly(lease.connection)


# LLM: 与 urllib 同一连接类和 TLS 默认上下文；经代理时用标准库 set_tunnel 走 CONNECT 隧道。
# 函数用途: 为一条长连接身份新建一个尚未连接的连接对象（真正连接发生在第一次发送时）。
def _new_connection(route: KeepAliveRoute, options: Any):
    from .gateway_helpers import _SplitTimeoutHTTPConnection, _SplitTimeoutHTTPSConnection

    host, port = (route.proxy_host, route.proxy_port) if route.proxy_host else (route.host, route.port)
    if route.scheme == "https":
        connection = _SplitTimeoutHTTPSConnection(host, port, context=_tls_context(), transport_options=options)
    else:
        connection = _SplitTimeoutHTTPConnection(host, port, transport_options=options)
    if route.proxy_host:
        connection.set_tunnel(route.host, route.port)
    return connection


# LLM: 与 urllib 默认 HTTPSHandler 相同返回 None（标准库默认 TLS 上下文，校验证书与主机名）；单独成函数只为测试替换假握手。
# 函数用途: 返回新建 HTTPS 长连接要用的 TLS 上下文。
def _tls_context():
    return None


# LLM: 复用连接的守卫、期限与读超时都换成本次尝试的；本次已被中断就直接关闭并上抛。计时从原方法重新包裹后记“跳过建连”。
# 函数用途: 把一条从池里取出的连接准备成本次尝试专用。
def _prepare_reused(connection: Any, route: KeepAliveRoute, options: Any) -> None:
    connection._provider_read_timeout = options.read_timeout
    connection._provider_deadline = options.deadline
    connection._provider_open_guard = options.open_guard
    if options.open_guard is not None:
        options.open_guard.attach(connection)
        if options.open_guard.aborted:
            _close_quietly(connection)
            raise InterruptedError("模型接口请求已被用户停止")
    remaining = remaining_deadline_seconds(options.deadline)
    connection.sock.settimeout(min(options.read_timeout, remaining) if remaining is not None else options.read_timeout)
    instrument_connection(connection, options.timing, tls=route.scheme == "https")
    if options.timing is not None:
        options.timing.skip_connection(tunnel=bool(route.proxy_host), tls=route.scheme == "https")


# LLM: 与 urllib do_open 同一合并顺序与首字母大写规则；只把 Connection 换成 keep-alive。
# 函数用途: 生成本次发送的请求头。
def _request_headers(req: urllib.request.Request) -> dict[str, str]:
    headers = dict(req.unredirected_hdrs)
    headers.update({key: value for key, value in req.headers.items() if key not in headers})
    headers["Connection"] = "keep-alive"
    return {name.title(): value for name, value in headers.items()}


# 函数用途: 取 URL 的路径加查询串作为请求行目标（与 urllib 隧道/直连时的 selector 相同）。
def _selector(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


# LLM: 零超时 select：空闲长连接上出现可读事件只可能是对端关闭或协议外数据，两种都不能复用。
# 函数用途: 判断一条空闲连接是否仍然安静可用。
def _connection_quiet(connection: Any) -> bool:
    sock = getattr(connection, "sock", None)
    if sock is None:
        return False
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return False
    return not readable


# 函数用途: 关闭一条连接并吞掉清理噪声。
def _close_quietly(connection: Any) -> None:
    try:
        connection.close()
    except Exception:  # noqa: BLE001 清理失败不影响调用结果
        pass


__all__ = [
    "DECISION_CONNECTION_POOL",
    "KEEPALIVE_IDLE_PER_ROUTE_COUNT",
    "KEEPALIVE_MAX_IDLE_SECONDS",
    "KeepAlivePool",
    "KeepAliveRoute",
    "KeepAliveTarget",
    "keepalive_route",
    "keepalive_target",
    "pooled_urlopen",
    "release_pooled_response",
]
