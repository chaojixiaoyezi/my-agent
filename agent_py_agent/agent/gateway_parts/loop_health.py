# LLM: Gateway 循环健康账本，进程内唯一事实源：派发线程的起止、每次 tick 的起止时间与派发数、各后台循环（按 loop id：
#   dispatcher/background-main/scheduler-due/owner-maintenance/orphan-reconcile/heartbeat）被守卫接住的异常次数，
#   以及循环错误打印（cli/gateway_loops._print_gateway_loop_error）本身失败的次数。只记内存、不做 IO，所以磁盘写满时
#   仍能记录并经 /status 直接读出；心跳写入把同一份快照落盘。dispatcher_alive 由"已登记、未记退出、登记的线程 ident
#   仍在 threading.enumerate()"共同判定，线程没走到退出记录（vanished）也不会被误报为活。改动时同步
#   cli/gateway_loops.py、gateway_parts/http_handlers.handle_status 与 tests/test_gateway_dispatcher_resilience.py。
# 模块用途: 让心跳文件和 /status 能直接看出"派发线程还活着吗、上一次 tick 是什么时候"，而不是只看 pending 数。
from __future__ import annotations

import threading
import time

from ..common.log_redaction import redact_sensitive_text
from ..runtime_errors import environment_cause_fields


# LLM: 只记类型名、脱敏并截断后的消息、上下文和时间，不存异常对象（避免引用链把线程栈和 agent 留在内存里）；
#   消息会进心跳文件与 /status，所以先过 redact_sensitive_text。包装异常带显式环境根因时补 cause_type/cause_category。
# 函数用途: 把一个异常压成可 JSON 序列化、可对外展示的小记录。
def _error_record(context: str, exc: BaseException) -> dict:
    return {
        "context": context,
        "type": type(exc).__name__,
        "message": redact_sensitive_text(str(exc))[:500],
        "at": time.time(),
        **environment_cause_fields(exc),
    }


