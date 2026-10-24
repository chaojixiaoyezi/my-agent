# LLM: 只限制当前进程工具工作项，不拥有授权或落账；可信owner根不能来自模型参数。
# 模块用途: 跨Agent共享有界FIFO worker，逐项复制Context并隔离SQLite连接寿命。
from __future__ import annotations

import os
import threading
import weakref
from collections import deque
from concurrent.futures import Future
from contextlib import nullcontext
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from pathlib import Path

from ..common.cancellation import ToolCancelled
from ..io.sqlite_scope import sqlite_connection_scope

_SCHEDULERS = weakref.WeakValueDictionary()
_SCHEDULER_LOCK = threading.Lock()
_WORKER_PERMIT = ContextVar("owner_tool_worker_permit", default=None)
_WORKER_SQLITE_REUSE = ContextVar("owner_tool_sqlite_reuse", default=False)
# 单进程调度锁同时保护owner队列和全局执行额度，不在持一把owner锁时再等待另一把锁。
_DISPATCH_CONDITION = threading.Condition()
_GLOBAL_ACTIVE = 0
_GLOBAL_CONTROL_ACTIVE = 0
_GLOBAL_LIMIT = 0
# 已接受的全局等待项需要强引用保活；队空立即摘除，字典保留跨owner唤醒顺序。
_GLOBAL_WAITERS = {}
_CONTROL_BURST = 2


# LLM: 每个实例在提交线程独立捕获上下文；取消结果由原调用方构造，不合成成功或跳落账。
# 类用途: 保存单个工具调用的执行与取消边界。
@dataclass
class ToolWorkItem:
    call_id: str
    invoke: object
    cancellation: object = None
    cancelled_result: object = None
    sqlite_reuse: bool = False
    priority: str = 'normal'
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


