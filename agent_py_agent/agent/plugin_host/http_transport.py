# LLM: HTTP只认证、解码和运输；领域权威由A0端口提供，SSE设施属于原Gateway寿命且不持普通请求worker。
# 模块用途: 提供回环v2调用、有界独立SSE泵、限流与安全响应外壳。
from __future__ import annotations

import ipaddress
import json
import queue
import secrets
import socket
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from functools import partial

from ..concurrency import DurableDaemonThreadPoolExecutor
from ..gateway_parts.multiprocess_http_transport import original_http_birth
from .action_specs import action_spec
from .authorization import authorize_call, revalidate_call
from .composition import HostComposition, dispatch_call, domain_ready, stream_events
from .contracts import decode_call
from .errors import HostAPIError
from .schemas import PAGE_MAX_BYTES, REQUEST_MAX_BYTES, canonical_json, thaw, validate_request_event

# 每个宿主最多同时保留的流数；每流最多占两个专用worker。
STREAM_MAX_COUNT = 16
# 每个安装最多同时保留的流数，不按目标owner分裂额度。
INSTALL_STREAM_MAX_COUNT = 2
# 单流事件缓冲条数上限。
STREAM_QUEUE_COUNT = 8
# 空闲心跳及撤权检查间隔，单位秒。
HEARTBEAT_SECONDS = 15.0
# 慢客户端写入超时，单位秒。
SOCKET_TIMEOUT_SECONDS = 5.0
# 限流窗口，单位秒。
RATE_WINDOW_SECONDS = 1.0
# 一个窗口读取调用数量上限。
READ_RATE_COUNT = 10
# 一个窗口写入调用数量上限。
WRITE_RATE_COUNT = 2
# 限流键缓存上限，满时拒绝而不是丢弃活跃计数。
RATE_BUCKET_COUNT = 1024


# LLM: 内存限流只守传输容量，不代替目标owner原配额；窗口内键不能被LRU逐出绕过。
# 类用途: 对安装和实际owner实行有界滑动窗口限流。
class CallLimiter:
    # LLM: 构造不访问外部存储。
    # 函数用途: 初始化限流桶和互斥锁。
    def __init__(self):
        self._rows = {}
        self._lock = threading.Lock()

    # LLM: 键来自已授权绑定，客户端不能自填owner；拒绝只发生在领域执行前。
    # 函数用途: 消耗一个读取或写入名额。
    def admit(self, authorized):
        write = action_spec(authorized.call.action).requires_idempotency
        key = (authorized.binding.installation_ref, authorized.effective_owner, write)
        now = time.monotonic()
        with self._lock:
            self._rows = {key: [stamp for stamp in stamps if now - stamp < RATE_WINDOW_SECONDS]
                          for key, stamps in self._rows.items() if now - stamps[-1] < RATE_WINDOW_SECONDS}
            stamps = self._rows.get(key, [])
            if len(stamps) >= (WRITE_RATE_COUNT if write else READ_RATE_COUNT):
                raise HostAPIError("RATE_LIMITED", 429)
            if key not in self._rows and len(self._rows) >= RATE_BUCKET_COUNT:
                raise HostAPIError("RATE_LIMITED", 429)
            self._rows[key] = [*stamps, now]


