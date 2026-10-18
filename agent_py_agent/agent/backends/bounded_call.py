# LLM: 同步callable唯一有界worker；原Call保同一次Context供自产错误只读核源，不运行旧Context、不代替模型准入/业务账/提交。
# 模块用途: 按绝对期限消费同步调用；出生前固定原观察，worker/清理身份未知保留且禁止自动重放。
from __future__ import annotations

import contextvars
import math
import threading
import time
import uuid
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import TypeVar

from ..concurrency.interrupt import (
    InterruptHandle,
    _ThreadExitObservation,
    is_interrupted,
    register_interrupt_wakeup,
    register_interruptible,
)

_Result = TypeVar("_Result")
_CALL_LOCK = threading.Lock()
# 这是残留资源保护上限，不是业务并发配置；不能由单次调用放大，optional 必须为基础调用保留一席。
_MAX_RETAINED_CALL_COUNT = 32
_CALLS: dict[Hashable, _Call] = {}


# LLM: 到期与业务异常独立，不能把此类型当作已确认底层退出或未计费。
# 类用途: 表示调用者总期限已耗尽。
class BoundedCallTimeoutError(TimeoutError):
    pass


# LLM: 仍有 worker 或清理线程时禁止立即重试，同资源登记在实际退出前不得移除。
# 类用途: 区分已退出超时与仍需保留资源的超时。
class BoundedCallStillRunningError(BoundedCallTimeoutError):
    pass


# LLM: 退出未确认独立于业务失败/超时，调用方须保原请求 unknown 和物理责任；不得自动重放或据此释放 P3。
# 类用途: 向宿主报告原 worker/清理在原期限内没有取得物理退出证明，仅携安全调用 ID 和固定原因。
class BoundedCallExitUnconfirmedError(RuntimeError):
    error_code = "BOUNDED_CALL_EXIT_UNCONFIRMED"
    automatic_replay = False

    # LLM: reason 只由本模块固定分支传入；异常文本不渲染资源键、底层异常或业务内容。
    # 函数用途: 保存机器可读的未结清责任及准确有界调用身份。
    def __init__(self, call_id: str, reason: str) -> None:
        self.call_id = call_id
        self.reason = reason
        super().__init__("有界调用的原线程或清理退出尚未确认")


# LLM: 资源冲突与进程容量拒绝都发生在启动前；reason 是机器事实，不按异常文本判断是否重试。
# 类用途: 明确本次调用尚未启动以及拒绝原因。
class BoundedCallBusyError(RuntimeError):
    # LLM: 不渲染资源键，避免宿主把私有配置或后端对象写进诊断。
    # 函数用途: 保存稳定的准入拒绝原因。
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("有界调用资源仍被占用" if reason == "resource_busy" else "有界调用保留容量已满")


