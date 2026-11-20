# 模块用途: 分片预读明文连接，保留原请求执行来源及有界工作容量。
from __future__ import annotations

import selectors
import socket
import threading
import time
from collections import deque
from contextvars import ContextVar
from functools import partial
from heapq import heapify, heappop, heappush
from itertools import count

from ..concurrency import DurableDaemonThreadPoolExecutor
from ..concurrency.lifo_work_queue import CancellableWorkQueue as _CancellableWorkQueue
from .http_body_readiness import HTTP_READY_PREFIX_BYTES, PrefetchedSocket, complete_request_length
from .http_routes import match_gateway_http_route
from .multiprocess_http_transport import HTTPBirthGateBusy

lane_queue_ms: ContextVar[int | None] = ContextVar('gateway_http_lane_queue_ms', default=None)

# 就绪分类时最多窥视的请求首行字节数：只用于选择车道，首行更长时按普通车道处理，不影响请求本身。
HTTP_CONTROL_PEEK_BYTES = 8192
# 控制车道只识别原路由模板，不赋予调用者身份或业务权限。
HTTP_CONTROL_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ('GET', '/status'), ('GET', '/metrics'), ('GET', '/control-status/*'),
    ('POST', '/control'), ('POST', '/client/agent-stop'), ('POST', '/client/agent-guidance'),
    ('POST', '/client/agent-permission'), ('POST', '/stop'), ('POST', '/client/turn-stop'),
})
# 公开只读状态在控制类内另走独立执行池，不排在慢 /control 处理器之后。
HTTP_STATUS_LANE_ROUTES: frozenset[tuple[str, str]] = frozenset({('GET', '/status'), ('GET', '/metrics')})
# 路由本身即停止语义的请求走停止执行池；未认证前绝不读正文，POST /control 一律留在原控制池。
HTTP_STOP_LANE_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ('POST', '/stop'), ('POST', '/client/agent-stop'), ('POST', '/client/turn-stop'),
})
# 状态与停止两个快速执行池各自的工作线程数：慢 /control（如手动 /compact）占满控制池时它们仍有独立线程。
HTTP_FAST_LANE_WORKERS_COUNT = 2


# LLM: 只解析已窥视的首行；不认识或不在控制类的路由返回 None，不授予任何身份。
# 函数用途: 取得控制类请求的 (方法, 路由模板)，供执行池细分。
def _control_route_key(data) -> tuple[str, str] | None:
    parts = bytes(data[:HTTP_CONTROL_PEEK_BYTES]).partition(b'\n')[0].rstrip(b'\r').split(b' ')
    if len(parts) != 3:
        return None
    try:
        method, target = parts[0].decode('ascii'), parts[1].decode('ascii')
    except UnicodeDecodeError:
        return None
    route = match_gateway_http_route(method, target.split('?', 1)[0])
    return (method, route.template) if route is not None else None


# LLM: 只在已归入控制车道后按首行路由细分执行池，classify_peeked_request 的车道语义与路由表保持不变；
#   选择器线程处于认证之前，只看方法与路径，绝不解析正文或据正文授予更快的池。
# 函数用途: 为控制类请求选择 status/stop/control 执行池，使状态读与停止路由不被慢控制命令饿死。
def control_sublane(data) -> str:
    key = _control_route_key(data)
    if key in HTTP_STATUS_LANE_ROUTES:
        return 'status'
    if key in HTTP_STOP_LANE_ROUTES:
        return 'stop'
    return 'control'


# LLM: 只窥视而不消费首行；不完整、未知或无法解析的输入保持普通处理路径。
# 函数用途: 通过原运行时路由表选择车道，空输入单独表示连接已关闭。
def classify_peeked_request(data: bytes) -> str:
    if not data:
        return 'eof'
    line, newline, _ = data[:HTTP_CONTROL_PEEK_BYTES].partition(b'\n')
    parts = line.rstrip(b'\r').split(b' ')
    if not newline or len(parts) != 3 or not all(parts):
        return 'general'
    try:
        method = parts[0].decode('ascii')
        target = parts[1].decode('ascii')
    except UnicodeDecodeError:
        return 'general'
    route = match_gateway_http_route(method, target.split('?', 1)[0])
    return 'control' if route is not None and (method, route.template) in HTTP_CONTROL_ROUTES else 'general'