# LLM: 正常线程数不超过owner/global cap；额外控制槽最多2个，队列有界且不为全局等待创建线程。
# 类用途: 按FIFO分配普通owner许可并保留控制快车道，空闲worker退出，不保留永久池。
class OwnerToolScheduler:
    # LLM: owner额度须为正整数；全局绑定初始0，后由可信宿主解析器提供；仅初始化内存，不启动线程或读取owner配置。
    # 函数用途: 建立owner有界队列和普通/控制计数，等待宿主绑定整机额度。
    def __init__(self, root, limit):
        if type(limit) is not int or limit < 1:
            raise ValueError("owner工具额度必须是正整数")
        self.key = _owner_key(root)
        self.limit = limit
        self._condition = _DISPATCH_CONDITION
        self._queue = deque()
        self._active = 0
        self._control_active = 0
        self._configured_global = 0
        self._submitted = 0
        self._admitted = 0
        self._abandoned = set()

    # LLM: 同进程同owner共用实例并绑定宿主全局配置；忙时取较小正值，空闲采用新值，旧submit调用不能绕过绑定。
    # 函数用途: 从弱注册表取得规范owner调度器。
    @classmethod
    def for_owner(cls, root, limit, max_parallel_global=0):
        if type(limit) is not int or limit < 1:
            raise ValueError("owner工具额度必须是正整数")
        if type(max_parallel_global) is not int or max_parallel_global < 0:
            raise ValueError('global工具额度必须为非负整数')
        key = _owner_key(root)
        with _SCHEDULER_LOCK:
            scheduler = _SCHEDULERS.get(key)
            if scheduler is None:
                scheduler = cls(root, limit)
                _SCHEDULERS[key] = scheduler
            with scheduler._condition:
                busy = scheduler._active or scheduler._queue
                scheduler.limit = min(scheduler.limit, limit) if busy else limit
                limits = [value for value in (scheduler._configured_global, max_parallel_global) if value]
                scheduler._configured_global = min(limits, default=0) if busy else max_parallel_global
            return scheduler

    # LLM: 同owner同线程重入内联；背压接收取消唤醒，异常退出废弃票号；Future回调始终在队列锁外完成。
    # 函数用途: 公平提交一个工作项，队满时阻塞生产者而不增worker。
    def submit(self, item, cancellation_token: object | None = None, max_parallel_global: int = 0, priority: str = 'normal'):
        if type(max_parallel_global) is not int or max_parallel_global < 0 or priority not in {'normal', 'control'}:
            raise ValueError('invalid global tool limit or priority')
        _raise_queue_cancelled(cancellation_token)
        future = Future()
        if current_owner_scheduler() is self:
            self._complete(item, future)
            return future
        future.add_done_callback(self._discard_cancelled)
        register = getattr(cancellation_token, 'register_callback', None)
        wakeup = register(self._notify_waiters) if callable(register) else nullcontext()
        with wakeup, self._condition:
            effective = min((value for value in (max_parallel_global, self._configured_global) if value), default=0)
            _configure_global_limit(effective)
            self._admit(item, future, cancellation_token, priority)
            failure = self._start_worker()
            self._condition.notify_all()
        _finish_start_failure(failure)
        return future

    # LLM: 控制队列有独立2项容量，不消耗普通FIFO票号；正常背压中异常退出必须废弃票号。
    # 函数用途: 在同一调度锁内按优先级将工作项放入有界物理队列。
    def _admit(self, item, future, token, priority):
        if priority == 'control':
            while sum(work[2] == 'control' for work in self._queue) >= _CONTROL_BURST:
                _raise_queue_cancelled(token)
                self._condition.wait(0.05 if token is not None else None)
            _raise_queue_cancelled(token)
            self._queue.appendleft((item, future, priority))
        else:
            ticket = self._submitted
            self._submitted += 1
            try:
                self._wait_admission(ticket, token)
            except BaseException:
                self._abandoned.add(ticket)
                self._skip_abandoned()
                self._condition.notify_all()
                raise
            self._queue.append((item, future, priority))
            self._admitted += 1
            self._skip_abandoned()

    # LLM: 本次取消回调只通知条件，不操作Future或调用handler；注册范围退出即撤销。
    # 函数用途: 唤醒等待入队的生产者。
    def _notify_waiters(self):
        with self._condition:
            self._condition.notify_all()

    # LLM: 无token沿原wait_for合同；有token短片兜住外部检查式取消，显式cancel回调立即唤醒。
    # 函数用途: 等待FIFO票号与物理队列容量，并在取消时退出。
    def _wait_admission(self, ticket, token):
        # LLM: 只查询锁内FIFO位置和物理容量，不调用外部执行器。
        # 函数用途: 判断当前票号是否可以入队。
        def ready():
            return ticket == self._admitted and len(self._queue) < 2 * self.limit
        if token is None:
            self._condition.wait_for(ready)
            return
        while not ready():
            _raise_queue_cancelled(token)
            self._condition.wait_for(lambda: ready() or _queue_cancelled(token), timeout=0.05)
        _raise_queue_cancelled(token)

    # LLM: deque长度是唯一容量权威；取消只摘匹配Future一次，不另设易漂移pending计数。
    # 函数用途: 对直接Future.cancel物理移除尚未领取的死项并释放容量。
    def _discard_cancelled(self, future):
        if not future.cancelled():
            return
        with self._condition:
            work = next((work for work in self._queue if work[1] is future), None)
            if work is not None:
                self._queue.remove(work)
                if not self._queue:
                    _GLOBAL_WAITERS.pop(self, None)
                self._condition.notify_all()

    # LLM: 调用方持condition；票号只单调前进，不回退已发出的后继序号。
    # 函数用途: 越过连续废弃票号，恢复FIFO提交。
    def _skip_abandoned(self):
        while self._admitted in self._abandoned:
            self._abandoned.remove(self._admitted)
            self._admitted += 1

    # LLM: 调用方持共享锁；正常与保留槽分别计数，启动失败归还两层许可，Future回调交锁外完成。
    # 函数用途: 为队列准备一个受额度约束的worker。
    def _start_worker(self):
        global _GLOBAL_ACTIVE, _GLOBAL_CONTROL_ACTIVE
        if not self._queue:
            return
        normal_active = _GLOBAL_ACTIVE - _GLOBAL_CONTROL_ACTIVE
        reserve = self._queue[0][2] == 'control' and (self._active - self._control_active >= self.limit or
                                                    bool(_GLOBAL_LIMIT and normal_active >= _GLOBAL_LIMIT))
        owner_full = self._control_active >= _CONTROL_BURST if reserve else self._active - self._control_active >= self.limit
        global_full = bool(_GLOBAL_LIMIT and (_GLOBAL_CONTROL_ACTIVE >= _CONTROL_BURST if reserve
                                             else normal_active >= _GLOBAL_LIMIT))
        if owner_full or global_full:
            _GLOBAL_WAITERS.setdefault(self, None)
            return
        _GLOBAL_WAITERS.pop(self, None)
        self._active += 1
        self._control_active += int(reserve)
        _GLOBAL_ACTIVE += 1
        _GLOBAL_CONTROL_ACTIVE += int(reserve)
        try:
            threading.Thread(target=self._drain, args=(reserve,), name="my-agent-owner-tool", daemon=True).start()
        except BaseException as error:
            self._active -= 1
            self._control_active -= int(reserve)
            _GLOBAL_ACTIVE -= 1
            _GLOBAL_CONTROL_ACTIVE -= int(reserve)
            pending = tuple(work[:2] for work in self._queue)
            self._queue.clear()
            return pending, error

    # LLM: 每项后恢复Context；全局上限下若有其它owner排队则让出槽位轮转；降额度时超额worker退出。
    # 函数用途: 消费FIFO队列，队空或需跨owner公平让槽时退出并唤醒等待者。
    def _drain(self, reserve=False):
        global _GLOBAL_ACTIVE, _GLOBAL_CONTROL_ACTIVE
        yield_to_waiters = False
        try:
            while True:
                work = self._take_next(reserve, yield_to_waiters=yield_to_waiters)
                if work is None:
                    return
                yield_to_waiters = True
                self._complete(*work)
        finally:
            with self._condition:
                self._active -= 1
                self._control_active -= int(reserve)
                _GLOBAL_ACTIVE -= 1
                _GLOBAL_CONTROL_ACTIVE -= int(reserve)
                if self._queue:
                    _GLOBAL_WAITERS.setdefault(self, None)
                failures = _wake_global_waiters()
                self._condition.notify_all()
            for failure in failures:
                _finish_start_failure(failure)

    # LLM: 保留worker只取控制项，正常许可不扣预留控制槽；已完成一项且有其它owner等待全局槽时主动让出。
    # 函数用途: 领取下一工作项并解除生产者背压。
    def _take_next(self, reserve=False, *, yield_to_waiters=False):
        with self._condition:
            if (not self._queue or (reserve and self._queue[0][2] != 'control')
                    or (not reserve and (self._active - self._control_active > self.limit
                                        or bool(_GLOBAL_LIMIT and _GLOBAL_ACTIVE - _GLOBAL_CONTROL_ACTIVE > _GLOBAL_LIMIT)
                                        or (yield_to_waiters and bool(_GLOBAL_LIMIT)
                                            and any(waiter is not self and waiter._queue for waiter in _GLOBAL_WAITERS))))):
                return None
            work = self._queue.popleft()
            self._condition.notify_all()
            return work[:2]

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