# LLM: 原_CALLS独占原Thread/InterruptHandle及出生前固定观察；同一次Context只供错误出处只读核对，不授执行权或运行旧Context。
# 类用途: 保存一次 callable 的结果与原物理责任，drain 直接读取本对象而不维护第二表。
@dataclass(eq=False)
class _Call:
    call_id: str
    interrupt: InterruptHandle
    wake: threading.Event = field(default_factory=threading.Event)
    starting: bool = True
    worker: threading.Thread | None = None
    result: tuple[bool, object] | None = None
    worker_observation: _ThreadExitObservation | None = None
    context: contextvars.Context | None = field(default=None, repr=False)
    _worker_observation_origin: _ThreadExitObservation | None = field(default=None, init=False, repr=False)
    _context_origin: contextvars.Context | None = field(default=None, init=False, repr=False)
    _exit_error_origin: BoundedCallExitUnconfirmedError | None = field(default=None, init=False, repr=False)
    _exit_reason_origin: str | None = field(default=None, init=False, repr=False)
    interrupt_origin: InterruptHandle = field(init=False)

    # LLM: 原取消句柄、Context及自产错误都是一次绑定事实；普通字段赋值不能重签原物理身份或另一个错误，不增加登记表。
    # 函数用途: 保护原引用不被替换，可见 worker/observation 变化仍由统一身份门报告 unknown。
    def __setattr__(self, name, value):
        if (name in ("interrupt_origin", "_worker_observation_origin", "_context_origin", "_exit_error_origin", "_exit_reason_origin")
                and self.__dict__.get(name) is not None
                and value is not self.__dict__[name]):
            raise AttributeError("有界调用原身份不能替换")
        object.__setattr__(self, name, value)

    # LLM: 冻结同一个取消句柄引用，当前可见字段被替换也不能把原清理责任洗成空。
    # 函数用途: 为准确句柄保留身份校验引用，不复制或认领另一份状态。
    def __post_init__(self) -> None:
        self.interrupt_origin = self.interrupt

    # LLM: 同一次copy_context仍是原Thread的实际运行载体；只保存原引用不复制、不执行，不将context内容持久化或打印。
    # 函数用途: 在线程出生前记录同一上下文，后续只读核对自产错误的真实出处。
    def bind_context(self, context: contextvars.Context) -> None:
        if type(context) is not contextvars.Context or self._context_origin is not None:
            raise RuntimeError("有界调用上下文不能重复绑定")
        self.context = self._context_origin = context

    # LLM: 先完成纯观察构造再绑定一次原引用；构造失败时尚无 start 责任，已绑定后的 None/副本不能冒充未构建。
    # 函数用途: 出生前同批记录 worker 与唯一原观察，准备失败不遗留已知从未启动的记录。
    def bind_worker(self, worker: threading.Thread) -> None:
        observation = _ThreadExitObservation(worker)
        with _CALL_LOCK:
            if self.worker is not None or self._worker_observation_origin is not None:
                raise RuntimeError("有界调用线程不能重复绑定")
            self.worker = worker
            self.worker_observation = self._worker_observation_origin = observation

    # LLM: Event/结果不证明退出；只查出生前原观察，缺失/副本/新同Thread观察或内部身份漂移都保留。
    # 函数用途: 无等待核原物理责任，不让当前可见观察或线程字段洗掉容量。
    def retained(self) -> bool:
        observation = self._worker_observation_origin
        return (self.starting or _call_identity_reason(self) is not None
                or self.interrupt_origin.cleanup_pending
                or (observation is not None and not observation.exited()))


# LLM: 稳定资源键/绝对期限/原ContextVar传播不变；同一次Context只保在原Call供错误出处核对，wait_for_exit在T外且不增加模型准入/重试。
# 函数用途: 默认有界消费且不 join；显式收尾只在原期限内等原 worker/清理，未确认抛独立错误并保留责任。
def call_with_deadline(
    operation: Callable[[], _Result],
    *,
    deadline: float,
    resource_key: Hashable,
    interrupt_handle: InterruptHandle | None = None,
    optional: bool = False,
    wait_for_exit: bool = False,
) -> _Result:
    _validate_call(deadline, optional)
    if type(wait_for_exit) is not bool:
        raise ValueError("有界调用等待退出选项必须是布尔值")
    handle = interrupt_handle if interrupt_handle is not None else InterruptHandle()
    _check_cancelled(handle)
    if deadline <= time.monotonic():
        raise BoundedCallTimeoutError("调用总期限已耗尽")
    call = _claim_call(resource_key, handle, optional)
    try:
        context = contextvars.copy_context()
        call.bind_context(context)
        call.bind_worker(threading.Thread(
            target=context.run, args=(_invoke, call, operation, deadline), name=call.call_id, daemon=True,
        ))
        with register_interrupt_wakeup(call.wake), register_interrupt_wakeup(call.wake, handle=handle):
            _check_cancelled(handle)
            if deadline <= time.monotonic():
                raise BoundedCallTimeoutError("调用总期限已耗尽")
            _start_call(call)
            return _await_call(call, deadline)
    except BaseException:
        handle.cancel()
        if call.starting:
            with _CALL_LOCK:
                call.starting = False
            handle.close_unstarted()
        raise
    finally:
        try:
            if wait_for_exit:
                _wait_original_call_exit(call, deadline)
        finally:
            with _CALL_LOCK:
                _reap_calls_locked()


