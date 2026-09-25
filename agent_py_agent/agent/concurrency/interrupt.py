# LLM: per-thread 协作中断；可选精确句柄在 worker 启动前保留取消事实，且跟踪清理直到真实退出。
#   契约:①中断按线程隔离——同进程多 agent 并发时,取消一个会话不影响其他
#   会话正在跑的工具(gateway 场景的关键);②中断状态仍在安全点轮询，
#   但阻塞传输可以注册幂等关闭回调，不强杀线程;③register_
#   interruptible 的 finally 必清标志+注销名字——线程池 ident 会复用,脏标志
#   会让下一个任务莫名"被中断";标志同时记住立旗时的线程对象,线程已退出或 ident
#   换了主人即视为过期,给已退出线程立旗直接落空(关闭竞态里晚到的立旗不再残留);④这里只覆盖进程内线程，独立 runner 的原
#   session 心跳读取持久取消事实，再在其宿主内转交精确 attempt 的中断。
#   改动时同步检查 round_execution 轮询点、background/dispatch worker 注册、
#   subagents/cancellation.py 接线与 tests/test_thread_interrupt.py。
# 模块用途: 统一线程停止与阻塞连接关闭；普通控制沿原路径，精确句柄支持无等待取消及清理去重。
from __future__ import annotations

import threading
import weakref
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

_lock = threading.Lock()
# ident → 立旗时仍存活的那个线程对象（弱引用）；只按 ident 记会把旧停止误投给复用该 ident 的新线程。
_interrupted_threads: dict[int, weakref.ReferenceType[threading.Thread]] = {}
_named_threads: dict[str, set[int]] = {}
_interrupt_callbacks: dict[int, list[_InterruptCallback]] = {}
_thread_handles: dict[int, InterruptHandle] = {}
_thread_wakeups: dict[int, set[threading.Event]] = {}

# 会话运行时 cancels the turn token immediately, gives cooperative cleanup 100 ms,
# then aborts the task handle. Transport close hooks are advisory cleanup too:
# a slow socket close must never make an IM/CLI stop wait for the provider timeout.
_GRACEFUL_INTERRUPT_TIMEOUT_SECONDS = 0.1


# LLM: 每次回调注册独立计数，精确句柄只安排一次；不能按 callable 相等判断不同注册是否同一资源。
# 类用途: 保存当前关闭动作及是否已交给受跟踪清理线程。
@dataclass(eq=False)
class _InterruptCallback:
    callback: Callable[[], object]
    scheduled: bool = False


