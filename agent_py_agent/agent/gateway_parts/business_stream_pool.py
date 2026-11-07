# LLM: 仅运输已鉴权的原生事件迭代器；不导入业务schema，不改变任务状态或原插件流配额。
# 模块用途: 为原生长流提供独立有界线程、owner预留额度及可立即唤醒的停止令牌。
from __future__ import annotations

import select
import socket
import threading
from collections import Counter
from collections.abc import Mapping

from ..concurrency import DurableDaemonThreadPoolExecutor
from ..conversation.business_access import BusinessError
from ..plugin_host.errors import HostAPIError
from .http_lanes import _CancellableWorkQueue


# LLM: 停止只结束观察运输，不取消被观察任务。
# 类用途: 中断流等待并让writer沿finally释放资源。
class NativeStreamStopped(Exception):
    pass


# LLM: 票据从handler预留到writer退出一直占位，关闭socket与释放额度分开。
# 类用途: 保存一个owner的预留额度、停止事件及移交后的连接。
class StreamTicket:
    # LLM: 创建时尚未移交fd；池锁不跨票据锁或套接字操作。
    # 函数用途: 建立一次流预留的生命周期。
    def __init__(self, pool, owner_key):
        self.owner_key = owner_key
        self.stop_event = threading.Event()
        self._pool = pool
        self._lock = threading.Lock()
        self._sock = None
        self._released = False
        self._submitted = False
        self._ready = threading.Event()
        self._submission_aborted = False

    # LLM: close可能先于detach完成，晚到的连接也必须立即停止。
    # 函数用途: 绑定移交的socket并复核已发出的停止信号。
    def attach(self, sock: socket.socket) -> None:
        with self._lock:
            self._sock = sock
        if self.stop_event.is_set():
            self.stop()

    # LLM: 只窥视EOF，保留客户端字节；所有等待可被同一停止事件唤醒。
    # 函数用途: 等待原心跳期限，并在停机或客户端离开时中断迭代。
    def wait(self, timeout: float) -> None:
        if self.stop_event.is_set():
            raise NativeStreamStopped()
        with self._lock:
            sock = self._sock
        if sock is not None:
            try:
                readable, _, _ = select.select([sock], [], [], 0)
                closed = bool(readable) and sock.recv(1, socket.MSG_PEEK) == b''
            except (OSError, ValueError):
                closed = True
            if closed:
                self.stop()
                raise NativeStreamStopped()
        if self.stop_event.wait(max(0.0, timeout)):
            raise NativeStreamStopped()

    # LLM: shutdown唤醒正在sendall的writer，只有writer清理才关闭fd和释放额度。
    # 函数用途: 非等待地停止观察运输。
    def stop(self) -> None:
        self.stop_event.set()
        with self._lock:
            sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # LLM: handler失败与writer收口共享幂等释放，不能提前归还仍在运行的流。
    # 函数用途: 在调用方已完成资源清理后归还预留额度。
    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._pool._release(self)


