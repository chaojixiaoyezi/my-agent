# LLM: per-thread 协作中断。
#   契约:①中断按线程隔离——同进程多 agent 并发时,取消一个会话不影响其他
#   会话正在跑的工具(gateway 场景的关键);②中断状态仍在安全点轮询，
#   但阻塞传输可以注册幂等关闭回调，不强杀线程;③register_
#   interruptible 的 finally 必清标志+注销名字——线程池 ident 会复用,脏标志
#   会让下一个任务莫名"被中断";④只覆盖进程内线程形态,独立进程形态照旧走
#   SIGTERM 升级链(subagents/process_control),gateway 停止语义不动。
#   改动时同步检查 round_execution 轮询点、background/dispatch worker 注册、
#   orchestration/tools/cancel.py 接线与 tests/test_thread_interrupt.py。
# 模块用途: 给"正在干活的线程"递一张暂停条；安全点体面收工，阻塞的模型连接则主动关闭。
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

_lock = threading.Lock()
_interrupted_threads: set[int] = set()
_named_threads: dict[str, set[int]] = {}
_interrupt_callbacks: dict[int, list[Callable[[], object]]] = {}

# 会话运行时 cancels the turn token immediately, gives cooperative cleanup 100 ms,
# then aborts the task handle. Transport close hooks are advisory cleanup too:
# a slow socket close must never make an IM/CLI stop wait for the provider timeout.
_GRACEFUL_INTERRUPT_TIMEOUT_SECONDS = 0.1


# LLM: Setting a typed interrupt also invokes that thread's idempotent transport abort hooks outside the registry lock.
# 函数用途: 给某线程立/撤中断旗；立旗时同时关闭该线程正在等待的阻塞连接。
def set_interrupt(active: bool, thread_id: int | None = None) -> None:
    tid = thread_id if thread_id is not None else threading.current_thread().ident
    if tid is None:
        return
    callbacks: tuple[Callable[[], object], ...] = ()
    with _lock:
        if active:
            _interrupted_threads.add(tid)
            callbacks = tuple(_interrupt_callbacks.get(tid) or ())
        else:
            _interrupted_threads.discard(tid)
    _run_interrupt_callbacks_bounded(callbacks)


# 函数用途: 当前线程被要求中断了吗?(工具循环安全点逐次轮询用)
def is_interrupted() -> bool:
    tid = threading.current_thread().ident
    with _lock:
        return tid in _interrupted_threads


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


# LLM: Named interruption fans out to every registered execution thread and invokes callbacks after releasing the shared lock.
# 函数用途: 按任务名停止所有匹配的执行线程，并收回它们正在等待的模型连接。
def interrupt_by_name(name: str) -> bool:
    callbacks: list[Callable[[], object]] = []
    with _lock:
        tids = tuple(_named_threads.get(name) or ())
        if not tids:
            return False
        _interrupted_threads.update(tids)
        for tid in tids:
            callbacks.extend(_interrupt_callbacks.get(tid) or ())
    _run_interrupt_callbacks_bounded(tuple(callbacks))
    return True


# LLM: Control routing may inspect whether an exact execution token is live, without sending an
# interrupt as a probe or inferring activity from a durable task/checklist record.
# 函数用途: 只读判断某个命名运行轮是否已登记，供 `/stop` 精确定位当前执行。
def is_interruptible_registered(name: str) -> bool:
    with _lock:
        return bool(_named_threads.get(str(name or "")))


# 函数用途: worker 模板——进入登记"名字→本线程",退出 finally 注销并清旗
#   (线程复用安全:绝不把脏中断状态留给下一个任务)。
@contextmanager
def register_interruptible(name: str) -> Iterator[None]:
    tid = threading.current_thread().ident
    _register_named(name, tid)
    try:
        yield
    finally:
        _unregister_named(name, tid)


# LLM: Blocking transports register a short abort callback on the current interruptible thread; callbacks run outside the registry lock and must be idempotent.
# 函数用途: 让模型 HTTP 流等阻塞操作在收到同一任务的停止信号时主动关闭，而不是等完整超时。
@contextmanager
def register_interrupt_callback(callback: Callable[[], object]) -> Iterator[None]:
    tid = threading.current_thread().ident
    if tid is None:
        yield
        return
    call_now = False
    with _lock:
        _interrupt_callbacks.setdefault(tid, []).append(callback)
        call_now = tid in _interrupted_threads
    if call_now:
        _run_interrupt_callbacks((callback,))
    try:
        yield
    finally:
        with _lock:
            callbacks = _interrupt_callbacks.get(tid)
            if callbacks is not None:
                try:
                    callbacks.remove(callback)
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


# 函数用途: 把"名字→线程"写进登记表(拿不到 ident 就什么都不做)。
def _register_named(name: str, tid: int | None) -> None:
    if tid is None:
        return
    with _lock:
        _named_threads.setdefault(name, set()).add(tid)


# LLM: Unregister the name, typed flag, and any leftover abort hooks together so pooled thread identifiers cannot inherit stale state.
# 函数用途: 注销登记，并清掉本线程的中断旗与连接关闭回调。
def _unregister_named(name: str, tid: int | None) -> None:
    if tid is None:
        return
    with _lock:
        tids = _named_threads.get(name)
        if tids is not None:
            tids.discard(tid)
            if not tids:
                _named_threads.pop(name, None)
        if not any(tid in registered for registered in _named_threads.values()):
            _interrupted_threads.discard(tid)
            _interrupt_callbacks.pop(tid, None)


__all__ = [
    "interrupt_by_name",
    "is_interruptible_registered",
    "is_interrupted",
    "register_interrupt_callback",
    "register_interruptible",
    "set_interrupt",
    "wait_interruptibly",
]