# LLM: 句柄独占一次执行，绑定/取消沿本模块同一锁；最多一个清理线程，退出前保留引用，不能重用或控制父线程。
# 类用途: 在 worker 启动前准备准确取消身份，支持立即返回的停止请求及晚注册连接的清理。
class InterruptHandle:
    # LLM: 构造无线程和控制副作用；取消 Event 是句柄事实，线程标志只是绑定期间的投影。
    # 函数用途: 建立尚未认领的取消句柄及清理状态。
    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._cleanup_wake = threading.Event()
        self._cleanup_done = threading.Event()
        self._cleanup_done.set()
        self._claimed = False
        self._bound = False
        self._closed = False
        self._tid: int | None = None
        self._pending: deque[_InterruptCallback] = deque()
        self._waiters: set[threading.Event] = set()
        self._cleanup_thread: threading.Thread | None = None
        self._cleanup_attempted = False

    # LLM: 认领必须在 worker 启动前，已取消或已用句柄不能成为另一次调用的身份；不清理已有取消事实。
    # 函数用途: 为通用有界调用保留一次性取消身份。
    def claim(self) -> None:
        with _lock:
            if self._claimed or self._closed:
                raise ValueError("取消句柄已经用于另一次调用")
            if self._cancelled.is_set():
                raise InterruptedError("当前调用已被取消")
            self._claimed = True

    # LLM: 此入口只标记本句柄和已绑定线程；关闭回调异步且每次注册只执行一次，不等待清理。
    # 函数用途: 立即取消准确 worker，包括尚未完成线程注册的调用。
    def cancel(self) -> None:
        with _lock:
            self._cancelled.set()
            for wake in self._waiters:
                wake.set()
            if self._tid is not None and _thread_handles.get(self._tid) is self:
                _flag_locked(self._tid)
                for wake in _thread_wakeups.get(self._tid, ()):
                    wake.set()
                for item in _interrupt_callbacks.get(self._tid, ()):
                    self._enqueue_locked(item)
        self._start_cleanup()

    # LLM: 只读准确句柄的不可撤销取消事实，不从父线程状态或异常文案推断。
    # 函数用途: 供启动、等待和结果消费安全点拒绝已取消调用。
    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    # LLM: completed Event 不能单独证明线程退出；未启动成功、待处理或仍存活的清理都保留占用。
    # 函数用途: 告诉资源登记表当前是否仍需持有清理名额。
    @property
    def cleanup_pending(self) -> bool:
        with _lock:
            return bool(self._pending) or not self._cleanup_done.is_set() or (
                self._cleanup_thread is not None and self._cleanup_thread.is_alive()
            )

    # LLM: 仅 worker 未启动时可关闭未绑定句柄；不能用 caller 超时提前关闭仍在运行的注册。
    # 函数用途: 回收启动失败或发送前取消的句柄生命周期，不等待清理。
    def close_unstarted(self) -> None:
        with _lock:
            if not self._bound:
                self._closed = True
                self._cleanup_wake.set()

    # LLM: 调用方持本模块锁；每个注册只入队一次，通知现有清理线程，不在锁内执行用户回调。
    # 函数用途: 把一个当前连接的关闭动作加入准确句柄的清理队列。
    def _enqueue_locked(self, item: _InterruptCallback) -> None:
        if not item.scheduled:
            item.scheduled = True
            self._pending.append(item)
            self._cleanup_wake.set()

    # LLM: 每个句柄最多安排一次清理线程；构造/启动失败保留未完成事实，不能释放资源或同步运行慢回调。
    # 函数用途: 有清理动作时启动受跟踪线程，重复取消沿原队列处理。
    def _start_cleanup(self) -> None:
        try:
            with _lock:
                if self._cleanup_attempted or not self._pending:
                    return
                self._cleanup_attempted = True
                self._cleanup_done.clear()
                worker = threading.Thread(target=self._clean, name="interrupt-handle-cleanup", daemon=True)
                self._cleanup_thread = worker
            worker.start()
        except Exception:
            return

    # LLM: 清理线程与 worker 共用准确句柄；晚注册动作仍可入队，worker 退出且队列耗尽前不宣称全部释放。
    # 函数用途: 串行执行本调用的关闭动作，直到调用注册结束并完成已排清理。
    def _clean(self) -> None:
        try:
            while True:
                with _lock:
                    item = self._pending.popleft() if self._pending else None
                    if item is None and self._closed:
                        return
                    self._cleanup_wake.clear()
                if item is None:
                    self._cleanup_wake.wait()
                else:
                    _run_interrupt_callbacks((item.callback,))
        finally:
            self._cleanup_done.set()


# LLM: 原线程控制保持，绑定精确句柄时取消沿句柄转交；所有关闭动作都在 registry 锁外执行。
#   目标线程已退出时立旗直接落空，不留下会被 ident 复用继承的脏标志。
# 函数用途: 给某线程立/撤中断旗，立旗时关闭当前阻塞连接。
def set_interrupt(active: bool, thread_id: int | None = None) -> None:
    tid = thread_id if thread_id is not None else threading.current_thread().ident
    if tid is None:
        return
    callbacks: tuple[Callable[[], object], ...] = ()
    handle = None
    with _lock:
        if active:
            if not _flag_locked(tid):
                return
            for wake in _thread_wakeups.get(tid, ()):
                wake.set()
            handle = _thread_handles.get(tid)
            callbacks = tuple(item.callback for item in _interrupt_callbacks.get(tid, ())) if handle is None else ()
        else:
            _interrupted_threads.pop(tid, None)
    if handle is not None:
        handle.cancel()
    _run_interrupt_callbacks_bounded(callbacks)


# LLM: 精确句柄的取消不可由普通清旗撤销；没有句柄时仍按原线程标志判断。
# 函数用途: 在工具或传输安全点查询当前执行的停止事实。
def is_interrupted() -> bool:
    tid = threading.current_thread().ident
    with _lock:
        handle = _thread_handles.get(tid)
        return _is_flagged_locked(tid) or (handle is not None and handle.cancelled)