# LLM: 调用方持唯一调度锁；活跃代次内冲突配置取更小正值，0不会拆掉其它owner正在使用的正上限。
# 函数用途: 更新单进程共享工具额度，空闲时允许采用新配置。
def _configure_global_limit(limit):
    global _GLOBAL_LIMIT
    if not _GLOBAL_ACTIVE and not _GLOBAL_WAITERS:
        _GLOBAL_LIMIT = limit
    elif limit:
        _GLOBAL_LIMIT = min(_GLOBAL_LIMIT, limit) if _GLOBAL_LIMIT else limit


# LLM: 调用方持统一调度锁，不反向获取注册表锁；仅为已有待执行队列分配释放的槽，失败回调在锁外完成。
# 函数用途: 让全局额度释放后其它owner的队列继续前进。
def _wake_global_waiters():
    failures = []
    for scheduler in tuple(_GLOBAL_WAITERS):
        if scheduler._queue:
            failure = scheduler._start_worker()
            if failure is not None:
                failures.append(failure)
        else:
            _GLOBAL_WAITERS.pop(scheduler, None)
    return failures


# LLM: 仅查询宿主取消协议，不以Future状态冒充业务取消；兼容只提供状态属性的令牌。
# 函数用途: 判断排队提交是否收到取消信号。
def _queue_cancelled(token):
    state = getattr(token, 'cancelled', getattr(token, 'is_cancelled', False))
    return bool(state() if callable(state) else state)