# LLM: 订阅与业务任务寿命分离；关闭socket或事件迭代器不得调用cancel。
# 类用途: 连接有限事件缓冲、生产者和心跳发送者。
class StreamSession:
    # LLM: 先创建无socket会话，两个worker就绪后才转移连接，避免半启动写200。
    # 函数用途: 保存流所需的最小引用。
    def __init__(self, authorized, ports):
        self.authorized, self.ports = authorized, ports
        self.socket = None
        self.ready, self.stop = threading.Event(), threading.Event()
        self.events = queue.Queue(maxsize=STREAM_QUEUE_COUNT)
        self.remaining = 2
        self.headers_sent = False

    # LLM: 背压有界；关闭只丢弃本订阅未送事件，不改原请求。
    # 函数用途: 在订阅仍开启时把事件放入有限队列。
    def put(self, item):
        while not self.stop.is_set():
            try:
                self.events.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    # LLM: 领域迭代器可能等待但固定占槽；退订后不再主动next，退出不声称业务已停止。
    # 函数用途: 读取并校验事件，结束时发送订阅终止标记。
    def produce(self):
        self.ready.wait()
        if self.stop.is_set():
            return
        threading.current_thread()._plugin_stream_stop = self.stop
        iterator = stream_events(self.authorized, self.ports)
        try:
            while not self.stop.is_set():
                try:
                    event = next(iterator)
                except StopIteration:
                    break
                if self.stop.is_set():
                    break
                self.put(("request", validate_request_event(event)))
        except HostAPIError as error:
            self.put(("error", error.to_payload()))
        except Exception:
            self.put(("error", HostAPIError("STORE_UNAVAILABLE", 503).to_payload()))
        finally:
            threading.current_thread()._plugin_stream_stop = None
            iterator.close()
            self.put(None)

    # LLM: 独立writer保证领域阻塞时仍发心跳并检查撤权；任何断连只结束订阅。
    # 函数用途: 发送SSE头与有界事件，最后关闭转移来的socket。
    def write(self):
        self.ready.wait()
        try:
            if self.stop.is_set():
                return
            revalidate_call(self.authorized)
            self.socket.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\n"
                                b"Cache-Control: no-store\r\nConnection: close\r\n\r\n")
            self.headers_sent = True
            self._write_events()
        except HostAPIError as error:
            self._send_error(error)
        except OSError:
            pass
        finally:
            self.close()

    # LLM: 每个数据批和心跳之前末核，队列里旧事件不能越过撤权。
    # 函数用途: 持续发送事件或空闲心跳。
    def _write_events(self):
        while not self.stop.is_set():
            try:
                item = self.events.get(timeout=HEARTBEAT_SECONDS)
            except queue.Empty:
                item = ("heartbeat", None)
            revalidate_call(self.authorized)
            if item is None:
                return
            kind, data = item
            frame = (b": heartbeat\n\n" if kind == "heartbeat" else
                     f"event: {kind}\ndata: {canonical_json(data)}\n\n".encode())
            self.socket.sendall(frame)
            if kind == "error":
                return

    # LLM: 仅发送结构化公开错误，不包含异常正文或身份路径。
    # 函数用途: 在可写连接上报告订阅失败。
    def _send_error(self, error):
        try:
            if self.socket is not None:
                if self.headers_sent:
                    frame = f"event: error\ndata: {canonical_json(error.to_payload())}\n\n".encode()
                else:
                    body = canonical_json(envelope(error=error)).encode()
                    frame = (f"HTTP/1.1 {error.status} Error\r\nContent-Type: application/json\r\n"
                             f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body
                self.socket.sendall(frame)
        except OSError:
            pass

    # LLM: 关闭幂等，只释放网络资源，不取消业务请求或其配额。
    # 函数用途: 唤醒本订阅并关闭连接。
    def close(self):
        self.stop.set()
        self.ready.set()
        if self.socket is not None:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.socket.close()


# LLM: 全局和安装双限额；槽位直到生产者与writer都退出才释放，堵塞源不能无限新建线程。
# 类用途: 管理原Gateway寿命内的独立有界SSE线程池。
class StreamPool:
    # LLM: 构造只分配内存，设施出生在start原门内。
    # 函数用途: 初始化流池关闭门和会话集合。
    def __init__(self):
        self._lock = threading.RLock()
        self._sessions = set()
        self._executor = self._runtime = self._group = None
        self._closed = False

    # LLM: 使用原C设施门并向同Runtime登记executor；不是新的协调者或业务权限。
    # 函数用途: 显式创建固定容量的流设施。
    def start(self, runtime):
        with self._lock:
            if self._executor is not None or self._closed:
                raise RuntimeError("流池不能重复启动")
            self._runtime = runtime
            self._group = runtime.prepare_pool("control", "plugin-host/streams") if runtime is not None else None
            initializer = self._group.remember_current_thread if self._group is not None else None
            with original_http_birth(runtime):
                self._executor = DurableDaemonThreadPoolExecutor(
                    max_workers=STREAM_MAX_COUNT * 2, thread_name_prefix="plugin-host-stream", initializer=initializer)
                if self._group is not None:
                    self._group.bind_executor(self._executor)

    # LLM: 转移原fd而非dup，普通HTTP只能关旧socket；同步worker失败先拒绝移交，异步失败由回调关新socket。
    # 函数用途: 在限额和原设施门内将流交给独立worker。
    def open(self, handler, authorized, ports):
        with self._lock:
            session = self._reserve(authorized, ports)
            submitted = 0
            try:
                with original_http_birth(self._runtime):
                    for work in (session.write, session.produce):
                        future = self._executor.submit(work)
                        submitted += 1
                        future.add_done_callback(partial(self._finished, session))
                if session.stop.is_set():
                    raise HostAPIError("HOST_NOT_READY", 503)
                handler.close_connection = True
                handler.wfile.flush()
                session.socket = socket.socket(fileno=handler.connection.detach())
                session.socket.settimeout(SOCKET_TIMEOUT_SECONDS)
                session.ready.set()
            except BaseException as error:
                session.close()
                session.remaining -= 2 - submitted
                if session.remaining == 0:
                    self._sessions.discard(session)
                if self._group is not None:
                    self._group.retain_fault(error)
                raise

    # LLM: 按安装而非token或目标计流；旧代次未退出的流仍占用同安装额度。
    # 函数用途: 预留一个有限订阅槽。
    def _reserve(self, authorized, ports):
        if self._closed or self._executor is None:
            raise HostAPIError("HOST_NOT_READY", 503)
        count = sum(item.authorized.binding.installation_ref == authorized.binding.installation_ref
                    for item in self._sessions)
        if count >= INSTALL_STREAM_MAX_COUNT or len(self._sessions) >= STREAM_MAX_COUNT:
            raise HostAPIError("RATE_LIMITED", 429)
        session = StreamSession(authorized, ports)
        self._sessions.add(session)
        return session

    # LLM: 初始化失败/取消可能没进入worker的finally，回调必须关连接；原异常进入Runtime，不提前释放存活生产者。
    # 函数用途: 在两个worker都结束后释放订阅名额。
    def _finished(self, session, future):
        with self._lock:
            error = None if future.cancelled() else future.exception()
            if future.cancelled() or error is not None:
                session.close()
            if error is not None and self._group is not None:
                self._group.retain_fault(error)
            session.remaining -= 1
            if session.remaining == 0:
                self._sessions.discard(session)

    # LLM: 先关准入和socket再关pool，不join可能阻塞的业务迭代器；原Runtime继续观察线程寿命。
    # 函数用途: 停止本服务流运输。
    def close(self):
        with self._lock:
            self._closed = True
            for session in tuple(self._sessions):
                session.close()
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)