# LLM: Retry/backoff waits share the same cancellation token semantics as
# blocking transports: /stop wakes the wait immediately instead of waiting for
# the complete backoff interval.
# 函数用途: 可中断地等待一段时间；当前任务收到停止信号就立刻抛出中断。
def wait_interruptibly(timeout_seconds: float) -> None:
    timeout = max(0.0, float(timeout_seconds or 0.0))
    if is_interrupted():
        raise InterruptedError("当前任务已被用户停止")
    if timeout <= 0:
        return
    wake = threading.Event()
    with register_interrupt_callback(wake.set):
        if is_interrupted():
            raise InterruptedError("当前任务已被用户停止")
        wake.wait(timeout)
        if is_interrupted():
            raise InterruptedError("当前任务已被用户停止")


# LLM: 命名控制仍覆盖全部匹配线程，精确句柄仅转交自身；锁外调度关闭，重复控制不新增句柄清理线程。
# 函数用途: 按任务名停止所有匹配的执行线程，并收回它们正在等待的模型连接。
def interrupt_by_name(name: str) -> bool:
    callbacks: list[Callable[[], object]] = []
    handles: set[InterruptHandle] = set()
    with _lock:
        tids = tuple(_named_threads.get(name) or ())
        if not tids:
            return False
        for tid in tids:
            _flag_locked(tid)
        for tid in tids:
            for wake in _thread_wakeups.get(tid, ()):
                wake.set()
            handle = _thread_handles.get(tid)
            if handle is not None:
                handles.add(handle)
            else:
                callbacks.extend(item.callback for item in _interrupt_callbacks.get(tid, ()))
    for handle in handles:
        handle.cancel()
    _run_interrupt_callbacks_bounded(tuple(callbacks))
    return True


# LLM: Control routing may inspect whether an exact execution token is live, without sending an
# interrupt as a probe or inferring activity from a durable task/checklist record.
# 函数用途: 只读判断某个命名运行轮是否已登记，供 `/stop` 精确定位当前执行。
def is_interruptible_registered(name: str) -> bool:
    with _lock:
        return bool(_named_threads.get(str(name or "")))


# LLM: 可选句柄必须先绑定并继承早到取消；退出注销精确关联，清理线程未结束的事实仍归原句柄。
# 函数用途: 登记一次 worker 的名字及可选准确身份，退出清理线程投影以免 ident 复用污染下一任务。
@contextmanager
def register_interruptible(name: str, *, handle: InterruptHandle | None = None) -> Iterator[None]:
    tid = threading.current_thread().ident
    _register_named(name, tid, handle)
    try:
        yield
    finally:
        _unregister_named(name, tid, handle)


# LLM: 等待唤醒只置 Event，不排在慢清理后；普通清旗不能覆盖句柄取消，显式句柄不改变父线程停止旗。
# 函数用途: 给有界等待登记即时取消唤醒，避免用户取消被慢 close 回调拖住。
@contextmanager
def register_interrupt_wakeup(wake: threading.Event, *, handle: InterruptHandle | None = None) -> Iterator[None]:
    tid = threading.get_ident()
    with _lock:
        waiters = handle._waiters if handle is not None else _thread_wakeups.setdefault(tid, set())
        waiters.add(wake)
        current = handle if handle is not None else _thread_handles.get(tid)
        cancelled = (handle is None and _is_flagged_locked(tid)) or (current is not None and current.cancelled)
        if cancelled:
            wake.set()
    try:
        yield
    finally:
        with _lock:
            waiters.discard(wake)
            if handle is None and not waiters:
                _thread_wakeups.pop(tid, None)


# LLM: 回调按注册记录唯一调度；精确句柄的早到取消异步清理晚注册资源，普通线程保留既有同步补通知行为。
# 函数用途: 让模型 HTTP 流等阻塞操作在收到同一任务的停止信号时主动关闭，而不是等完整超时。
@contextmanager
def register_interrupt_callback(callback: Callable[[], object]) -> Iterator[None]:
    tid = threading.current_thread().ident
    if tid is None:
        yield
        return
    call_now = False
    item = _InterruptCallback(callback)
    with _lock:
        _interrupt_callbacks.setdefault(tid, []).append(item)
        handle = _thread_handles.get(tid)
        call_now = _is_flagged_locked(tid) or (handle is not None and handle.cancelled)
        if call_now and handle is not None:
            handle._enqueue_locked(item)
    if call_now:
        if handle is not None:
            handle._start_cleanup()
        else:
            _run_interrupt_callbacks((callback,))
    try:
        yield
    finally:
        with _lock:
            callbacks = _interrupt_callbacks.get(tid)
            if callbacks is not None:
                try:
                    callbacks.remove(item)
                except ValueError:
                    pass
                if not callbacks:
                    _interrupt_callbacks.pop(tid, None)


