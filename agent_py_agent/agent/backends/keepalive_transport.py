# LLM: 长连接只服务显式带池的请求；决策池保持零重试、禁止重定向、有绝对期限的合同，主模型按空闲容量配置显式启用。
#   主池允许现有有界重试循环接管失败，每次发送单独记账；没有池的请求完全走原 urllib 路径，字节与行为不变。
#   打开语义与 urllib.request.AbstractHTTPHandler.do_open 对齐：同一代理解析（环境/系统代理、回环直连、绕过表）、同一
#   双超时连接类与中断守卫、同一请求头（只把 Connection: close 换成 keep-alive）、非 2xx 一律抛 urllib HTTPError；
#   池内不跟随重定向、不重发；复用陈旧连接的失败仅标记并上抛，由现有重试层决定是否重新发送。
#   连接只在关闭响应前证明正文已读到 EOF、服务端没要求关闭、守卫没中止过时归还；其余一律关闭。
#   池按（协议, 主机, 端口, 代理）分组，凭据不进身份：同一 TLS 连接上的每个请求自带 Authorization。
#   空闲超过 KEEPALIVE_MAX_IDLE_SECONDS 或对端已关（套接字可读）的连接取出时直接丢弃。改动须同步 test_keepalive_transport.py。
# 模块用途: 让显式启用的主模型及决策请求复用长连接，省去重复建连、代理隧道与 TLS 握手。
from __future__ import annotations

import http.client
import select
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from .gateway_request_limits import remaining_deadline_seconds, stdlib_response_socket
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
    allows_retrying_requests: bool = False
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

    # LLM: 只在锁内摘掉指定线路的空闲表；实际关闭在锁外，正在使用的连接不受影响。
    # 函数用途: 清空一条线路的空闲连接并返回关闭条数。
    def drop_idle(self, route: KeepAliveRoute) -> int:
        with self._lock:
            entries = self._idle.pop(route, [])
        for _since, connection in entries:
            _close_quietly(connection)
        return len(entries)

    # 函数用途: 返回池里当前空闲连接条数（测试与诊断用）。
    def idle_count(self) -> int:
        with self._lock:
            return sum(len(entries) for entries in self._idle.values())


# 决策调用共用的进程级连接池；只有配置打开 decision_connection_reuse_enabled 时才传给请求。
DECISION_CONNECTION_POOL = KeepAlivePool()

_MAIN_POOLS: dict[int, KeepAlivePool] = {}
_MAIN_POOLS_LOCK = threading.Lock()


# LLM: 进程内按容量共享池；注册表锁只保护创建与查找，不执行网络操作。
# 函数用途: 返回指定空闲容量的主模型池，非正容量保持原无池路径。
def main_connection_pool(max_idle_per_route: int) -> KeepAlivePool | None:
    cap = int(max_idle_per_route)
    if cap <= 0:
        return None
    with _MAIN_POOLS_LOCK:
        pool = _MAIN_POOLS.get(cap)
        if pool is None:
            pool = KeepAlivePool(max_idle_per_key=cap, allows_retrying_requests=True)
            _MAIN_POOLS[cap] = pool
        return pool


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
#   发送 OSError 照 urllib 包成 URLError；复用陈旧连接失败仅标记、清空线路并上抛，不在池内重发。非 2xx 抛 HTTPError。
# 函数用途: 经长连接池发出一次请求并返回标准库响应，成功响应带上归还池所需的租约。
def pooled_urlopen(target: KeepAliveTarget, req: urllib.request.Request, options: Any):
    route = target.route
    connection = target.pool.checkout(route)
    reused = connection is not None
    try:
        if connection is None:
            connection = _new_connection(route, options)
        else:
            _prepare_reused(connection, route, options)
        try:
            connection.request(req.get_method(), _selector(req.full_url), req.data, _request_headers(req))
        except OSError as err:
            wrapped = urllib.error.URLError(err)
            if reused and isinstance(err, (BrokenPipeError, ConnectionResetError)):
                wrapped.stale_reused_connection = True
                drop_idle = getattr(target.pool, "drop_idle", None)
                if callable(drop_idle):
                    drop_idle(route)
            raise wrapped from err
        response_started = None
        response_class = getattr(connection, "response_class", None)
        probe_response = reused and isinstance(response_class, type) and issubclass(response_class, http.client.HTTPResponse)
        if probe_response:
            # 只对复用连接预读同一缓冲区：见过任何字节后，状态行/100/headers失败都不能再算陈旧。
            def open_response(*args, **kwargs):
                nonlocal response_started
                opened = response_class(*args, **kwargs)
                try:
                    peek = getattr(opened.fp, "peek", None)
                    if callable(peek):
                        response_started = False
                        response_started = bool(peek(1))
                except BaseException:
                    # 构造器尚未返回，stdlib还未接管此fp；先关闭自有响应，仍抛原异常。
                    _close_quietly(opened)
                    raise
                return opened

            connection.response_class = open_response
        try:
            try:
                response = connection.getresponse()
            except (BrokenPipeError, ConnectionResetError) as exc:
                # RemoteDisconnected的旧无状态行判据保留；新增Reset/BrokenPipe必须有零字节证明。
                if reused and (response_started is False or
                               (response_started is None and isinstance(exc, http.client.RemoteDisconnected))):
                    exc.stale_reused_connection = True
                    drop_idle = getattr(target.pool, "drop_idle", None)
                    if callable(drop_idle):
                        drop_idle(route)
                raise
        finally:
            if probe_response:
                connection.response_class = response_class
    except BaseException:
        if connection is not None:
            _close_quietly(connection)
        raise
    response.url = req.full_url
    response.msg = response.reason
    if not 200 <= response.status < 300:
        # 与 urllib do_open 一致：正文文件继续持有 socket，读完或关闭正文即释放 fd，不依赖异常回收。
        if connection.sock is not None:
            connection.sock.close()
            connection.sock = None
        raise urllib.error.HTTPError(req.full_url, response.status, response.reason, response.headers, response)
    setattr(response, _LEASE_ATTR, _Lease(target, connection))
    return response