# LLM: 原Call/Thread/取消句柄在等待前后逐一核实，锁外共用原绝对期限；只在本门自产错误时锚定出处，业务抛来的旧错误不能重绑。
# 函数用途: 宿主显式等原 worker 和 cleanup 真实退出，未结清则取消原句柄并抛安全 typed unknown，不延长期限。
def _wait_original_call_exit(call: _Call, deadline: float) -> None:
    observation = call._worker_observation_origin
    reason = _call_identity_reason(call)
    if reason is None and observation is not None and not observation.wait_until(deadline):
        reason = "worker_exit_unconfirmed"
    if reason is None and not call.interrupt_origin.wait_cleanup_until(deadline):
        reason = "cleanup_exit_unconfirmed"
    if reason is None:
        reason = _call_identity_reason(call)
    if reason is not None:
        call.interrupt_origin.cancel()
        raise _original_call_exit_error(call, reason) from None


# LLM: 唯一退出门只为原Call首次自产事件绑定同一error；不从相同ID或业务异常补来源，不增registry或持久账。
# 函数用途: 创建准确物理退出未知事件，并只在原调用对象内保留一次出处锚。
def _original_call_exit_error(call: _Call, reason: str) -> BoundedCallExitUnconfirmedError:
    with _CALL_LOCK:
        if call._exit_error_origin is None:
            error = BoundedCallExitUnconfirmedError(call.call_id, reason)
            error._call_origin = call
            call._exit_error_origin, call._exit_reason_origin = error, reason
        return call._exit_error_origin


# LLM: 只读原_CALLS/Call/error/Context同对象与固定字段；已reap、复制、伪造或可见Context换写都无出处，绝不执行捕获Context。
# 函数用途: 返回自产退出未知错误的原上下文，供宿主严格比对原执行，不证明执行未进入或物理已退出。
def bounded_call_error_origin(error: BaseException) -> contextvars.Context | None:
    if type(error) is not BoundedCallExitUnconfirmedError:
        return None
    call = getattr(error, "_call_origin", None)
    with _CALL_LOCK:
        if (type(call) is not _Call or not any(item is call for item in _CALLS.values())
                or call._exit_error_origin is not error
                or error.call_id != call.call_id or error.reason != call._exit_reason_origin
                or type(call._context_origin) is not contextvars.Context
                or call.context is not call._context_origin):
            return None
        return call._context_origin


# LLM: 等待前后核出生前原观察同一性及其不可变Thread身份；None只在原构建前合法，不依据当前副本状态归还责任。
# 函数用途: 把原取消句柄或 worker 引用变化转换为固定机器原因，不渲染底层对象。
def _call_identity_reason(call: _Call) -> str | None:
    if call.interrupt is not call.interrupt_origin:
        return "interrupt_identity_changed"
    observation = call._worker_observation_origin
    if call.worker_observation is not observation:
        return "worker_identity_changed"
    if observation is None:
        return "worker_identity_changed" if call.worker is not None else None
    if call.worker is not observation.original_thread or not observation._same_identity():
        return "worker_identity_changed"
    return None


# LLM: 时间必须有限，0/过去时间由入口按到期处理；optional 只接受布尔值，不能通过伪类型改资源政策。
# 函数用途: 在创建线程或占用保留之前检查调用参数。
def _validate_call(deadline: float, optional: bool) -> None:
    try:
        valid = not isinstance(deadline, bool) and isinstance(deadline, (int, float)) and math.isfinite(deadline)
    except OverflowError:
        valid = False
    if not valid or type(optional) is not bool:
        raise ValueError("有界调用需要有限绝对期限和布尔 optional")