# LLM: 组合根由宿主显式注入；默认没有B/C/D端口，不会误走管理员Agent。
# 类用途: 管理一次监听的协议、授权和流设施。
class HostTransport:
    # LLM: 构造无外部副作用，允许宿主启动前注入测试或正式领域。
    # 函数用途: 初始化公共接口服务。
    def __init__(self, composition=None):
        self.composition = composition or HostComposition()
        self.streams = StreamPool()
        self.limiter = CallLimiter()

    # LLM: start由原Gateway认证及设施检查通过后调用，不能从请求体启动。
    # 函数用途: 登记专用线程池的原服务寿命。
    def start(self, runtime=None):
        self.streams.start(runtime)

    # LLM: 必须在原监听停止时撤销issuer和分页引用；不改变底层任务。
    # 函数用途: 关闭宿主接口准入和所有订阅。
    def close(self):
        self.composition.close()
        self.streams.close()

    # LLM: 真实token只从独立header取得，先认证后读有界正文，禁止借Gateway身份。
    # 函数用途: 把一个真实HTTP请求转换为已授权调用。
    def prepare(self, handler, streaming):
        _validate_headers(handler)
        source = self.composition.issuer.ingress(handler.headers.get("X-Plugin-Host-Token", ""))
        call = decode_call(_read_body(handler))
        if (action_spec(call.action).mode == "stream") is not streaming:
            raise HostAPIError("INVALID_SCHEMA")
        authorized = authorize_call(source, call)
        self.limiter.admit(authorized)
        return authorized


