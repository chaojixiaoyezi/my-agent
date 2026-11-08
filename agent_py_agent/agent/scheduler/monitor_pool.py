"""监控专用有界线程池：非阻塞准入，业务并发权仍沿原工作流策略。"""
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import Context, copy_context
from pathlib import Path
from threading import Condition, Lock

from ..workflows.concurrency_policy import dispatch

_SHARED_LOCK = Lock()
_SHARED_POOL = None


def shared_monitor_pool(*, max_workers, queue_limit):
    """进程拥有唯一监控池；owner 只借用，最新配置原位调整资源上限。"""
    global _SHARED_POOL
    if type(max_workers) is not int or max_workers < 1 or type(queue_limit) is not int or queue_limit < 0:
        raise ValueError('monitor pool limits must be bounded nonnegative integers')
    with _SHARED_LOCK:
        if _SHARED_POOL is None or not _SHARED_POOL._reconfigure(max_workers, queue_limit):
            _SHARED_POOL = Context().run(MonitorPool, max_workers=max_workers, queue_limit=queue_limit)
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
        self._condition = Condition(self._lock)
        self._shared_limits, self._running = (max_workers, queue_limit), 0

    def _reconfigure(self, max_workers, queue_limit):
        """缩容只限制后续准入与执行，已接受任务保留；扩容随提交惰性启动线程。"""
        with self._condition:
            if self._closed:
                return False
            self._shared_limits = (max_workers, queue_limit)
            self._capacity = max_workers + queue_limit
            # 标准执行器没有公开的调容入口；沿提交/关闭锁修改，旧线程另由执行门约束。
            with self._executor._shutdown_lock:
                self._executor._max_workers = max_workers
            self._retire_idle_workers()
            self._condition.notify_all()
            return True

    def _retire_idle_workers(self):
        """调用方持池锁；只在全部任务归还后回收超额线程，不关闭 owner 手中的池引用。"""
        if not self._closed and not self._pending and len(self._executor._threads) > self._shared_limits[0]:
            previous = self._executor
            self._executor = Context().run(ThreadPoolExecutor, max_workers=self._shared_limits[0],
                                           thread_name_prefix='monitor')
            # 最后一份占位可能由 worker 自己归还，不能在此等待并 join 自身。
            previous.shutdown(wait=False)

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
        with self._condition:
            self._condition.wait_for(lambda: self._running < self._shared_limits[0])
            self._running += 1
        try:
            with bind_run_params(run_params) if bind_run_params is not None else nullcontext():
                return dispatch(owner_home, 'workflow', 'monitor:' + monitor_id, run_id, 'skip_if_running', callback)
        finally:
            with self._condition:
                self._running -= 1
                self._condition.notify_all()

    # LLM: 所有成功、失败和取消Future都在回调中归还容量，不保留无界历史。
    # 函数用途: 释放一份监控池占位。
    def _release(self, key):
        with self._lock:
            self._pending.discard(key)
            self._retire_idle_workers()

    # LLM: 关闭仅作用于本池，等待行为由宿主显式选择。
    # 函数用途: 回收监控池线程。
    def shutdown(self, *, wait=True):
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait)