# LLM: 所有写入都在同一把锁内；snapshot 返回带派生字段（dispatcher_alive/dispatcher_state）的扁平副本，键名带
#   dispatcher_/dispatch_/loop_error_ 前缀，可直接并入心跳与 /status 载荷。存活判定保存线程对象用 is_alive()，
#   不用 ident（macOS 上 ident 会立即复用，会把 vanished 误报成 running）。reset 只给测试隔离用。
# 类用途: Gateway 派发线程与循环错误打印的健康账本，一个进程一份（模块级 loop_health）。
class GatewayLoopHealth:
    # LLM: 锁是普通 Lock（不可重入），锁内方法不得再调带锁的公开方法。
    # 函数用途: 建锁并清零。
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reset_locked()

    # LLM: 只给测试的 autouse 夹具用，生产路径不调用；清零后状态回到 not_started。
    # 函数用途: 清空全部记录（测试隔离用），生产代码不调用。
    def reset(self) -> None:
        with self._lock:
            self._reset_locked()

    # LLM: 字段的唯一定义处；新增字段要同时进 snapshot 的输出与测试。
    # 函数用途: 锁内清零，__init__ 与 reset 共用。
    def _reset_locked(self) -> None:
        self._thread: threading.Thread | None = None
        self._started_at = 0.0
        self._exited_at = 0.0
        self._exit_error: dict = {}
        self._tick_started_at = 0.0
        self._tick_finished_at = 0.0
        self._tick_count = 0
        self._last_tick_dispatched = 0
        self._loop_errors: dict[str, int] = {}
        self._last_loop_errors: dict[str, dict] = {}
        self._print_failures = 0
        self._last_print_failure: dict = {}

    # LLM: 必须由派发线程自己调用（保存的是 current_thread）；清退出记录是为了同进程重启循环时不残留旧代。
    # 函数用途: 派发线程进入主循环时登记自己（线程对象与时间），并清掉上一代的退出记录。
    def mark_started(self) -> None:
        with self._lock:
            self._thread = threading.current_thread()
            self._started_at = time.time()
            self._exited_at = 0.0
            self._exit_error = {}

    # LLM: 只写时间戳；外部按 started_at > finished_at 判"卡住"，不要在这里加计数。
    # 函数用途: 一次 tick 开始；与 mark_tick_finished 配对，卡住的 tick 表现为 started_at 晚于 finished_at。
    def mark_tick_started(self) -> None:
        with self._lock:
            self._tick_started_at = time.time()

    # LLM: 出错的 tick 也要调（dispatched=0），否则 tick_count 与 last_dispatch_tick_at 会在持续出错时停更。
    # 函数用途: 一次 tick 结束，记录完成时间、累计次数与本轮派发数。
    def mark_tick_finished(self, dispatched: int) -> None:
        with self._lock:
            self._tick_finished_at = time.time()
            self._tick_count += 1
            self._last_tick_dispatched = int(dispatched)

    # LLM: 每一次被守卫接住的异常都记（打印限流不影响计数），按 loop id 分开计数，所以 loop_error_counts 是各循环的真实次数；
    #   派发循环（loop="dispatcher"）同时投影成 dispatch_tick_errors/last_dispatch_tick_error 两个对外字段。
    # 函数用途: 记录某个后台循环里一次已被守卫接住的异常（线程继续跑）。
    def note_loop_error(self, loop: str, context: str, exc: BaseException) -> None:
        with self._lock:
            self._loop_errors[loop] = self._loop_errors.get(loop, 0) + 1
            self._last_loop_errors[loop] = _error_record(context, exc)

    # LLM: 由 _print_gateway_loop_error 的 except 调用，本方法自己不做任何 IO，磁盘写满时也必须成功。
    # 函数用途: 记录一次"循环错误打印本身失败"（典型是 stderr 所在磁盘写满）。
    def note_print_failure(self, context: str, exc: BaseException) -> None:
        with self._lock:
            self._print_failures += 1
            self._last_print_failure = _error_record(context, exc)

    # LLM: stage 是退出阶段（构造/运行/关闭），进 exit_error.context；记了退出后 state 一定是 exited，不再看线程。
    # 函数用途: 派发线程退出时留下结构化事实；正常停止传 None，异常逃逸传该异常与退出阶段。
    def mark_exited(self, error: BaseException | None, stage: str = "gateway_request_loop.exit") -> None:
        with self._lock:
            self._exited_at = time.time()
            self._exit_error = {} if error is None else _error_record(stage, error)

    # LLM: 键名是心跳与 /status 的对外合同，改名要同步 docs/modules/gateway 与测试；dict 字段返回副本。
    # 函数用途: 返回可直接并入心跳/状态载荷的扁平快照（副本）。
    def snapshot(self) -> dict:
        with self._lock:
            state = self._state_locked()
            return {
                "dispatcher_alive": state == "running",
                "dispatcher_state": state,
                "dispatcher_thread_ident": self._thread.ident if self._thread is not None else None,
                "dispatcher_started_at": self._started_at,
                "dispatcher_exited_at": self._exited_at,
                "dispatcher_exit_error": dict(self._exit_error),
                "last_dispatch_tick_started_at": self._tick_started_at,
                "last_dispatch_tick_at": self._tick_finished_at,
                "last_dispatch_tick_dispatched": self._last_tick_dispatched,
                "dispatch_tick_count": self._tick_count,
                "dispatch_tick_errors": self._loop_errors.get("dispatcher", 0),
                "last_dispatch_tick_error": dict(self._last_loop_errors.get("dispatcher", {})),
                "loop_error_counts": dict(self._loop_errors),
                "last_loop_errors": {loop: dict(record) for loop, record in self._last_loop_errors.items()},
                "loop_error_print_failures": self._print_failures,
                "last_loop_error_print_failure": dict(self._last_print_failure),
            }

    # LLM: 线程存活用保存的 Thread.is_alive()：线程对象不会被复用，ident 会。锁内调用，不能再取锁。
    # 函数用途: 四态：not_started（没登记过）、exited（记了退出）、vanished（登记的线程已不在）、running。
    def _state_locked(self) -> str:
        if self._thread is None:
            return "not_started"
        if self._exited_at > 0:
            return "exited"
        if not self._thread.is_alive():
            return "vanished"
        return "running"


loop_health = GatewayLoopHealth()