# LLM: 同资源、全进程上限及普通调用预留在同一锁内裁决；句柄只能认领一次，不创建第二池。
# 函数用途: 无等待领取一次调用保留，拒绝尚未退出的旧请求与容量超限。
def _claim_call(key: Hashable, handle: InterruptHandle, optional: bool) -> _Call:
    with _CALL_LOCK:
        _reap_calls_locked()
        if key in _CALLS:
            raise BoundedCallBusyError("resource_busy")
        if len(_CALLS) >= _MAX_RETAINED_CALL_COUNT - int(optional):
            raise BoundedCallBusyError("capacity_exhausted")
        handle.claim()
        call = _Call(f"bounded-call:{uuid.uuid4().hex}", handle)
        _CALLS[key] = call
        return call


# LLM: 仅出生前固定的原观察记录 start 尝试，当前投影丢失不能改归属；ACK 异常保留，starting仍沿原锁。
# 函数用途: 启动原 worker 一次，异常仍保留原线程并关闭尚未绑定的取消身份。
def _start_call(call: _Call) -> None:
    assert call._worker_observation_origin is not None
    try:
        call._worker_observation_origin.start()
    except BaseException:
        call.interrupt_origin.close_unstarted()
        raise
    finally:
        with _CALL_LOCK:
            call.starting = False


# LLM: 原worker只向出生前固定观察记实际出生锁并注册原句柄；当前投影可失效但不重签，取消/Context.run传播保持。
# 函数用途: 执行一次 callable 并保留原异常；退出只发布结果唤醒，不提交业务状态。
def _invoke(call: _Call, operation: Callable[[], object], deadline: float) -> None:
    try:
        if call._worker_observation_origin is not None:
            call._worker_observation_origin.note_running()
        with register_interruptible(call.call_id, handle=call.interrupt_origin):
            _check_cancelled(call.interrupt_origin)
            if deadline <= time.monotonic():
                raise BoundedCallTimeoutError("调用总期限已耗尽")
            value = operation()
        call.result = (True, value)
    except BaseException as exc:
        call.result = (False, exc)
    finally:
        call.wake.set()


# LLM: 用户取消/外部精确取消优先于期限和迟到结果；只用出生前原句柄，不让替换字段改取消归属，默认不 join。
# 函数用途: 在系统允许的等待窗口内等结果或取消通知，完整有效结果只能在原期限内返回。
def _await_call(call: _Call, deadline: float):
    while True:
        try:
            _check_cancelled(call.interrupt_origin)
        except InterruptedError:
            call.interrupt_origin.cancel()
            raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            call.interrupt_origin.cancel()
            error = BoundedCallStillRunningError if call.retained() else BoundedCallTimeoutError
            raise error("调用总期限已耗尽，迟到结果不再采用")
        if call.result is not None:
            ok, value = call.result
            if ok:
                return value
            assert isinstance(value, BaseException)
            raise value
        call.wake.wait(min(remaining, threading.TIMEOUT_MAX))


# LLM: 当前线程的取消与本调用句柄分别查询，不从父线程继承或清除停止标志。
# 函数用途: 在启动与消费安全点优先抛出用户或精确调用取消。
def _check_cancelled(handle: InterruptHandle) -> None:
    if is_interrupted() or handle.cancelled:
        raise InterruptedError("当前调用已被取消")


# LLM: 仅在 registry 锁下按真实线程及清理状态回收；不创建轮询守护线程，不以结果 Event 代替退出证明。
# 函数用途: 后续调用与当前收尾顺带清理已退出的有限记录。
def _reap_calls_locked() -> None:
    for key, call in tuple(_CALLS.items()):
        if not call.retained():
            _CALLS.pop(key, None)


# LLM: 直接读取原 _CALLS 中实际保留的 Call；不 reap、不 cancel、不 join，不用结果通知代替物理退出。
# 函数用途: 向宿主 drain 返回原调用对象的只读快照，保留 worker 和 cleanup 两类准确责任。
def bounded_call_drain_snapshot() -> tuple[_Call, ...]:
    with _CALL_LOCK:
        return tuple(call for call in _CALLS.values() if call.retained())
