# LLM: 仅为无排空运行时的明文连接分类；鉴权和业务授权仍由原处理器负责。
# 模块用途: 用单选择器等待首字节，为控制请求保留独立线程和容量，并主动回收排队超时。
from __future__ import annotations

import selectors
import socket
import threading
import time
from collections import deque
from functools import partial
from queue import Queue

from ..concurrency import DurableDaemonThreadPoolExecutor
from .http_routes import match_gateway_http_route

# 就绪分类时最多窥视的请求首行字节数：只用于选择车道，首行更长时按普通车道处理，不影响请求本身。
HTTP_CONTROL_PEEK_BYTES = 8192
# 控制车道只识别原路由模板，不赋予调用者身份或业务权限。
HTTP_CONTROL_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ('GET', '/status'), ('GET', '/metrics'), ('GET', '/control-status/*'),
    ('POST', '/control'), ('POST', '/client/agent-stop'), ('POST', '/client/agent-guidance'),
    ('POST', '/client/agent-permission'), ('POST', '/stop'),
})


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
    def __init__(self, request, client_address, accepted_at, lane):
        self.request = request
        self.client_address = client_address
        self.accepted_at = accepted_at
        self.lane = lane
        self.future = None
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


# LLM: 3.14t 的 mimalloc 按线程保留峰值；FIFO 唤醒会把短请求轮转到池内每个空闲线程，最终每个线程都长到峰值。
#   这里只改“哪个等待线程接手”（最近空闲者优先），条目仍按 Queue 原 FIFO 出队，容量、超时与取消语义不变。
# 类用途: 让 put 唤醒最近进入等待的消费者，使短请求集中在少数热线程上。
class _LifoWakeCondition(threading.Condition):
    # 函数用途: 与标准 notify 相同的唤醒/计数规则，只从等待表尾部（最近等待者）开始。
    def notify(self, n: int = 1) -> None:
        if not self._is_owned():
            raise RuntimeError("cannot notify on un-acquired lock")
        waiters = self._waiters
        while waiters and n > 0:
            waiter = waiters.pop()
            try:
                waiter.release()
            except RuntimeError:
                continue  # 已被前一次 notify 释放但尚未移除的等待者，不计入本次唤醒。
            n -= 1


# LLM: 原daemon执行器沿用CPython WorkItem；Future取消本身不会删除排队项，必须在归还槽位前移除。
# 类用途: 为这两个运输池提供可按原对象身份丢弃的工作队列，避免取消占位随历史增长。
class _CancellableWorkQueue(Queue):
    # 函数用途: 建立原 Queue 后只把 not_empty 换成最近空闲优先的唤醒条件，共用同一把 mutex。
    def __init__(self, maxsize: int = 0) -> None:
        super().__init__(maxsize)
        self.not_empty = _LifoWakeCondition(self.mutex)

    # LLM: 仅删除尚未被worker取走的原WorkItem，不调用Future或用户代码；运行中的项由原CAS处理。
    # 函数用途: 在取消或提交中断后移除对应的物理排队项。
    def discard(self, owner) -> None:
        with self.mutex:
            for item in self.queue:
                args = getattr(item, 'args', ())
                # CPython新worker context将原(fn, args, kwargs)保存在task中。
                task = getattr(item, 'task', None)
                if not args and isinstance(task, tuple) and len(task) == 3:
                    args = task[1]
                if getattr(item, 'future', None) is owner or (args and args[0] is owner):
                    self.queue.remove(item)
                    self.unfinished_tasks -= 1
                    self.not_full.notify()
                    return


