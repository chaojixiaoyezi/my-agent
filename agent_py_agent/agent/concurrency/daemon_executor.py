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

import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _worker


class DurableDaemonThreadPoolExecutor(ThreadPoolExecutor):
    """Run durable, lease-fenced work without letting a wedged turn block exit."""

    # LLM: This mirrors CPython's worker creation except that recovery-safe
    # jobs use daemon workers and skip the interpreter-wide join registry.
    # 中文说明：这里沿用 CPython 的线程池创建逻辑，只把可由持久租约恢复的任务放到
    # daemon 线程，并且不登记到解释器退出时的全局等待表，防止卡死调用拖住服务重启。
    def _adjust_thread_count(self) -> None:
        if self._idle_semaphore.acquire(timeout=0):
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
        thread = threading.Thread(
            name=f"{self._thread_name_prefix or self}_{thread_count}",
            target=_worker,
            args=worker_args,
            daemon=True,
        )
        thread.start()
        self._threads.add(thread)


__all__ = ["DurableDaemonThreadPoolExecutor"]
