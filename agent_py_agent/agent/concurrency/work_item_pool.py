"""模型调用与工具等待的复用线程池：复用线程，但从不排队（C1.7）。

每次模型调用/工具等待都新建线程时，python3.14t 的 mimalloc 按线程保留堆，线程反复生灭让常驻内存持续上涨
（R3 §2.5、§4.9）。WorkItemPool 建在 DurableDaemonThreadPoolExecutor + CancellableWorkQueue 之上：
有空闲线程就交给它（最近空闲者优先），没有就扩容到 max_threads，再超出就起一次性守护线程运行——
提交永远不会排在别的工作项后面，也不设任何准入上限。

句柄状态只在句柄锁内按比较交换推进：PREPARED → RELEASED → RUNNING → DONE，或 PREPARED|RELEASED → CANCELLED。
prepare 只建句柄（任何线程都还拿不到它），start 才交给线程；执行者在跑 on_start/fn 之前做 RELEASED → RUNNING，
失败（已取消，或同一项的另一执行者已认领）就只做卫生步骤。interrupt 只在 RUNNING 时给执行线程立旗，开始前等同
cancel_if_not_started，结束后无操作，所以迟到的中断不会落到同一线程的下一项上。队列移除只是槽位卫生，
“未开始”的证明只来自 CANCELLED 状态。每个工作项前后都清当前线程中断旗、运行登记的线程局部复位；fn 在调用方
给的 contextvars.Context 里运行（等同原先 Thread(context=…) 的效果）。
"""

from __future__ import annotations

import contextvars
import sys
import threading
import time
from collections.abc import Callable

from .daemon_executor import DurableDaemonThreadPoolExecutor
from .interrupt import set_interrupt
from .lifo_work_queue import CancellableWorkQueue

# 每个池的常驻线程硬上限（R3 §4.9）：只限定池内复用线程数，超出时改用一次性守护线程运行，从不限制准入。
WORK_ITEM_POOL_MAX_THREADS_COUNT = 512

_PREPARED = "prepared"
_RELEASED = "released"
_RUNNING = "running"
_DONE = "done"
_CANCELLED = "cancelled"

_RESETS_LOCK = threading.Lock()
# 登记的线程局部复位函数（写时整体替换元组，读时无锁）。
_ITEM_LOCAL_RESETS: tuple[Callable[[], None], ...] = ()


# LLM: 与线程目标函数抛错时的处理一致：交给 threading.excepthook（运行时查找，便于测试替换），钩子本身失败再交
#   sys.excepthook；SystemExit 由 threading.excepthook 按标准规则静默。
# 函数用途: 报告工作项（或复位、on_start）抛出的异常，不让它终止复用线程。
def _report_exception() -> None:
    exc_type, exc_value, exc_traceback = sys.exc_info()
    try:
        threading.excepthook(threading.ExceptHookArgs([exc_type, exc_value, exc_traceback,
                                                        threading.current_thread()]))
    except Exception:  # noqa: BLE001 - 钩子失败退回解释器默认钩子
        sys.excepthook(*sys.exc_info())
    finally:
        del exc_type, exc_value, exc_traceback


# 函数用途: 依次运行全部登记的线程局部复位；单个复位失败只报告，不影响其余复位与线程存活。
def _run_item_local_resets() -> None:
    for reset in _ITEM_LOCAL_RESETS:
        try:
            reset()
        except BaseException:  # noqa: BLE001 - 复位失败只报告
            _report_exception()


# LLM: 同一函数重复登记只保留一份；登记对本进程所有池生效（复位针对线程局部状态，与池无关）。
# 函数用途: 登记一个在每个工作项前后运行的线程局部复位（如 agent._current_run_params）。
def register_item_local_reset(fn: Callable[[], None]) -> None:
    global _ITEM_LOCAL_RESETS
    with _RESETS_LOCK:
        if fn not in _ITEM_LOCAL_RESETS:
            _ITEM_LOCAL_RESETS = (*_ITEM_LOCAL_RESETS, fn)


# 函数用途: 在工作项 context 内先运行 on_start（失败只报告），再运行 fn（异常交 threading.excepthook）。
def _invoke(fn: Callable[[], object], on_start: Callable[[], None] | None) -> None:
    if on_start is not None:
        try:
            on_start()
        except BaseException:  # noqa: BLE001 - on_start 只是阶段记录，失败不阻止工作项
            _report_exception()
    try:
        fn()
    except BaseException:  # noqa: BLE001 - 与线程目标函数相同的报告方式
        _report_exception()