# LLM: 池锁只保护预留集合、owner计数及设施引用，不跨submit、迭代或socket写入。
# 类用途: 限制全局和单owner长流数量并管理独立writer执行器。
class BusinessStreamPool:
    # LLM: 大小与写超时由组合根读取配置后显式传入，构造不创建线程。
    # 函数用途: 初始化尚未启动的流池。
    def __init__(self, *, max_streams: int, per_owner_max: int, write_timeout_seconds: float):
        if max_streams <= 0 or per_owner_max <= 0:
            raise ValueError('stream_capacity_required')
        self.max_streams = max_streams
        self.per_owner_max = per_owner_max
        self.write_timeout_seconds = write_timeout_seconds
        self._lock = threading.Lock()
        self._tickets = set()
        self._owners = Counter()
        self._closed = False
        self._executor = None

    # LLM: executor构造不持预留锁；与关闭竞争时关闭未发表设施，不能重新开放准入。
    # 函数用途: 显式启动独立daemon流执行器。
    def start(self) -> None:
        executor = DurableDaemonThreadPoolExecutor(
            max_workers=self.max_streams, thread_name_prefix='gateway-native-stream')
        executor._work_queue = _CancellableWorkQueue()
        with self._lock:
            refused = self._closed or self._executor is not None
            if not refused:
                self._executor = executor
        if refused:
            executor.shutdown(wait=False, cancel_futures=True)
            raise RuntimeError('stream_pool_already_started_or_closed')

    # LLM: owner键只接受调用方已鉴权的home身份，流生产和首帧都必须晚于成功预留。
    # 函数用途: 原子领取全局和owner两层额度。
    def reserve(self, owner_key: tuple[str, str]) -> StreamTicket | None:
        with self._lock:
            if (self._closed or self._executor is None or len(self._tickets) >= self.max_streams
                    or self._owners[owner_key] >= self.per_owner_max):
                return None
            ticket = StreamTicket(self, owner_key)
            self._tickets.add(ticket)
            self._owners[owner_key] += 1
            return ticket

    # LLM: 可选编码和校验闭包由HTTP适配器提供；三参数调用可直接运输已编码字节。
    # 函数用途: 确认只提交一次writer，并在提交中断时保留handler清理权。
    def submit(self, ticket, sock, iterator, encode=None, check=None) -> None:
        with self._lock:
            if self._closed or ticket not in self._tickets or ticket._submitted:
                raise RuntimeError('stream_reservation_unavailable')
            ticket._submitted = True
            executor = self._executor
        try:
            executor.submit(self._run_stream, ticket, sock, iterator,
                encode if encode is not None else lambda event: event,
                check if check is not None else lambda event: None)
        except BaseException:
            # 队列接收后线程创建也可能失败；先撤销移交，再允许占位worker空退。
            ticket._submission_aborted = True
            try:
                executor._work_queue.discard(ticket)
            finally:
                ticket._ready.set()
            raise
        ticket._ready.set()

    # LLM: 只有writer推进迭代器，停止或传输/授权异常只结束观察，不重试帧。
    # 函数用途: 校验并写出后续每一帧，最终释放该流的全部资源。
    def _run_stream(self, ticket, sock, iterator, encode, check) -> None:
        ticket._ready.wait()
        if ticket._submission_aborted:
            return
        try:
            while True:
                # 每次推进前都复核停止，不能在上一帧发送后无检查地进入下一次阻塞next。
                ticket.wait(0)
                try:
                    event = next(iterator)
                except StopIteration:
                    break
                if ticket.stop_event.is_set():
                    raise NativeStreamStopped()
                check(event)
                sock.sendall(encode(event))
        except (NativeStreamStopped, OSError, HostAPIError, BusinessError):
            pass
        finally:
            self._cleanup(ticket, sock, iterator)

    # LLM: 迭代器close异常也不能跳过socket清理或额度归还；释放必须最后发生。
    # 函数用途: 按迭代器、连接、预留的固定顺序结束一个writer。
    def _cleanup(self, ticket, sock, iterator) -> None:
        try:
            close = getattr(iterator, 'close', None)
            if callable(close):
                close()
        finally:
            try:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
            finally:
                ticket.release()

    # LLM: 票据自身先作幂等领取；此处只改预留计数，不做任何I/O。
    # 函数用途: 从活跃集合删除已清理的流。
    def _release(self, ticket) -> None:
        with self._lock:
            if ticket in self._tickets:
                self._tickets.remove(ticket)
                self._owners[ticket.owner_key] -= 1
                if not self._owners[ticket.owner_key]:
                    del self._owners[ticket.owner_key]

    # LLM: 活跃数包含已预留但未移交及已停止但尚未退出的writer。
    # 函数用途: 返回当前仍占用额度的流数。
    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._tickets)

    # LLM: 先关准入，锁外停止全部票据；shutdown不等待可能阻塞的业务来源。
    # 函数用途: 唤醒等待和写入中的流，让排队writer也在自己的线程内关闭迭代器。
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            tickets = tuple(self._tickets)
            executor = self._executor
        for ticket in tickets:
            ticket.stop()
        if executor is not None:
            # 不在关闭线程取消已移交流，否则Future回调会在错误线程执行iterator.close。
            executor.shutdown(wait=False, cancel_futures=False)


# LLM: 每条流独享等待视图，共享服务、缓存及其event_signal不得被修改。
# 类用途: 将属性型服务的等待入口绑定到本次流停止票据。
class _StreamServicesView:
    # LLM: 保留原服务引用，其余属性按需透传。
    # 函数用途: 创建单流停止视图。
    def __init__(self, services, ticket):
        self._services = services
        import time
        self.wait = ticket.wait if ticket is not None else getattr(services, 'wait', time.sleep)
        factory = getattr(getattr(services, 'host', None), 'business_event_signal_factory', None)
        signal = factory() if callable(factory) else getattr(services, 'event_signal', None)
        self.event_signal = _StopAwareSignal(signal, ticket) if signal is not None and ticket is not None else signal

    # LLM: 不复制或持久化业务服务，不覆盖原授权属性。
    # 函数用途: 读取未被流专用等待入口覆盖的原属性。
    def __getattr__(self, name):
        return getattr(self._services, name)


# LLM: 映射返回浅拷贝，属性型服务返回代理；两者都不修改owner共享对象。
# 函数用途: 为一条流注入停止感知等待而保留原业务读取入口。
def stream_services_view(services, ticket):
    if isinstance(services, Mapping):
        signal = services.get('event_signal')
        return {**services, 'wait': ticket.wait if ticket is not None else services.get('wait'),
                'event_signal': _StopAwareSignal(signal, ticket) if signal is not None and ticket is not None else signal}
    return _StreamServicesView(services, ticket)


# LLM: Stop is transport-only; do not erase owner notifications or mutate shared services. Both before and after wait recheck the original ticket.
# 类用途: 让每条流的唤醒信号同时感知本连接停止，关连接不会取消业务。
class _StopAwareSignal:
    # LLM: Retain only this stream's ticket and notification view, no new threads or sockets.
    # 函数用途: 为当前流绑定原停止票据。
    def __init__(self, signal, ticket):
        self.signal, self.ticket = signal, ticket

    # LLM: The hub waits outside its leaf lock, with bounded checks of the original stop event.
    # 函数用途: 事件、超时或连接关闭后返回，并保持原NativeStreamStopped语义。
    def wait(self, timeout):
        self.ticket.wait(0)
        result = self.signal.wait(timeout, self.ticket.stop_event)
        self.ticket.wait(0)
        return result
