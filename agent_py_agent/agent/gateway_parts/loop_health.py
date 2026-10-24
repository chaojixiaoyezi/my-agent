# LLM: Gateway 循环健康账本，进程内唯一事实源：派发线程的起止、每次 tick 的起止时间与派发数、各后台循环（按 loop id：
#   dispatcher/background-main/scheduler-due/owner-maintenance/orphan-reconcile/heartbeat）被守卫接住的异常次数，
#   以及循环错误打印（cli/gateway_loops._print_gateway_loop_error）本身失败的次数。只记内存、不做 IO，所以磁盘写满时
#   仍能记录并经 /status 直接读出；心跳写入把同一份快照落盘。dispatcher_alive 由已登记、未记退出和原Thread.is_alive()
#   共同判定，不借会复用的ident判活；线程消失未记退出也不误报为活。改动时同步
#   cli/gateway_loops.py、gateway_parts/http_handlers.handle_status 与 tests/test_gateway_dispatcher_resilience.py。
# 模块用途: 让心跳文件和 /status 能直接看出"派发线程还活着吗、上一次 tick 是什么时候"，而不是只看 pending 数。
from __future__ import annotations

import math
import threading
import time

from ..common.log_redaction import redact_sensitive_text
from ..runtime_errors import environment_cause_fields

DISPATCHER_STALL_TIMEOUT_SECONDS = 120.0


# LLM: 生产快照用单调钟及显式在途位，epoch只展示；显式now或旧字典仍按原epoch接口评估，不混用时钟。
#   不改线程alive或退出态，空闲旧时间与worker执行时长不是卡死证据。
# 函数用途: 识别在途tick超时或已有pending却长时间没有tick进展。
def evaluate_dispatcher_stall(snapshot: dict[str, object], *, now: float | None = None,
        stall_timeout_s: float = DISPATCHER_STALL_TIMEOUT_SECONDS, pending_count: int = 0) -> dict[str, object]:
    monotonic = now is None and "dispatch_tick_inflight" in snapshot
    if now is None:
        now = time.monotonic() if monotonic else time.time()
    started_key = "dispatch_tick_started_monotonic" if monotonic else "last_dispatch_tick_started_at"
    finished_key = "dispatch_tick_finished_monotonic" if monotonic else "last_dispatch_tick_at"
    started = float(snapshot.get(started_key, 0) or 0)
    finished = float(snapshot.get(finished_key, 0) or 0)
    inflight = bool(snapshot["dispatch_tick_inflight"]) if monotonic else started > finished
    reason, age = "", 0.0
    if (snapshot.get("dispatcher_state") == "running" and started > 0 and stall_timeout_s > 0
            and all(math.isfinite(value) for value in (now, started, finished, stall_timeout_s))):
        if inflight and now - started >= stall_timeout_s:
            reason, age = "tick_wedged", now - started
        elif pending_count > 0 and now - max(started, finished) >= stall_timeout_s:
            reason, age = "pending_starved", now - max(started, finished)
    return {**snapshot, "dispatcher_state": "stalled" if reason else snapshot.get("dispatcher_state"),
        "dispatcher_stalled": bool(reason), "dispatcher_stall_reason": reason,
        "dispatcher_stall_age_s": age, "dispatcher_stall_timeout_s": stall_timeout_s}


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
#   snapshot保原生命周期事实；HTTP/心跳统一用evaluate_dispatcher_stall及本次pending计算stalled，不在只读投影里领取诊断。
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

    # LLM: 初始化全部账本字段；_stall_reported是内部episode闩，不对外投影；monotonic起止与_tick_inflight供snapshot同锁读取。
    # 函数用途: 锁内清零生命周期、双时钟tick、在途标记、诊断闩及错误统计，构造和测试reset共用。
    def _reset_locked(self) -> None:
        self._stall_reported = False
        self._thread: threading.Thread | None = None
        self._started_at = 0.0
        self._exited_at = 0.0
        self._exit_error: dict = {}
        self._tick_started_at = 0.0
        self._tick_finished_at = 0.0
        self._tick_started_monotonic = 0.0
        self._tick_finished_monotonic = 0.0
        self._tick_inflight = False
        self._tick_count = 0
        self._last_tick_dispatched = 0
        self._loop_errors: dict[str, int] = {}
        self._last_loop_errors: dict[str, dict] = {}
        self._print_failures = 0
        self._last_print_failure: dict = {}

    # LLM: 派发线程自己保存current_thread；清退出与_stall_reported。含monotonic的tick起止及_tick_inflight仍由起止接口维护，登记不冒认完成。
    # 函数用途: 登记当前派发线程与启动墙钟，清除旧退出记录并允许新一轮卡死诊断。
    def mark_started(self) -> None:
        with self._lock:
            self._stall_reported = False
            self._thread = threading.current_thread()
            self._started_at = time.time()
            self._exited_at = 0.0
            self._exit_error = {}

    # LLM: 锁内同时记展示墙钟、超时单调钟与在途位，不加计数；校时不能影响生产卡死判定。
    # 函数用途: 标记一次tick开始，与完成接口配对。
    def mark_tick_started(self) -> None:
        with self._lock:
            self._tick_started_at = time.time()
            self._tick_started_monotonic = time.monotonic()
            self._tick_inflight = True

    # LLM: 同一tick内跨长维护段（恢复、终态补投影、输入对账、孤儿锁清扫）完成一步后只刷新在途单调钟，
    #   保留last_dispatch_tick_started_at墙钟起点不变，避免有界维护段累计耗时误触发tick_wedged。
    # 函数用途: 在同一派发tick的相邻维护段之间记录正常单调进展。
    def note_dispatch_tick_progress(self) -> None:
        with self._lock:
            if self._tick_inflight:
                self._tick_started_monotonic = time.monotonic()

    # LLM: 出错tick也要调用；起止同锁记录单调进展并清在途位，不借墙钟大小判断是否已完成。
    # 函数用途: 一次 tick 结束，记录完成时间、累计次数与本轮派发数。
    def mark_tick_finished(self, dispatched: int) -> None:
        with self._lock:
            self._tick_finished_at = time.time()
            self._tick_finished_monotonic = time.monotonic()
            self._tick_inflight = False
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

    # LLM: 仅记录是否尝试过本轮卡死诊断，不做IO；正常进展后重置，失败也不每拍放大日志故障。
    # 函数用途: 原子领取一次卡死episode的诊断责任。
    def note_dispatcher_stall(self, stalled: bool) -> bool:
        with self._lock:
            first = stalled and not self._stall_reported
            self._stall_reported = stalled
            return first

    # LLM: 保留原epoch展示键，新增单调起止及在途位供生产判定；字段同锁取样，dict字段返回副本。
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
                "dispatch_tick_started_monotonic": self._tick_started_monotonic,
                "dispatch_tick_finished_monotonic": self._tick_finished_monotonic,
                "dispatch_tick_inflight": self._tick_inflight,
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