# LLM: 池线程与一次性溢出线程都执行这一函数。RELEASED → RUNNING 在句柄锁内比较交换，谁先认领谁运行；已取消或已被
#   另一执行者认领时只做前置卫生就返回，所以同一项不会运行两次。结束时先在句柄锁内转 DONE（此后 interrupt 不再
#   立旗）并清 ident，再清旗与复位，最后才置结算事件：done()/join 返回时线程状态已经干净。
# 函数用途: 运行一个已交付的工作项并完成前后卫生步骤。
def _run_item(handle: WorkItemHandle) -> None:
    set_interrupt(False)
    _run_item_local_resets()
    with handle._lock:
        if handle._state != _RELEASED:
            return
        handle._state = _RUNNING
        handle.ident = threading.get_ident()
        fn, context, on_start = handle._fn, handle._context, handle._on_start
        handle._fn = handle._context = handle._on_start = None
    try:
        context.run(_invoke, fn, on_start)
    finally:
        with handle._lock:
            handle._state = _DONE
            handle.ident = None
        del fn, context, on_start
        set_interrupt(False)
        _run_item_local_resets()
        handle._settled.set()


# 类用途: 一个工作项的句柄：交付、查询运行线程 ident、精确中断、开始前取消、等待完成。
class WorkItemHandle:
    __slots__ = ("ident", "_state", "_lock", "_settled", "_pool", "_fn", "_context", "_on_start")

    def __init__(self, pool: WorkItemPool, fn: Callable[[], object], context: contextvars.Context,
                 on_start: Callable[[], None] | None) -> None:
        # 字段用途: 本项 RUNNING 期间为执行线程的 ident，其余状态为 None（线程复用后不会指向别的工作项）。
        self.ident: int | None = None
        self._state = _PREPARED
        self._lock = threading.Lock()
        # 结算事件：DONE 且卫生步骤完成，或 CANCELLED 时置位。
        self._settled = threading.Event()
        self._pool = pool
        self._fn: Callable[[], object] | None = fn
        self._context: contextvars.Context | None = context
        self._on_start: Callable[[], None] | None = on_start

    # LLM: PREPARED → RELEASED 后交给池；已被取消的预备项不交付（与执行者认领失败时一样静默跳过），重复 start 与
    #   Thread.start 一样抛 RuntimeError。线程起不来且本项仍未被认领时，本项转 CANCELLED（已证明未开始）并抛出。
    # 函数用途: 把预备好的工作项交给空闲线程、新扩容线程或一次性溢出线程。
    def start(self) -> None:
        with self._lock:
            if self._state == _CANCELLED:
                return
            if self._state != _PREPARED:
                raise RuntimeError("work items can only be started once")
            self._state = _RELEASED
        self._pool._release(self)

    # 函数用途: 调用方持句柄锁、状态为 PREPARED 或 RELEASED 时转 CANCELLED、释放工作引用并结算；返回此前是否已交付。
    def _cancel_locked(self) -> bool:
        released = self._state == _RELEASED
        self._state = _CANCELLED
        self._fn = self._context = self._on_start = None
        self._settled.set()
        return released

    # LLM: 三种情形在同一次句柄锁内判定：RUNNING 时给执行线程立中断旗（结束步骤也要取这把锁，旗只会落在本项运行
    #   期间，结束后的清旗再把它撤掉）；开始前等同 cancel_if_not_started；DONE/CANCELLED 时什么也不做。
    # 函数用途: 中断本工作项。
    def interrupt(self) -> None:
        with self._lock:
            if self._state == _RUNNING:
                set_interrupt(True, self.ident)
                return
            if self._state != _PREPARED and self._state != _RELEASED:
                return
            released = self._cancel_locked()
        if released:
            self._pool._queue.discard(self)

    # 函数用途: 本次调用把句柄从 PREPARED/RELEASED 转为 CANCELLED 时返回 True（随后只做队列槽位卫生）；否则返回 False。
    def cancel_if_not_started(self) -> bool:
        with self._lock:
            if self._state != _PREPARED and self._state != _RELEASED:
                return False
            released = self._cancel_locked()
        if released:
            self._pool._queue.discard(self)
        return True

    # 函数用途: 只有 CANCELLED（含交付失败后的取消）才证明 fn 从未开始。
    def proven_not_started(self) -> bool:
        return self._state == _CANCELLED

    # 函数用途: fn 已返回或抛出且本项卫生步骤已完成时为 True；池线程之后复用也不改变它。
    def done(self) -> bool:
        return self._state == _DONE and self._settled.is_set()

    # 函数用途: 与 Thread.is_alive 相同的含义：已交付且尚未结算时为 True。
    def is_alive(self) -> bool:
        return self._state != _PREPARED and not self._settled.is_set()

    # LLM: 等 done()，CANCELLED 时立即返回；与 Thread.join 一样禁止在本项自身运行期间等待自己（必然死锁）。
    # 函数用途: 等待工作项结算，返回是否已结算（超时返回 False）。
    def join(self, timeout: float | None = None) -> bool:
        if self._state == _RUNNING and self.ident == threading.get_ident():
            raise RuntimeError("cannot join the current work item")
        return self._settled.wait(timeout)