# LLM: 使用原ToolCancelled类型，不能把取消写成普通成功或改变原handler结果。
# 函数用途: 在入队前和背压等待中拒绝取消提交。
def _raise_queue_cancelled(token):
    if token is not None and _queue_cancelled(token):
        raise ToolCancelled('OWNER_TOOL_QUEUE_CANCELLED')


# LLM: 只读宿主主配置/能力配置；双0保留旧线程池，global正值在owner不限时也必须启用，任务不能调大额度。
# 函数用途: 为工具轮和嵌套调用选择并绑定同一个owner/global调度器。
def scheduler_for_agent(agent):
    limit = getattr(getattr(agent, "config", None), "max_parallel_tool_calls_per_owner", 0)
    global_limit = resolve_max_parallel_tool_calls_global(agent)
    limit = limit or global_limit
    if not limit:
        return None
    root = getattr(getattr(agent, "home_paths", None), "owner_home_dir", None) or getattr(agent, "root", None)
    if root is None:
        raise ValueError("owner工具额度缺少宿主根")
    return OwnerToolScheduler.for_owner(root, limit, max_parallel_global=global_limit)


# LLM: 宿主显式非None注入优先；仅main沿原owner读口，非main只读无参宿主默认，不信私有快照或任务参数；0不限。
# 函数用途: 按可信身份选择整机工具安全阀来源，并严格校验非负整数。
def resolve_max_parallel_tool_calls_global(agent):
    from ..capability.config import load_capability_config
    from ..capability.runtime_config_reload import capability_config_for_agent
    config = getattr(agent, 'config', None)
    value = getattr(config, 'max_parallel_tool_calls_global', None)
    if value is None:
        owner_kind = getattr(getattr(agent, 'home_paths', None), 'owner_kind', None)
        if owner_kind in ('', None):
            owner_kind = getattr(config, 'owner_kind', None)
        if owner_kind in ('', None):
            owner_kind = getattr(config, 'my_agent_owner_kind', 'main')
        capability = capability_config_for_agent(agent) if owner_kind in ('', 'main', None) else load_capability_config()
        value = getattr(capability, 'max_parallel_tool_calls_global', 0)
    if type(value) is not int or value < 0:
        raise ValueError('max_parallel_tool_calls_global must be a nonnegative integer')
    return value


# LLM: 入队取消仍由原调用方构造逐项取消结果，不丢provider配对；无取消工厂时保留原异常。
# 函数用途: 在真实轮次与原调度器之间传递宿主令牌、全局额度和优先级。
def submit_owner_tool_call(scheduler, item, *, cancellation_token: object | None = None,
                           max_parallel_global: int = 0, priority: str = 'normal'):
    try:
        return scheduler.submit(item, cancellation_token=cancellation_token,
                                max_parallel_global=max_parallel_global, priority=priority)
    except ToolCancelled:
        if item.cancelled_result is None:
            raise
        future = Future()
        future.set_result(item.cancelled_result())
        return future


# LLM: 只在已获准独立批次内先提交控制项，结果仍按原索引；部分失败等本批在途项后传播，不等其它轮。
# 函数用途: 避免普通背压阻挡同批控制调用，并按provider顺序收集结果。
def execute_work_items(items, submit):
    futures = {}
    ordered = sorted(enumerate(items), key=lambda pair: getattr(pair[1], 'priority', 'normal') != 'control')
    try:
        for index, item in ordered:
            futures[index] = submit(item)
    except BaseException:
        _settle_futures([future for _, future in sorted(futures.items())], ignore_errors=True)
        raise
    return _settle_futures([future for _, future in sorted(futures.items())])


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
