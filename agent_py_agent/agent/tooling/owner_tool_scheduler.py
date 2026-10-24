# LLM: 只限制当前进程工具工作项，不拥有授权或落账；可信owner根不能来自模型参数。
# 模块用途: 跨Agent共享有界FIFO worker，逐项复制Context并隔离SQLite连接寿命。
from __future__ import annotations

import os
import threading
import weakref
from collections import deque
from concurrent.futures import Future
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from pathlib import Path

from ..common.cancellation import ToolCancelled
from ..io.sqlite_scope import sqlite_connection_scope

_SCHEDULERS = weakref.WeakValueDictionary()
_SCHEDULER_LOCK = threading.Lock()
_WORKER_PERMIT = ContextVar("owner_tool_worker_permit", default=None)
_WORKER_SQLITE_REUSE = ContextVar("owner_tool_sqlite_reuse", default=False)


# LLM: 每个实例在提交线程独立捕获上下文；取消结果由原调用方构造，不合成成功或跳落账。
# 类用途: 保存单个工具调用的执行与取消边界。
@dataclass
class ToolWorkItem:
    call_id: str
    invoke: object
    cancellation: object = None
    cancelled_result: object = None
    sqlite_reuse: bool = False
    captured_context: object = field(default_factory=copy_context, init=False, repr=False)

    # LLM: SQLite scope先替换继承Context中的旧scope，绝不操作父线程连接；退出恢复且关闭本项连接。
    # 函数用途: 在实际worker内检查取消并执行一次调用。
    def run(self):
        token = _WORKER_SQLITE_REUSE.set(self.sqlite_reuse)
        try:
            with sqlite_connection_scope(self.sqlite_reuse):
                return self._run_unless_cancelled()
        finally:
            _WORKER_SQLITE_REUSE.reset(token)

    # LLM: 不以Future运行态替代业务取消；取消结果仍归原执行轮配对。
    # 函数用途: 在handler之前执行最后的取消检查。
    def _run_unless_cancelled(self):
        if self.cancellation is not None and self.cancellation.cancelled:
            if self.cancelled_result is not None:
                return self.cancelled_result()
            raise ToolCancelled("工具排队期间已取消")
        return self.invoke()


# LLM: 只传播配置布尔值，不读或返回任何父scope/连接引用。
# 函数用途: 为嵌套子工作项选择同轮SQLite开关。
def current_worker_sqlite_reuse():
    return _WORKER_SQLITE_REUSE.get()


# LLM: key含PID及规范根，复制Context不转移线程许可；仅持有许可的原线程可内联嵌套。
# 函数用途: 查询当前线程真正持有的调度器，不从线程名推断。
def current_owner_scheduler(root=None):
    permit = _WORKER_PERMIT.get()
    if permit is None:
        return None
    scheduler, thread = permit
    if scheduler.key[0] != os.getpid() or thread is not threading.current_thread():
        return None
    if root is not None and scheduler.key != _owner_key(root):
        return None
    return scheduler


# LLM: 根只接宿主已绑定路径，resolve仅做身份规范化，不创建目录、不赋予路径访问权限。
# 函数用途: 计算进程内owner额度键。
def _owner_key(root):
    return os.getpid(), str(Path(root).resolve())