# LLM: 所有身份信息只来自已授权DTO；鉴权失败meta不得返回owner，trace为无意义随机值。
# 函数用途: 构造固定的成功或错误信封。
def envelope(authorized=None, result=None, error=None):
    meta = {"trace_id": secrets.token_hex(16), "action": authorized.call.action if authorized else "",
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    if authorized is not None:
        meta["effective_owner"] = asdict(authorized.effective_owner)
        meta.update(provenance="canonical", warnings=[])
    if result is not None:
        meta.update(provenance=result.provenance, warnings=list(result.warnings))
        return {"schema": "plugin_host_result.v2", "ok": True, "data": thaw(result.data), "meta": meta}
    return {"schema": "plugin_host_result.v2", "ok": False, "error": error.to_payload(), "meta": meta}


# LLM: 不处理Origin/Cookie/部署凭据，不信转发地址；即使本机浏览器持token也不开放CORS。
# 函数用途: 核验回环连接及唯一插件认证头。
def _validate_headers(handler):
    try:
        peer = ipaddress.ip_address(handler.client_address[0])
    except ValueError:
        raise HostAPIError("PLUGIN_TOKEN_INVALID", 401) from None
    if not (peer.is_loopback or getattr(peer, "ipv4_mapped", None) and peer.ipv4_mapped.is_loopback):
        raise HostAPIError("PLUGIN_TOKEN_INVALID", 401)
    forbidden = {"origin", "cookie", "authorization", "x-user-id", "x-channel", "x-owner-id", "forwarded"}
    for name in handler.headers.keys():
        lower = name.lower()
        if lower in forbidden or lower.startswith(("x-my-agent-", "x-forwarded-", "x-gateway-")):
            raise HostAPIError("CAPABILITY_DENIED", 403)
    if len(handler.headers.get_all("X-Plugin-Host-Token", [])) != 1:
        raise HostAPIError("PLUGIN_TOKEN_INVALID", 401)
    if handler.headers.get("Transfer-Encoding") is not None:
        raise HostAPIError("INVALID_SCHEMA")


# LLM: 不调用旧宽松_read_json；拒绝重复长度和非UTF8JSON，不支持chunked或压缩正文。
# 函数用途: 有界读取原始请求字节。
def _read_body(handler):
    lengths = handler.headers.get_all("Content-Length", [])
    media = handler.headers.get_all("Content-Type", [])
    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
        raise HostAPIError("INVALID_SCHEMA")
    if len(media) != 1 or media[0].lower().replace(" ", "") not in {"application/json", "application/json;charset=utf-8"}:
        raise HostAPIError("INVALID_SCHEMA")
    if handler.headers.get("Content-Encoding") is not None:
        raise HostAPIError("INVALID_SCHEMA")
    length = int(lengths[0])
    if length > REQUEST_MAX_BYTES:
        raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise HostAPIError("INVALID_SCHEMA")
    return raw


# LLM: 唯一路由适配器不含业务分支；未知异常按是否已入域区分，绝不泄露原错误。
# 函数用途: 完成同步或流请求的真实HTTP运输。
def _handle(handler, server, streaming):
    authorized, entered = None, False
    try:
        transport = getattr(server, "plugin_host", None)
        if not isinstance(transport, HostTransport):
            raise HostAPIError("HOST_NOT_READY", 503)
        authorized = transport.prepare(handler, streaming)
        if streaming:
            if not domain_ready(authorized.call.action, transport.composition.ports):
                raise HostAPIError("HOST_NOT_READY", 503)
            transport.streams.open(handler, authorized, transport.composition.ports)
            return
        entered = True
        result = dispatch_call(authorized, transport.composition.ports)
        body = envelope(authorized, result)
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > PAGE_MAX_BYTES:
            outcome = "committed" if action_spec(authorized.call.action).requires_idempotency else "not_started"
            raise HostAPIError("PAYLOAD_TOO_LARGE", 413, outcome)
        handler._send_json(result.http_status, body)
    except HostAPIError as error:
        handler._send_json(error.status, envelope(authorized, error=error))
    except (OSError, ValueError, TypeError, RuntimeError):
        outcome = "unknown" if entered and action_spec(authorized.call.action).requires_idempotency else "not_started"
        error = HostAPIError("OUTCOME_UNKNOWN" if outcome == "unknown" else "HOST_NOT_READY", 503, outcome)
        handler._send_json(error.status, envelope(authorized, error=error))


# LLM: 独立plugin_token路由，旧query不复用此入口。
# 函数用途: 处理v2同步调用。
def handle_plugin_host_call(handler, server):
    _handle(handler, server, False)


# LLM: 路由返回后普通worker立即释放，socket归专用流池。
# 函数用途: 处理v2流订阅。
def handle_plugin_host_stream(handler, server):
    _handle(handler, server, True)