# LLM: 按工作项而非线程观察：线程复用到后续工作项不会改变本观察的结论（C5.2）。attempted 在进入 start 时置真；
#   从未 start、本项 done() 或已证明未开始都算 exited；wait_until 在绝对期限内等本项并返回 exited()。
# 类用途: 让工作项句柄满足 C5.2 的 WorkObservation 协议，供后台关停登记与有界调用收尾。
class WorkItemObservation:
    __slots__ = ("_handle", "_attempted")

    def __init__(self, handle: WorkItemHandle) -> None:
        self._handle = handle
        self._attempted = False

    # 函数用途: 只读本观察是否已进入 start。
    @property
    def attempted(self) -> bool:
        return self._attempted

    # 函数用途: 记下启动尝试后交付工作项；交付失败的异常原样抛出。
    def start(self) -> None:
        self._attempted = True
        self._handle.start()

    def proven_not_started(self) -> bool:
        return self._handle.proven_not_started()

    # 函数用途: 无等待判断本工作项的责任是否已结清。
    def exited(self) -> bool:
        handle = self._handle
        return not self._attempted or handle.done() or handle.proven_not_started()

    # LLM: deadline 是绝对 time.monotonic() 时刻；只在剩余预算内等待，自身线程内等待自己时直接返回当前结论。
    # 函数用途: 在期限内等本工作项结清，返回 exited()。
    def wait_until(self, deadline: float) -> bool:
        if self.exited():
            return True
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                self._handle.join(min(remaining, threading.TIMEOUT_MAX))
            except RuntimeError:
                return False
        return self.exited()


# 类用途: 复用线程、从不排队的工作项池。
class WorkItemPool:
    def __init__(self, name: str, *, max_threads: int) -> None:
        if max_threads < 1:
            raise ValueError("max_threads must be at least 1")
        self.name = name
        self.max_threads = max_threads
        self._queue = CancellableWorkQueue()
        self._executor = DurableDaemonThreadPoolExecutor(max_workers=max_threads, thread_name_prefix=name)
        self._executor._work_queue = self._queue
        self._lock = threading.Lock()

    # 函数用途: 建一个尚不可运行的工作项句柄（PREPARED）；调用 start 前没有任何线程能拿到它。
    def prepare(self, fn: Callable[[], object], /, *, context: contextvars.Context,
                on_start: Callable[[], None] | None = None) -> WorkItemHandle:
        return WorkItemHandle(self, fn, context, on_start)

    # 函数用途: prepare 后立即 start，返回已交付的句柄；fn 在 context 内运行，on_start 在 fn 之前运行。
    def submit(self, fn: Callable[[], object], /, *, context: contextvars.Context,
               on_start: Callable[[], None] | None = None) -> WorkItemHandle:
        handle = self.prepare(fn, context=context, on_start=on_start)
        handle.start()
        return handle

    # LLM: 交付与覆盖判定在池锁内串行完成：执行器先按原规则（空闲令牌 → needs_worker → 扩容）处理；之后若线程数
    #   没有增长且排队项仍多于空闲容量，本项就会排队，于是移除排队槽位并改由一次性守护线程运行。执行器拒绝提交
    #   （解释器关闭、扩容线程起不来等）时同样改走一次性线程。排队项若已被某个 worker 取走，两个执行者由
    #   RELEASED → RUNNING 的比较交换决出唯一运行者，另一方只做卫生步骤。
    # 函数用途: 把 RELEASED 的工作项交给池线程，必要时交给一次性线程。
    def _release(self, handle: WorkItemHandle) -> None:
        executor = self._executor
        with self._lock:
            before = len(executor._threads)
            try:
                executor.submit(_run_item, handle)
            except Exception:  # noqa: BLE001 - 执行器不可用时改走一次性线程，失败再向调用方抛出
                overflow = True
            else:
                overflow = len(executor._threads) == before and self._queue.needs_worker()
            if overflow:
                self._queue.discard(handle)
        if overflow:
            self._start_one_shot(handle)

    # LLM: 一次性线程以空 context 出生（3.14t 默认会继承提交者的 context），工作项仍在句柄携带的 context 里运行。
    #   线程起不来时：本项仍未被认领就转 CANCELLED（已证明未开始）并把异常抛给调用方；已被池线程认领则照常返回。
    # 函数用途: 在新的一次性守护线程上运行工作项。
    def _start_one_shot(self, handle: WorkItemHandle) -> None:
        kwargs = {"context": contextvars.Context()} if hasattr(sys.flags, "thread_inherit_context") else {}
        thread = threading.Thread(target=_run_item, args=(handle,), name=f"{self.name}-overflow", daemon=True,
                                  **kwargs)
        try:
            thread.start()
        except BaseException:
            if handle.cancel_if_not_started():
                raise


MODEL_CALL_POOL = WorkItemPool("my-agent-model-pool", max_threads=WORK_ITEM_POOL_MAX_THREADS_COUNT)
TOOL_WAIT_POOL = WorkItemPool("my-agent-tool-wait-pool", max_threads=WORK_ITEM_POOL_MAX_THREADS_COUNT)

__all__ = [
    "MODEL_CALL_POOL",
    "TOOL_WAIT_POOL",
    "WORK_ITEM_POOL_MAX_THREADS_COUNT",
    "WorkItemHandle",
    "WorkItemObservation",
    "WorkItemPool",
    "register_item_local_reset",
]
