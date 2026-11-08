"""监控专用有界线程池：非阻塞准入，业务并发权仍沿原工作流策略。"""
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import Context, copy_context
from pathlib import Path
from threading import Lock

from ..workflows.concurrency_policy import dispatch

_SHARED_LOCK = Lock()
_SHARED_POOL = None


def shared_monitor_pool(*, max_workers, queue_limit):
    """进程拥有唯一监控池；owner 只借用，部署级容量冲突必须显式拒绝。"""
    global _SHARED_POOL
    if type(max_workers) is not int or max_workers < 1 or type(queue_limit) is not int or queue_limit < 0:
        raise ValueError('monitor pool limits must be bounded nonnegative integers')
    limits = (max_workers, queue_limit)
    with _SHARED_LOCK:
        if _SHARED_POOL is None or _SHARED_POOL._closed:
            _SHARED_POOL = MonitorPool(max_workers=max_workers, queue_limit=queue_limit)
            _SHARED_POOL._shared_limits = limits
        elif _SHARED_POOL._shared_limits != limits:
            raise ValueError('shared monitor pool configuration conflict')
        return _SHARED_POOL


# LLM: 容量只控制本进程资源，不替代原宿主权限、取消及耐久run。
# 类用途: 接收监控任务，满载或重入时返回结构化跳过。
class MonitorPool:
    # LLM: 默认四个worker及有限排队，不在构造时执行任何业务。
    # 函数用途: 创建一个惰性启动的监控池。
    def __init__(self, *, max_workers=4, queue_limit=8):
        if type(max_workers) is not int or max_workers < 1 or type(queue_limit) is not int or queue_limit < 0:
            raise ValueError('monitor pool limits must be bounded nonnegative integers')
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='monitor')
        self._capacity = max_workers + queue_limit
        self._lock, self._pending, self._closed = Lock(), set(), False

    # LLM: 提交不等待容量，worker必须显式绑定宿主run参数。
    # 函数用途: 返回一次监控执行的Future或已完成的跳过Future。
    def submit(self, owner_home, monitor_id, run_id, callback, *, run_params=None, bind_run_params=None):
        key = (str(Path(owner_home).absolute()), monitor_id)
        with self._lock:
            reason = ('closed' if self._closed else 'already_running' if key in self._pending
                      else 'queue_full' if len(self._pending) >= self._capacity else '')
            if reason:
                skipped = Future()
                skipped.set_result({'status': 'skipped', 'error_code': 'WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING', 'reason': reason})
                return skipped
            self._pending.add(key)
        context = copy_context()
        try:
            future = Context().run(self._executor.submit, context.run, self._execute, owner_home, monitor_id, run_id,
                                   callback, run_params, bind_run_params)
        except BaseException:
            with self._lock:
                self._pending.discard(key)
            raise
        future.add_done_callback(lambda completed: self._release(key))
        return future

    # LLM: worker显式绑定thread-local；并发claim复用原skip_if_running策略，不接管业务终态。
    # 函数用途: 在原执行权范围内调用一个监控业务。
    def _execute(self, owner_home, monitor_id, run_id, callback, run_params, bind_run_params):
        with bind_run_params(run_params) if bind_run_params is not None else nullcontext():
            return dispatch(owner_home, 'workflow', 'monitor:' + monitor_id, run_id, 'skip_if_running', callback)

    # LLM: 所有成功、失败和取消Future都在回调中归还容量，不保留无界历史。
    # 函数用途: 释放一份监控池占位。
    def _release(self, key):
        with self._lock:
            self._pending.discard(key)

    # LLM: 关闭仅作用于本池，等待行为由宿主显式选择。
    # 函数用途: 回收监控池线程。
    def shutdown(self, *, wait=True):
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait)