# LLM: 状态锁只执行CAS；关闭标记先在锁内领取，套接字操作在锁外执行。
# 类用途: 保证worker与过期处理者只有一个能接管连接，且连接只关闭一次。
class _LaneTicket:
    # LLM: 提交前创建，future由提交者设置；所有权从QUEUED开始。
    # 函数用途: 保存一次连接移交及其单调状态。
    def __init__(self, request, client_address, lane):
        self.request = request
        self.client_address = client_address
        self.enqueued_at = 0.0
        self.lane = lane
        self.future = None
        self.executor = None
        self.state = 'QUEUED'
        self._lock = threading.Lock()
        self._closed = False
        self._ready = threading.Event()

    # LLM: 只有仍排队的票据可以进入处理器。
    # 函数用途: 原子领取运行权。
    def claim_running(self) -> bool:
        with self._lock:
            if self.state != 'QUEUED':
                return False
            self.state = 'RUNNING'
            return True

    # LLM: 已开始执行或已完成的票据不能再被过期处理者关闭。
    # 函数用途: 原子领取过期处理权。
    def claim_expired(self) -> bool:
        with self._lock:
            if self.state != 'QUEUED':
                return False
            self.state = 'EXPIRED'
            return True

    # LLM: 只读取内存状态，不跨状态锁执行任何I/O。
    # 函数用途: 判断是否仍需排队期限观察。
    def is_queued(self) -> bool:
        with self._lock:
            return self.state == 'QUEUED'

    # LLM: 只允许终态前进，回调与worker重复收口无副作用。
    # 函数用途: 记录连接处理已结束。
    def mark_done(self) -> None:
        with self._lock:
            self.state = 'DONE'

    # LLM: 关闭权必须先领取；实际shutdown在锁外执行。
    # 函数用途: 只让首个关闭者执行连接回收。
    def _claim_close(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._closed = True
            return True

    # LLM: 取消回调与处理器finally共享同一关闭标记。
    # 函数用途: 至多调用一次服务器的连接关闭入口。
    def close_once(self, server) -> None:
        if self._claim_close():
            server.shutdown_request(self.request)

    # LLM: 非阻塞拒绝入口自身会关闭连接，必须共享关闭标记以免再shutdown一次。
    # 函数用途: 至多回一次503并由原拒绝入口回收连接。
    def reject_once(self, server) -> None:
        if self._claim_close():
            server._reject_without_wait(self.request)


class _Pending:
    """前缀和时限仅由其选择器线程访问。"""

    def __init__(self, sock, addr, deadline):
        self.sock = sock
        self.addr = addr
        self.deadline = deadline
        self.prefix = bytearray()
        self.lane = None
        self.length = None
        self.registered = False


class _ReadinessShard:
    def __init__(self, router):
        self.router = router
        self._lock = threading.Lock()
        self.incoming = deque()
        self.pending = {}
        self.waiting = {}
        self.queued = {}
        self._deadlines = []
        self._sequence = count()
        self._selector = selectors.DefaultSelector()
        try:
            self.wake_read, self.wake_write = socket.socketpair()
            self.wake_read.setblocking(False)
            self.wake_write.setblocking(False)
            self._selector.register(self.wake_read, selectors.EVENT_READ)
        except BaseException:
            self._close_selector()
            raise

    def _close_selector(self):
        self._selector.close()
        for sock in (getattr(self, 'wake_read', None), getattr(self, 'wake_write', None)):
            if sock is not None:
                sock.close()

    def add(self, pending):
        with self._lock:
            if self.router._closing.is_set():
                return False
            first = not self.incoming
            self.incoming.append(pending)
        if first:
            self.wake()
        return True

    def wake(self):
        try:
            self.wake_write.send(b'\0')
        except OSError:
            pass

    def _register_incoming(self):
        with self._lock:
            incoming, self.incoming = self.incoming, deque()
        for item in incoming:
            key = next(self._sequence)
            self.pending[key] = item
            try:
                item.sock.setblocking(False)
                self._selector.register(item.sock, selectors.EVENT_READ, key)
                item.registered = True
                self._schedule(item.deadline, key, False)
            except (OSError, ValueError):
                self._drop(key, item)

    def _schedule(self, deadline, key, queued):
        heappush(self._deadlines, (deadline, key, queued))
        # 最早项仍有效时，后面的失效项也最多占两倍原准入预算。
        if len(self._deadlines) > 2 * (self.router.pending_limit + self.router.queue_capacity):
            with self._lock:
                self._deadlines = [
                    entry for entry in self._deadlines
                    if entry[1] in (self.queued if entry[2] else self.pending)
                ]
            heapify(self._deadlines)

    def track_ticket(self, ticket):
        key = next(self._sequence)
        with self._lock:
            self.queued[key] = ticket
        self._schedule(ticket.enqueued_at + self.router.queue_timeout_seconds, key, True)
        return key

    def retire_ticket(self, key):
        with self._lock:
            self.queued.pop(key, None)

    def _expire_due(self, now):
        while self._deadlines:
            deadline, key, queued = self._deadlines[0]
            if queued:
                with self._lock:
                    item = self.queued.get(key)
            else:
                item = self.pending.get(key)
            # 同一序号只代表一次移交，晚到的过期事件无需查找旧的FD。
            if item is not None and deadline > now:
                return max(0.0, deadline - time.monotonic())
            heappop(self._deadlines)
            if item is None:
                continue
            if queued:
                if item.is_queued() and item.claim_expired():
                    item.reject_once(self.router.server)
                    item.future.cancel()
            else:
                self._drop(key, item)
        return None

    def _unregister(self, item):
        if item.registered:
            self._selector.unregister(item.sock)
            item.registered = False

    def _drop(self, key, item, *, close=True):
        self._unregister(item)
        del self.pending[key]
        if self.waiting.pop(key, None) is not None and not self.waiting:
            self.router.body_wait_ended(self)
        self.router.release_pending()
        if close:
            self.router.server.shutdown_request(item.sock)

    def _handoff(self, key, item, lane):
        self._drop(key, item, close=False)
        try:
            replay = PrefetchedSocket(item.sock, item.prefix)
        except BaseException:
            self.router.server.shutdown_request(item.sock)
            if lane == 'stream':
                self.router.release_lane(lane, self.router.server._request_slots)
            raise
        self.router._submit(lane, replay, item.addr, self)

    def _offer_body(self, key, item):
        if self.router.reserve_body(self):
            self._handoff(key, item, 'stream')
            return True
        self.waiting[key] = item
        return False

    def _read_ready(self, key, item):
        if time.monotonic() >= item.deadline:
            self._drop(key, item)
            return
        prefix = item.prefix
        try:
            data = item.sock.recv(HTTP_READY_PREFIX_BYTES - len(prefix))
        except BlockingIOError:
            return
        except OSError:
            self._drop(key, item)
            return
        if not data:
            self._drop(key, item)
            return
        prefix.extend(data)
        if item.lane is None:
            if b'\n' not in data and len(prefix) < HTTP_CONTROL_PEEK_BYTES:
                return
            item.lane = classify_peeked_request(prefix)
        if item.lane == 'control':
            self._handoff(key, item, control_sublane(prefix))
            return
        # 单 worker 下不留完整请求专用容量，保留原先首行分类后尽早交给处理器的行为。
        if not self.router.body_readiness:
            self._handoff(key, item, 'general')
            return
        if item.length is None:
            item.length = complete_request_length(
                prefix, single_request=self.router.single_request,
                search_from=max(0, len(prefix) - len(data) - 3),
            )
        if item.length and len(prefix) >= item.length:
            self._handoff(key, item, 'general')
        elif item.length is not None or len(prefix) == HTTP_READY_PREFIX_BYTES:
            if not self._offer_body(key, item) and len(prefix) == HTTP_READY_PREFIX_BYTES:
                # 前缀预算已满；下一次只响应容量释放或到期，避免可读FD忙转。
                self._unregister(item)

    def _drain_wake(self):
        try:
            while self.wake_read.recv(HTTP_CONTROL_PEEK_BYTES):
                pass
        except BlockingIOError:
            pass

    def run(self):
        try:
            while not self.router._closing.is_set():
                self._register_incoming()
                timeout = self._expire_due(time.monotonic())
                for key, _mask in self._selector.select(timeout):
                    if self.router._closing.is_set():
                        break
                    if key.data is None:
                        self._drain_wake()
                        while self.waiting:
                            pending_key = next(iter(self.waiting))
                            pending = self.waiting[pending_key]
                            if time.monotonic() >= pending.deadline:
                                self._drop(pending_key, pending)
                            elif not self._offer_body(pending_key, pending):
                                break
                    elif (item := self.pending.get(key.data)) is not None:
                        self._read_ready(key.data, item)
        finally:
            self.router._stop_admission()
            with self._lock:
                incoming, self.incoming = self.incoming, deque()
                tickets = tuple(self.queued.values())
            try:
                for item in incoming:
                    self.router.release_pending()
                    self.router.server.shutdown_request(item.sock)
                for key, item in tuple(self.pending.items()):
                    self._drop(key, item)
                for ticket in tickets:
                    ticket.future.cancel()
            finally:
                self._deadlines.clear()
                self._close_selector()


class HTTPLaneRouter:
    """分片观察明文连接，按原车道提交并独立计算排队期限。"""

    def __init__(self, server, *, control_workers: int, pending_limit: int,
                 header_timeout_seconds: float, queue_timeout_seconds: float, control_capacity: int,
                 max_workers: int, drain_runtime=None):
        self.server = server
        self.pending_limit = pending_limit
        self.header_timeout_seconds = header_timeout_seconds
        self.queue_timeout_seconds = queue_timeout_seconds
        self._closing = threading.Event()
        self._pending_slots = threading.BoundedSemaphore(pending_limit)
        self._body_lock = threading.Lock()
        self._body_waiters = {}
        self._streaming = 0
        self.body_readiness = max_workers > 1
        self._body_limit = max(1, max_workers - 1)
        self.single_request = bool(getattr(server.RequestHandlerClass, 'single_request_connection', False)
                                   or server.RequestHandlerClass.protocol_version == 'HTTP/1.0')
        self._lane_executors = {}
        self._lane_slots = {}
        self.queue_capacity = server.max_outstanding_requests
        server._request_executor._work_queue = _CancellableWorkQueue()
        if control_workers:
            for lane, workers in (('control', control_workers), ('status', HTTP_FAST_LANE_WORKERS_COUNT),
                                  ('stop', HTTP_FAST_LANE_WORKERS_COUNT)):
                executor = DurableDaemonThreadPoolExecutor(max_workers=workers, thread_name_prefix=f'gateway-http-{lane}')
                executor._work_queue = _CancellableWorkQueue()
                self._lane_executors[lane] = executor
                self._lane_slots[lane] = threading.BoundedSemaphore(control_capacity)
                self.queue_capacity += control_capacity
        self._control_executor = self._lane_executors.get('control')
        self._status_executor = self._lane_executors.get('status')
        self._stop_executor = self._lane_executors.get('stop')

        self.shards = []
        try:
            # 冻结合约按每 32 个普通 worker 建一个选择器，最多八片。
            for index in range(min(8, max(1, max_workers // 32))):
                shard = _ReadinessShard(self)
                self.shards.append(shard)
                shard.thread = threading.Thread(target=shard.run, name=f'gateway-http-readiness-{index}', daemon=True)
                group = (drain_runtime.retain_thread('control', f'http/readiness/{index}', shard.thread)
                         if drain_runtime is not None else None)
                try:
                    shard.thread.start()
                except BaseException as error:
                    if group is not None:
                        group.retain_fault(error)
                    raise
        except BaseException:
            self.close()
            raise

    def admit(self, request, client_address):
        if not self._closing.is_set() and self._pending_slots.acquire(blocking=False):
            shard = self.shards[request.fileno() % len(self.shards)]
            if shard.add(_Pending(request, client_address, time.monotonic() + self.header_timeout_seconds)):
                return
            self.release_pending()
        self.server._reject_without_wait(request)

    def release_pending(self):
        self._pending_slots.release()

    def reserve_body(self, shard):
        # 未收完整帧的工作最多占用 N-1 个普通席位，完整帧仍能使用原池中的全部 N 个线程。
        with self._body_lock:
            # 先登记等待者再试容量，避免归还线程看到空表后漏掉此次唤醒。
            self._body_waiters.setdefault(shard, None)
            if (self._streaming < self._body_limit
                    and self.server._request_slots.acquire(blocking=False)):
                self._streaming += 1
                if not shard.waiting:
                    self._body_waiters.pop(shard, None)
                return True
            return False

    def release_lane(self, lane, slots):
        slots.release()
        if slots is self.server._request_slots and (lane == 'stream' or self._body_waiters):
            self._wake_body_waiter(released_stream=lane == 'stream')

    def body_wait_ended(self, shard):
        with self._body_lock:
            self._body_waiters.pop(shard, None)
        # 若被唤醒的最后一条连接已收齐正文或退出，把可用席位的通知交给下一片。
        self._wake_body_waiter()

    def _wake_body_waiter(self, *, released_stream=False):
        with self._body_lock:
            if released_stream:
                self._streaming -= 1
            if self._streaming >= self._body_limit or not self.server._request_slots._value:
                return
            shard = next(iter(self._body_waiters), None)
            if shard is not None:
                del self._body_waiters[shard]
        if shard is not None:
            shard.wake()

    def _submit(self, lane, sock, addr, shard):
        executor = self._lane_executors.get(lane, self.server._request_executor)
        slots = self._lane_slots.get(lane, self.server._request_slots)
        if lane != 'stream' and not slots.acquire(blocking=False):
            self.server._reject_without_wait(sock)
            return
        if self._closing.is_set():
            self.release_lane(lane, slots)
            self.server.shutdown_request(sock)
            return
        ticket = _LaneTicket(sock, addr, lane)
        ticket.executor = executor
        try:
            sock.settimeout(self.server.request_socket_timeout)
            with self.server._lane_birth():
                ticket.enqueued_at = time.monotonic()
                ticket.future = executor.submit(self._run_ticket, ticket)
        except BaseException as error:
            # 线程出生可在入队之后失败；先撤销票据，再放行已经取到它的 worker。
            ticket.claim_expired()
            try:
                executor._work_queue.discard(ticket)
            finally:
                ticket._ready.set()
                self.release_lane(lane, slots)
                if isinstance(error, (RuntimeError, OSError, HTTPBirthGateBusy)):
                    ticket.reject_once(self.server)
                else:
                    ticket.close_once(self.server)
            if self.server._request_worker_failure is not None and not isinstance(error, HTTPBirthGateBusy):
                self.server._request_worker_failure(error)
            if not isinstance(error, (RuntimeError, OSError, HTTPBirthGateBusy)):
                raise
            return
        key = shard.track_ticket(ticket)
        ticket.future.add_done_callback(partial(self._ticket_finished, ticket, slots, shard, key))
        ticket._ready.set()

    def _run_ticket(self, ticket):
        ticket._ready.wait()
        start = time.monotonic()
        if start >= ticket.enqueued_at + self.queue_timeout_seconds:
            if ticket.claim_expired():
                ticket.reject_once(self.server)
            return
        if not ticket.claim_running():
            return
        token = lane_queue_ms.set(int((start - ticket.enqueued_at) * 1000))
        try:
            self.server._finish_http_request(ticket.request, ticket.client_address)
        finally:
            lane_queue_ms.reset(token)
            ticket.close_once(self.server)

    def _ticket_finished(self, ticket, slots, shard, key, future):
        try:
            if future.cancelled():
                ticket.executor._work_queue.discard(future)
                ticket.close_once(self.server)
            ticket.mark_done()
        finally:
            shard.retire_ticket(key)
            self.release_lane(ticket.lane, slots)

    def _stop_admission(self):
        self._closing.set()
        for shard in self.shards:
            shard.wake()

    def close(self):
        self._stop_admission()
        for shard in self.shards:
            thread = getattr(shard, 'thread', None)
            if thread is None or thread.ident is None:
                shard._close_selector()
            else:
                thread.join()
        for executor in self._lane_executors.values():
            executor.shutdown(wait=False, cancel_futures=True)