# LLM: 线程数不超过cap，待执行队列最多2*cap；生产者在提交处背压，不为排队创建工具线程。
# 类用途: 按FIFO分配同owner许可，空闲worker立即退出，注册表仅弱引用不保留永久池。
class OwnerToolScheduler:
    # LLM: 构造只准备内存，不启动线程；limit只能为正整数。
    # 函数用途: 建立一个owner的有界队列。
    def __init__(self, root, limit):
        if type(limit) is not int or limit < 1:
            raise ValueError("owner工具额度必须是正整数")
        self.key = _owner_key(root)
        self.limit = limit
        self._condition = threading.Condition()
        self._queue = deque()
        self._active = 0
        self._submitted = 0
        self._admitted = 0
        self._abandoned = set()

    # LLM: 同进程同owner共用实例；并发宿主配置不一致时取较小值，空闲后采用最新值。
    # 函数用途: 从弱注册表取得规范owner调度器。
    @classmethod
    def for_owner(cls, root, limit):
        if type(limit) is not int or limit < 1:
            raise ValueError("owner工具额度必须是正整数")
        key = _owner_key(root)
        with _SCHEDULER_LOCK:
            scheduler = _SCHEDULERS.get(key)
            if scheduler is None:
                scheduler = cls(root, limit)
                _SCHEDULERS[key] = scheduler
            else:
                with scheduler._condition:
                    busy = scheduler._active or scheduler._queue
                    scheduler.limit = min(scheduler.limit, limit) if busy else limit
            return scheduler

    # LLM: 同owner同线程重入内联；背压中异常退出必须废弃票号，后继不得永远等缺口。
    # 函数用途: 公平提交一个工作项，队满时阻塞生产者而不增worker。
    def submit(self, item):
        future = Future()
        if current_owner_scheduler() is self:
            self._complete(item, future)
            return future
        with self._condition:
            ticket = self._submitted
            self._submitted += 1
            try:
                self._condition.wait_for(lambda: ticket == self._admitted and len(self._queue) < 2 * self.limit)
            except BaseException:
                self._abandoned.add(ticket)
                self._skip_abandoned()
                self._condition.notify_all()
                raise
            self._queue.append((item, future))
            self._admitted += 1
            self._skip_abandoned()
            failure = self._start_worker()
            self._condition.notify_all()
        _finish_start_failure(failure)
        return future

    # LLM: 调用方持condition；票号只单调前进，不回退已发出的后继序号。
    # 函数用途: 越过连续废弃票号，恢复FIFO提交。
    def _skip_abandoned(self):
        while self._admitted in self._abandoned:
            self._abandoned.remove(self._admitted)
            self._admitted += 1

    # LLM: 调用方持队列锁；启动失败先摘出积压，Future回调交锁外完成，不留下无人消费的项。
    # 函数用途: 为队列准备一个受额度约束的worker。
    def _start_worker(self):
        if self._active >= self.limit:
            return
        self._active += 1
        try:
            threading.Thread(target=self._drain, name="my-agent-owner-tool", daemon=True).start()
        except BaseException as error:
            self._active -= 1
            pending = tuple(self._queue)
            self._queue.clear()
            return pending, error

    # LLM: 每项后恢复Context；降额度时超额worker退出而不领取新项，finally归还线程额度。
    # 函数用途: 消费FIFO队列，队空即退出而不保留空闲线程。
    def _drain(self):
        try:
            while True:
                work = self._take_next()
                if work is None:
                    return
                self._complete(*work)
        finally:
            with self._condition:
                self._active -= 1
                failure = self._start_worker() if self._queue else None
                self._condition.notify_all()
            _finish_start_failure(failure)

    # LLM: 条件锁只覆盖FIFO摘取；降额度时超额线程必须退场，不再领新调用。
    # 函数用途: 领取下一工作项并解除生产者背压。
    def _take_next(self):
        with self._condition:
            if not self._queue or self._active > self.limit:
                return None
            work = self._queue.popleft()
            self._condition.notify_all()
            return work

    # LLM: Future取消不调用handler；结果/异常原样交给等待方，回调不在队列锁内执行。
    # 函数用途: 在独立Context内结束一个工作项。
    def _complete(self, item, future):
        if not future.set_running_or_notify_cancel():
            return
        try:
            value = item.captured_context.run(self._invoke, item)
        except BaseException as error:
            future.set_exception(error)
        else:
            future.set_result(value)

    # LLM: 许可与原线程对象一起绑定；copy_context到别的线程不能借用此许可。
    # 函数用途: 在工具执行期间临时安装owner worker许可。
    def _invoke(self, item):
        token = _WORKER_PERMIT.set((self, threading.current_thread()))
        try:
            return item.run()
        finally:
            _WORKER_PERMIT.reset(token)


# LLM: 只读agent宿主home与配置，不消费task_attributes，任务不能调大进程额度；默认0不改变旧线程池。
# 函数用途: 为一轮工具选择可选owner调度器。
def scheduler_for_agent(agent):
    limit = getattr(getattr(agent, "config", None), "max_parallel_tool_calls_per_owner", 0)
    if not limit:
        return None
    root = getattr(getattr(agent, "home_paths", None), "owner_home_dir", None) or getattr(agent, "root", None)
    if root is None:
        raise ValueError("owner工具额度缺少宿主根")
    return OwnerToolScheduler.for_owner(root, limit)


# LLM: 只等待本批已提交项，不等其它owner轮；部分提交失败仍清理已在途调用后原样传播。
# 函数用途: 沿原线程池语义提交一批并顺序收集结果。
def execute_work_items(items, submit):
    futures = []
    try:
        for item in items:
            futures.append(submit(item))
    except BaseException:
        _settle_futures(futures, ignore_errors=True)
        raise
    return _settle_futures(futures)


# LLM: 必须在队列锁外执行Future回调；排队已取消项不重复完成，系统线程启动异常不得伪装成功。
# 函数用途: 将worker启动失败原样交给所有被摘出的等待者。
def _finish_start_failure(failure):
    if failure is None:
        return
    pending, error = failure
    for _item, future in pending:
        if future.set_running_or_notify_cancel():
            future.set_exception(error)


# LLM: 捕获异常仅为等待剩余本批，不把失败转成功；保留原顺序第一个异常及traceback。
# 函数用途: 等全部在途工作结束后返回结果或传播错误。
def _settle_futures(futures, *, ignore_errors=False):
    results, first_error = [], None
    for future in futures:
        try:
            results.append(future.result())
        except BaseException as error:
            if first_error is None:
                first_error = error, error.__traceback__
    if first_error is not None and not ignore_errors:
        raise first_error[0].with_traceback(first_error[1])
    return results
