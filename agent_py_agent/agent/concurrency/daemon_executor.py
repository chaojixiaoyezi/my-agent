"""Daemon-backed executor for durable, fenced work that may be abandoned.

The standard ``ThreadPoolExecutor`` registers non-daemon workers in CPython's
global exit hook.  Consequently, ``shutdown(wait=False)`` still cannot let the
interpreter exit while one worker is stuck in a provider or tool call.

The shared daemon pool keeps normal
executor behavior is preserved, but workers are daemon threads and are not
registered in the global join table.  It is only appropriate when the work has
an authoritative durable lease/attempt record so a later process can recover it
and a stale worker is fenced from committing.
"""

from __future__ import annotations

import heapq
import itertools
import sys
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _worker
from contextvars import Context, copy_context

# 共用调度线程用导入时绑定的单调钟（同 threading.Timer）：调用方或测试替换 time.monotonic 只改它自己的计时，拨不动全进程的到期堆。
from time import monotonic as _monotonic


class DurableDaemonThreadPoolExecutor(ThreadPoolExecutor):
    """Run durable, lease-fenced work without letting a wedged turn block exit."""

    # LLM: This mirrors CPython's worker creation except that recovery-safe
    # jobs use daemon workers and skip the interpreter-wide join registry.
    # 函数用途: 沿用 CPython 的线程池创建逻辑，只把可由持久租约恢复的任务放到
    # daemon 线程，并且不登记到解释器退出时的全局等待表，防止卡死调用拖住服务重启。
    def _adjust_thread_count(self) -> None:
        if self._idle_semaphore.acquire(timeout=0):
            return
        # CPython 3.13+ 的 _worker 只在取空队列时归还空闲令牌，被抢走工作的已唤醒 worker 无令牌地重新挂起，令牌少计
        # 真实空闲数，池子随时间涨到 max_workers；队列能给出准确空闲容量时，只有排队项多于空闲容量才扩容。
        needs_worker = getattr(self._work_queue, "needs_worker", None)
        if callable(needs_worker) and not needs_worker():
            return

        def weakref_callback(_, queue=self._work_queue) -> None:
            queue.put(None)

        thread_count = len(self._threads)
        if thread_count >= self._max_workers:
            return
        # LLM: CPython 3.14 replaced the initializer/initargs worker contract
        # with a prepared worker context. Feature-detect the executor surface
        # so the durable daemon pool keeps the stdlib lifecycle on both sides
        # of that boundary without registering workers in _threads_queues.
        # 中文说明：Python 3.14 改了线程池私有 worker 参数；按标准库实例能力选择
        # 对应参数，而不是写死版本号，兼容 3.11–3.14 且仍避免退出时等待卡死线程。
        create_worker_context = getattr(self, "_create_worker_context", None)
        if callable(create_worker_context):
            worker_args = (
                weakref.ref(self, weakref_callback),
                create_worker_context(),
                self._work_queue,
            )
        else:
            worker_args = (
                weakref.ref(self, weakref_callback),
                self._work_queue,
                self._initializer,
                self._initargs,
            )
        # 3.14t 默认继承触发扩容的请求上下文；显式空上下文避免跨 owner 复用和保留引用。
        # 3.12 没有 context 参数，继续沿用原来的空出生上下文。
        thread_kwargs = {}
        if hasattr(sys.flags, "thread_inherit_context"):
            thread_kwargs["context"] = Context()
        thread = threading.Thread(
            name=f"{self._thread_name_prefix or self}_{thread_count}",
            target=_worker,
            args=worker_args,
            daemon=True,
            **thread_kwargs,
        )
        thread.start()
        self._threads.add(thread)


# LLM: 3.14t 每条短命线程退出后，其 mimalloc 堆里仍被别的线程引用的页无法回收，按线程创建次数线性涨 RSS；短延时回调
#   统一挂在一条常驻守护线程上，不再一次回调一条 threading.Timer。回调须短小、不阻塞；在创建句柄时的 context 里执行
#   （同 3.14t Timer 的继承语义）；cancel 只丢引用，已经开始执行的回调不撤回（同 Timer）。
# 类用途: 一次延时回调的句柄，用法同 threading.Timer：先建句柄、再登记、可取消。
class DeferredCall:
    __slots__ = ("fn", "context")

    def __init__(self, fn) -> None:
        self.fn, self.context = fn, copy_context()

    def cancel(self) -> None:
        self.fn = self.context = None


# 类用途: 进程级共享延时调用器：一条懒启动、空 context 出生的守护线程按到期顺序执行 DeferredCall，单个回调异常不终止线程。
class SharedDeferredCalls:
    def __init__(self, name: str) -> None:
        self._name, self._cv, self._heap, self._seq, self._thread = name, threading.Condition(), [], itertools.count(), None

    def start(self, call: DeferredCall, delay: float) -> None:
        with self._cv:
            heapq.heappush(self._heap, (_monotonic() + max(0.0, float(delay)), next(self._seq), call))
            if self._thread is None or not self._thread.is_alive():
                kwargs = {"context": Context()} if hasattr(sys.flags, "thread_inherit_context") else {}
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True, **kwargs)
                self._thread.start()
            self._cv.notify()

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._heap or self._heap[0][0] > _monotonic():
                    self._cv.wait(self._heap[0][0] - _monotonic() if self._heap else None)
                call = heapq.heappop(self._heap)[2]
            fn, context = call.fn, call.context
            if fn is not None and context is not None:
                try:
                    context.run(fn)
                except Exception:
                    sys.excepthook(*sys.exc_info())


__all__ = ["DeferredCall", "DurableDaemonThreadPoolExecutor", "SharedDeferredCalls"]
