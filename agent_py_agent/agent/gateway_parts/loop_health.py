# LLM: Gateway 循环健康账本，进程内唯一事实源：派发线程的起止、每次 tick 的起止时间与派发数、从 tick 逃逸的异常，
#   以及循环错误打印（cli/gateway_loops._print_gateway_loop_error）本身失败的次数。只记内存、不做 IO，所以磁盘写满时
#   仍能记录并经 /status 直接读出；心跳写入把同一份快照落盘。dispatcher_alive 由"已登记、未记退出、登记的线程 ident
#   仍在 threading.enumerate()"共同判定，线程没走到退出记录（vanished）也不会被误报为活。改动时同步
#   cli/gateway_loops.py、gateway_parts/http_handlers.handle_status 与 tests/test_gateway_dispatcher_resilience.py。
# 模块用途: 让心跳文件和 /status 能直接看出"派发线程还活着吗、上一次 tick 是什么时候"，而不是只看 pending 数。
from __future__ import annotations

import threading
import time


# LLM: 只记类型名、截断后的消息、上下文和时间，不存异常对象（避免引用链把线程栈和 agent 留在内存里）。
# 函数用途: 把一个异常压成可 JSON 序列化的小记录。
def _error_record(context: str, exc: BaseException) -> dict:
    return {"context": context, "type": type(exc).__name__, "message": str(exc)[:500], "at": time.time()}


# LLM: 所有写入都在同一把锁内；snapshot 返回带派生字段（dispatcher_alive/dispatcher_state）的扁平副本，键名带
#   dispatcher_/dispatch_/loop_error_ 前缀，可直接并入心跳与 /status 载荷。reset 只给测试隔离用。
# 类用途: Gateway 派发线程与循环错误打印的健康账本，一个进程一份（模块级 loop_health）。
class GatewayLoopHealth:
    # 函数用途: 建锁并清零。
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reset_locked()

    # 函数用途: 清空全部记录（测试隔离用），生产代码不调用。
    def reset(self) -> None:
        with self._lock:
            self._reset_locked()

    # 函数用途: 锁内清零，__init__ 与 reset 共用。
    def _reset_locked(self) -> None:
        self._thread_ident: int | None = None
        self._started_at = 0.0
        self._exited_at = 0.0
        self._exit_error: dict = {}
        self._tick_started_at = 0.0
        self._tick_finished_at = 0.0
        self._tick_count = 0
        self._last_tick_dispatched = 0
        self._tick_errors = 0
        self._last_tick_error: dict = {}
        self._print_failures = 0
        self._last_print_failure: dict = {}

    # 函数用途: 派发线程进入主循环时登记自己（线程 ident 与时间），并清掉上一代的退出记录。
    def mark_started(self) -> None:
        with self._lock:
            self._thread_ident = threading.get_ident()
            self._started_at = time.time()
            self._exited_at = 0.0
            self._exit_error = {}

    # 函数用途: 一次 tick 开始；与 mark_tick_finished 配对，卡住的 tick 表现为 started_at 晚于 finished_at。
    def mark_tick_started(self) -> None:
        with self._lock:
            self._tick_started_at = time.time()

    # 函数用途: 一次 tick 结束，记录完成时间、累计次数与本轮派发数。
    def mark_tick_finished(self, dispatched: int) -> None:
        with self._lock:
            self._tick_finished_at = time.time()
            self._tick_count += 1
            self._last_tick_dispatched = int(dispatched)

    # 函数用途: 记录一次从 tick 逃逸、已被外层守卫接住的异常（线程继续跑）。
    def note_tick_error(self, context: str, exc: BaseException) -> None:
        with self._lock:
            self._tick_errors += 1
            self._last_tick_error = _error_record(context, exc)

    # 函数用途: 记录一次"循环错误打印本身失败"（典型是 stderr 所在磁盘写满）。
    def note_print_failure(self, context: str, exc: BaseException) -> None:
        with self._lock:
            self._print_failures += 1
            self._last_print_failure = _error_record(context, exc)

    # 函数用途: 派发线程退出时留下结构化事实；正常停止传 None，异常逃逸传该异常与退出阶段。
    def mark_exited(self, error: BaseException | None, stage: str = "gateway_request_loop.exit") -> None:
        with self._lock:
            self._exited_at = time.time()
            self._exit_error = {} if error is None else _error_record(stage, error)

    # 函数用途: 返回可直接并入心跳/状态载荷的扁平快照（副本）。
    def snapshot(self) -> dict:
        with self._lock:
            state = self._state_locked()
            return {
                "dispatcher_alive": state == "running",
                "dispatcher_state": state,
                "dispatcher_thread_ident": self._thread_ident,
                "dispatcher_started_at": self._started_at,
                "dispatcher_exited_at": self._exited_at,
                "dispatcher_exit_error": dict(self._exit_error),
                "last_dispatch_tick_started_at": self._tick_started_at,
                "last_dispatch_tick_at": self._tick_finished_at,
                "last_dispatch_tick_dispatched": self._last_tick_dispatched,
                "dispatch_tick_count": self._tick_count,
                "dispatch_tick_errors": self._tick_errors,
                "last_dispatch_tick_error": dict(self._last_tick_error),
                "loop_error_print_failures": self._print_failures,
                "last_loop_error_print_failure": dict(self._last_print_failure),
            }

    # 函数用途: 四态：not_started（没登记过）、exited（记了退出）、vanished（登记的线程已不在）、running。
    def _state_locked(self) -> str:
        if self._thread_ident is None:
            return "not_started"
        if self._exited_at > 0:
            return "exited"
        if not any(thread.ident == self._thread_ident for thread in threading.enumerate()):
            return "vanished"
        return "running"


loop_health = GatewayLoopHealth()