# LLM: Abort hooks are advisory cleanup and cannot make the control endpoint fail.
# 函数用途: 安全调用阻塞资源的关闭动作；某个关闭失败时仍继续通知其他资源。
def _run_interrupt_callbacks(callbacks: tuple[Callable[[], object], ...]) -> None:
    for callback in callbacks:
        try:
            callback()
        except Exception:
            continue


def _run_interrupt_callbacks_bounded(callbacks: tuple[Callable[[], object], ...]) -> None:
    """Start transport cleanup now but never block a stop caller beyond 100 ms."""

    if not callbacks:
        return
    completed = threading.Event()

    def run() -> None:
        try:
            _run_interrupt_callbacks(callbacks)
        finally:
            completed.set()

    threading.Thread(target=run, name="interrupt-cleanup", daemon=True).start()
    completed.wait(_GRACEFUL_INTERRUPT_TIMEOUT_SECONDS)


# LLM: 名字与精确句柄在同一锁下绑定；句柄不可二次注册，既有取消事实必须投影到新 worker。
# 函数用途: 登记当前线程，同时封住取消早于线程启动的竞态。
def _register_named(name: str, tid: int | None, handle: InterruptHandle | None = None) -> None:
    if tid is None:
        return
    with _lock:
        if handle is not None:
            if handle._bound or handle._closed or tid in _thread_handles:
                raise ValueError("取消句柄不能重复绑定执行线程")
            handle._bound = handle._claimed = True
            handle._tid = tid
            _thread_handles[tid] = handle
            if handle.cancelled:
                _flag_locked(tid)
        _named_threads.setdefault(name, set()).add(tid)


# LLM: 精确句柄在 worker 注册结束时关闭，清理队列和句柄仍保留；线程投影与普通嵌套名称按原规则清理。
# 函数用途: 注销当前执行，通知其清理线程退出，避免线程 ID 重用导致误取消。
def _unregister_named(name: str, tid: int | None, handle: InterruptHandle | None = None) -> None:
    if tid is None:
        return
    with _lock:
        if handle is not None and _thread_handles.get(tid) is handle:
            _thread_handles.pop(tid, None)
            handle._tid = None
            handle._closed = True
            handle._cleanup_wake.set()
        tids = _named_threads.get(name)
        if tids is not None:
            tids.discard(tid)
            if not tids:
                _named_threads.pop(name, None)
        if not any(tid in registered for registered in _named_threads.values()):
            _interrupted_threads.pop(tid, None)
            _interrupt_callbacks.pop(tid, None)
            _thread_wakeups.pop(tid, None)


# LLM: 只给仍存活且 ident 对应的线程立旗并记住线程对象；目标已退出时清掉旧记录且不立旗。调用方持 _lock。
# 函数用途: 把一个线程标记为已中断，返回是否真的立了旗。
def _flag_locked(tid: int) -> bool:
    thread = _live_thread(tid)
    if thread is None:
        _interrupted_threads.pop(tid, None)
        return False
    _interrupted_threads[tid] = weakref.ref(thread)
    return True


# LLM: 只认立旗时的那个线程对象：线程已死或 ident 已换主人都视为过期并清除，复用 ident 的新线程不继承旧停止。调用方持 _lock。
# 函数用途: 判断某个线程 ident 当前是否处于中断状态。
def _is_flagged_locked(tid: int | None) -> bool:
    ref = _interrupted_threads.get(tid) if tid is not None else None
    if ref is None:
        return False
    thread = ref()
    if thread is not None and thread.is_alive() and _live_thread(tid) is thread:
        return True
    _interrupted_threads.pop(tid, None)
    return False


# LLM: 当前线程直接取 current_thread，其余 ident 在存活线程表里找；找不到即视为已退出，不读私有字段。
# 函数用途: 返回 ident 对应的存活线程对象，没有则返回 None。
def _live_thread(tid: int) -> threading.Thread | None:
    current = threading.current_thread()
    if current.ident == tid:
        return current
    return next((thread for thread in threading.enumerate() if thread.ident == tid), None)


__all__ = [
    "InterruptHandle",
    "interrupt_by_name",
    "is_interruptible_registered",
    "is_interrupted",
    "register_interrupt_callback",
    "register_interruptible",
    "register_interrupt_wakeup",
    "set_interrupt",
    "wait_interruptibly",
]