# LLM: 只在响应作用域收尾时调用（gateway_helpers._gateway_response_scope）；没有租约的响应什么都不做。
#   aborted 来自响应守卫；显式 body_complete 必须是关闭前的 EOF 证明，None 保留已有直接调用方的判据。
# 函数用途: 请求结束后把干净用完的长连接还给池，其余情况关闭它。
def release_pooled_response(response: Any, *, aborted: bool, body_complete: bool | None = None) -> None:
    lease = getattr(response, _LEASE_ATTR, None)
    if not isinstance(lease, _Lease):
        return
    setattr(response, _LEASE_ATTR, None)
    complete = response.isclosed() if body_complete is None else bool(body_complete)
    if not aborted and complete and not response.will_close and lease.connection.sock is not None:
        lease.target.pool.checkin(lease.target.route, lease.connection)
    else:
        _close_quietly(lease.connection)


# LLM: 只认本模块签发的租约，不将测试替身或其它响应属性当作连接归属。
# 函数用途: 判断响应是否持有需要归还或关闭的池连接。
def has_pooled_lease(response: Any) -> bool:
    return isinstance(getattr(response, _LEASE_ATTR, None), _Lease)


# LLM: 关闭响应前以零等待单次读取证明 EOF；任何不完整或不可读取结果都拒绝归还，退出恢复原 socket 超时。
# 函数用途: 尝试读完已就绪的正文尾部，不等待终止块或后续网络数据。
def drain_completed_response(response: Any) -> bool:
    if response.isclosed():
        return True
    sock = stdlib_response_socket(response)
    if sock is None:
        return False
    restore_timeout = False
    try:
        timeout = sock.gettimeout()
        restore_timeout = True
        sock.settimeout(0.0)
        response.read()
        return response.isclosed()
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return False
    finally:
        if restore_timeout:
            try:
                sock.settimeout(timeout)
            except OSError:
                pass


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


# LLM: 与标准库 context=None 时自建的默认上下文设置相同（系统 CA、校验证书与主机名、HTTP/1.1 ALPN），只是进程内共享一份；
#   SSLContext 可多线程只读共享，调用方不得在返回值上改设置。单独成函数也供测试替换假握手。
# 函数用途: 返回所有模型 HTTPS 连接共用的 TLS 上下文，免去每条连接约 0.7 MB 内存与约 20 ms CPU 的 CA 重新加载。
def _tls_context():
    return _shared_https_context()


# LLM: 首次调用时按 http.client 的默认规则建一次（3.12 私有工厂存在就直接用它，3.11 按同样三步构造）；并发首调最多多建一份，结果等价。
# 函数用途: 创建并缓存进程级默认 HTTPS 客户端上下文。
@cache
def _shared_https_context() -> ssl.SSLContext:
    factory = getattr(http.client, "_create_https_context", None)
    if callable(factory):
        return factory(http.client.HTTPSConnection._http_vsn)
    context = ssl._create_default_https_context()
    if http.client.HTTPSConnection._http_vsn == 11:
        context.set_alpn_protocols(["http/1.1"])
    if context.post_handshake_auth is not None:
        context.post_handshake_auth = True
    return context


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
    "drain_completed_response",
    "has_pooled_lease",
    "keepalive_route",
    "keepalive_target",
    "main_connection_pool",
    "pooled_urlopen",
    "release_pooled_response",
]