# LLM: selector仅由readiness线程注册和注销；锁仅保护有界队列与表，不跨submit或handler。
# 类用途: 等待首字节后把明文连接送往独立的控制或普通车道。
class HTTPLaneRouter:
    # LLM: 容量与期限由组合根注入；唤醒使用跨平台socketpair，不增加周期轮询。
    # 函数用途: 建立独立控制池并启动唯一就绪观察线程。
    def __init__(self, server, *, control_workers: int, pending_limit: int,
                 header_timeout_seconds: float, queue_timeout_seconds: float, control_capacity: int):
        self.server = server
        self.pending_limit = pending_limit
        self.header_timeout_seconds = header_timeout_seconds
        self.queue_timeout_seconds = queue_timeout_seconds
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._incoming = deque()
        self._pending = {}
        self._queued = {'general': deque(), 'control': deque()}
        self._selector = selectors.DefaultSelector()
        self._wake_read, self._wake_write = socket.socketpair()
        self._wake_read.setblocking(False)
        self._wake_write.setblocking(False)
        self._selector.register(self._wake_read, selectors.EVENT_READ, None)
        self._control_executor = DurableDaemonThreadPoolExecutor(
            max_workers=control_workers, thread_name_prefix='gateway-http-control')
        # 构造期尚无worker，队列必须在首次submit前固定，原执行器及类属性不变。
        server._request_executor._work_queue = _CancellableWorkQueue()
        self._control_executor._work_queue = _CancellableWorkQueue()
        self._control_slots = threading.BoundedSemaphore(control_capacity)
        self._thread = threading.Thread(target=self._loop, name='gateway-http-readiness', daemon=True)
        self._thread.start()

    # LLM: 接收线程不读客户端，也不等待容量；满载时只尝试一次非阻塞写回。
    # 函数用途: 有界登记新连接并唤醒选择器。
    def admit(self, request, client_address) -> None:
        with self._lock:
            rejected = self._closing.is_set() or len(self._pending) + len(self._incoming) >= self.pending_limit
            if not rejected:
                self._incoming.append((request, client_address, time.monotonic()))
        if rejected:
            self.server._reject_without_wait(request)
        else:
            self._wake()

    # LLM: 唤醒字节仅是提示，满缓冲意味着已有唤醒待处理；关闭后的写失败无新工作。
    # 函数用途: 非阻塞通知选择器重读内存状态。
    def _wake(self) -> None:
        try:
            self._wake_write.send(b'\0')
        except OSError:
            pass

    # LLM: 每次从incoming移入pending保持同一锁内计数，避免临时低估容量。
    # 函数用途: 仅在选择器线程注册已接受的套接字。
    def _register_incoming(self) -> None:
        while True:
            with self._lock:
                if not self._incoming or self._closing.is_set():
                    return
                sock, addr, accepted_at = self._incoming.popleft()
                self._pending[sock] = (addr, accepted_at)
            try:
                sock.setblocking(False)
                self._selector.register(sock, selectors.EVENT_READ, (addr, accepted_at))
            except (OSError, ValueError):
                self._drop_pending(sock)

    # LLM: pending只由选择器增删；实际关闭不持路由锁。
    # 函数用途: 注销一个等待首字节的连接并可选关闭。
    def _drop_pending(self, sock, *, close=True) -> None:
        try:
            self._selector.unregister(sock)
        except (KeyError, ValueError):
            pass
        with self._lock:
            self._pending.pop(sock, None)
        if close:
            self.server.shutdown_request(sock)

    # LLM: 从未分类表移交时不消费请求字节，未读满的首行交回普通handler继续读取。
    # 函数用途: 处理一次可读通知并把可处理连接送入车道。
    def _read_ready(self, sock, addr, accepted_at) -> None:
        if time.monotonic() >= accepted_at + self.header_timeout_seconds:
            self._drop_pending(sock)
            return
        try:
            data = sock.recv(HTTP_CONTROL_PEEK_BYTES, socket.MSG_PEEK)
        except BlockingIOError:
            return
        except OSError:
            self._drop_pending(sock)
            return
        lane = classify_peeked_request(data)
        self._drop_pending(sock, close=lane == 'eof')
        if lane != 'eof':
            self._submit(lane, sock, addr, accepted_at)

    # LLM: 期限取已接受时间；队列只保留QUEUED，避免长worker挡住队首后积累完成历史。
    # 函数用途: 回收过期连接和票据，并计算最近的真实截止时间。
    def _sweep(self) -> float | None:
        now = time.monotonic()
        with self._lock:
            pending = tuple(self._pending.items())
            queued = {lane: tuple(tickets) for lane, tickets in self._queued.items()}
        deadlines = []
        for sock, (_addr, accepted_at) in pending:
            deadline = accepted_at + self.header_timeout_seconds
            if deadline <= now:
                self._drop_pending(sock)
            else:
                deadlines.append(deadline)
        for lane, tickets in queued.items():
            keep = deque()
            for ticket in tickets:
                deadline = ticket.accepted_at + self.queue_timeout_seconds
                if deadline <= now and ticket.claim_expired():
                    ticket.reject_once(self.server)
                    ticket.close_once(self.server)
                    ticket.future.cancel()
                if ticket.is_queued():
                    keep.append(ticket)
                    deadlines.append(deadline)
            with self._lock:
                self._queued[lane] = keep
        return max(0.0, min(deadlines) - time.monotonic()) if deadlines else None

    # LLM: worker状态变化也唤醒，空闲时select(None)，不新增tick常量。
    # 函数用途: 串行处理注册、就绪通知与期限，到关闭时回收选择器自有资源。
    def _loop(self) -> None:
        try:
            while not self._closing.is_set():
                self._register_incoming()
                timeout = self._sweep()
                for key, _mask in self._selector.select(timeout):
                    if key.data is None:
                        try:
                            while self._wake_read.recv(HTTP_CONTROL_PEEK_BYTES):
                                pass
                        except BlockingIOError:
                            pass
                    elif not self._closing.is_set():
                        self._read_ready(key.fileobj, *key.data)
        finally:
            with self._lock:
                self._closing.set()
                incoming, self._incoming = self._incoming, deque()
                pending = tuple(self._pending)
                tickets = [ticket for queue in self._queued.values() for ticket in queue]
                self._queued = {'general': deque(), 'control': deque()}
            for sock, _addr, _accepted_at in incoming:
                self.server.shutdown_request(sock)
            for sock in pending:
                self._drop_pending(sock)
            for ticket in tickets:
                ticket.future.cancel()
            self._selector.close()
            self._wake_read.close()
            self._wake_write.close()

    # LLM: 两车道分别非阻塞领取自己的容量；submit与错误上报均在路由锁外。
    # 函数用途: 将有首字节的连接移交固定池并登记一次性释放回调。
    def _submit(self, lane, sock, addr, accepted_at) -> None:
        executor = self._control_executor if lane == 'control' else self.server._request_executor
        slots = self._control_slots if lane == 'control' else self.server._request_slots
        if self._closing.is_set() or not slots.acquire(blocking=False):
            self.server._reject_without_wait(sock)
            return
        ticket = _LaneTicket(sock, addr, accepted_at, lane)
        try:
            sock.settimeout(self.server.request_socket_timeout)
            ticket.future = executor.submit(self._run_ticket, ticket)
        except BaseException as error:
            # submit可能先入队再在线程出生时报错；未确认的worker只能空退，不能触碰连接。
            ticket.claim_expired()
            try:
                executor._work_queue.discard(ticket)
            finally:
                ticket._ready.set()
                slots.release()
                if isinstance(error, (RuntimeError, OSError)):
                    ticket.reject_once(self.server)
                else:
                    ticket.close_once(self.server)
            ticket.mark_done()
            if self.server._request_worker_failure is not None:
                self.server._request_worker_failure(error)
            if not isinstance(error, (RuntimeError, OSError)):
                raise
            return
        ticket.future.add_done_callback(partial(self._ticket_finished, ticket, slots))
        with self._lock:
            self._queued[lane].append(ticket)
        ticket._ready.set()

    # LLM: CAS失败的worker不接触socket；只有成功领取者进入原handler和原异常策略。
    # 函数用途: 执行一次已分类的普通请求或控制请求。
    def _run_ticket(self, ticket) -> None:
        ticket._ready.wait()
        # worker取到任务时再核期限，不能依赖选择器恰好先执行sweep。
        if time.monotonic() - ticket.accepted_at >= self.queue_timeout_seconds:
            if ticket.claim_expired():
                ticket.reject_once(self.server)
                ticket.mark_done()
            return
        if not ticket.claim_running():
            return
        self._wake()
        try:
            self.server.finish_request(ticket.request, ticket.client_address)
        except (TimeoutError, ConnectionError):
            pass
        except Exception:
            self.server.handle_error(ticket.request, ticket.client_address)
        finally:
            ticket.close_once(self.server)
            ticket.mark_done()

    # LLM: Future回调是成功提交后的唯一释放者，取消时也不进入处理器。
    # 函数用途: 回收一次连接及容量并通知选择器移除完成票据。
    def _ticket_finished(self, ticket, slots, future) -> None:
        try:
            if future.cancelled():
                executor = self._control_executor if ticket.lane == 'control' else self.server._request_executor
                executor._work_queue.discard(future)
                ticket.close_once(self.server)
            ticket.mark_done()
        finally:
            slots.release()
            self._wake()

    # LLM: 先关准入并唤醒，再等待有界选择器退出；运行中的handler沿原服务器生命周期收口。
    # 函数用途: 关闭等待中的连接并停止独立控制执行器。
    def close(self) -> None:
        with self._lock:
            self._closing.set()
        self._wake()
        self._thread.join(timeout=self.header_timeout_seconds)
        self._control_executor.shutdown(wait=False, cancel_futures=True)
