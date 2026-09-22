# LLM: 同步 callable 的唯一有界 worker 原语；仅拥有取消/线程保留，不代替模型准入、账本、业务重试或提交。
# 模块用途: 按绝对期限等待调用，及时放弃迟到结果，并保留未退出 worker 与清理资源的准确句柄。
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
    is_interrupted,
    register_interrupt_wakeup,
    register_interruptible,
)

_Result = TypeVar("_Result")
_CALL_LOCK = threading.Lock()
# 这是残留资源保护上限，不是业务并发配置；不能由单次调用放大，optional 必须为基础调用保留一席。
_MAX_RETAINED_CALLS = 32
_CALLS: dict[Hashable, _Call] = {}


# LLM: 到期与业务异常独立，不能把此类型当作已确认底层退出或未计费。
# 类用途: 表示调用者总期限已耗尽。
class BoundedCallTimeoutError(TimeoutError):
    pass


# LLM: 仍有 worker 或清理线程时禁止立即重试，同资源登记在实际退出前不得移除。
# 类用途: 区分已退出超时与仍需保留资源的超时。
class BoundedCallStillRunningError(BoundedCallTimeoutError):
    pass


# LLM: 资源冲突与进程容量拒绝都发生在启动前；reason 是机器事实，不按异常文本判断是否重试。
# 类用途: 明确本次调用尚未启动以及拒绝原因。
class BoundedCallBusyError(RuntimeError):
    # LLM: 不渲染资源键，避免宿主把私有配置或后端对象写进诊断。
    # 函数用途: 保存稳定的准入拒绝原因。
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("有界调用资源仍被占用" if reason == "resource_busy" else "有界调用保留容量已满")


# LLM: 记录拥有准确 Thread/InterruptHandle，starting 防止 Thread.start 前被回收；结果仅供原等待者消费。
# 类用途: 保存一次 callable 的生命周期和唤醒事实，不持有业务提交权。
@dataclass(eq=False)
class _Call:
    call_id: str
    interrupt: InterruptHandle
    wake: threading.Event = field(default_factory=threading.Event)
    starting: bool = True
    worker: threading.Thread | None = None
    result: tuple[bool, object] | None = None

    # LLM: Event 完成不等同实际线程退出，worker 和 cleanup 两者都静止才释放保留。
    # 函数用途: 判断这次调用是否仍占用同资源及进程容量。
    def retained(self) -> bool:
        return self.starting or (self.worker is not None and self.worker.is_alive()) or self.interrupt.cleanup_pending


# LLM: 入口接收宿主稳定资源键与绝对 monotonic 期限，copy_context 只传播上下文；不套第二模型准入。
# 函数用途: 启动并有界等待一次同步操作；准备或等待异常也清理准确句柄，超时不 join，迟到结果没有消费权。
def call_with_deadline(
    operation: Callable[[], _Result],
    *,
    deadline: float,
    resource_key: Hashable,
    interrupt_handle: InterruptHandle | None = None,
    optional: bool = False,
) -> _Result:
    _validate_call(deadline, optional)
    handle = interrupt_handle if interrupt_handle is not None else InterruptHandle()
    _check_cancelled(handle)
    if deadline <= time.monotonic():
        raise BoundedCallTimeoutError("调用总期限已耗尽")
    call = _claim_call(resource_key, handle, optional)
    try:
        context = contextvars.copy_context()
        call.worker = threading.Thread(
            target=context.run, args=(_invoke, call, operation, deadline), name=call.call_id, daemon=True,
        )
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
        with _CALL_LOCK:
            _reap_calls_locked()


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
        if len(_CALLS) >= _MAX_RETAINED_CALLS - int(optional):
            raise BoundedCallBusyError("capacity_exhausted")
        handle.claim()
        call = _Call(f"bounded-call:{uuid.uuid4().hex}", handle)
        _CALLS[key] = call
        return call


# LLM: starting 的变更与回收在同一 registry 锁内；启动失败不遗失句柄，也不回滚已实际启动的线程。
# 函数用途: 启动已认领的 worker，并在启动失败时关闭未绑定取消身份。
def _start_call(call: _Call) -> None:
    assert call.worker is not None
    try:
        call.worker.start()
    except BaseException:
        call.interrupt.close_unstarted()
        raise
    finally:
        with _CALL_LOCK:
            call.starting = False


# LLM: 注册准确句柄后再进入操作，早到取消不清零；Context.run 已由启动方绑定，不在 worker 重建上下文。
# 函数用途: 执行一次 callable 并保留原异常；退出只发布结果唤醒，不提交业务状态。
def _invoke(call: _Call, operation: Callable[[], object], deadline: float) -> None:
    try:
        with register_interruptible(call.call_id, handle=call.interrupt):
            _check_cancelled(call.interrupt)
            if deadline <= time.monotonic():
                raise BoundedCallTimeoutError("调用总期限已耗尽")
            value = operation()
        call.result = (True, value)
    except BaseException as exc:
        call.result = (False, exc)
    finally:
        call.wake.set()


# LLM: 用户取消/外部精确取消优先于期限和迟到结果；期限只取消本 worker，不 join 或额外等待清理。
# 函数用途: 在系统允许的等待窗口内等结果或取消通知，完整有效结果只能在原期限内返回。
def _await_call(call: _Call, deadline: float):
    while True:
        try:
            _check_cancelled(call.interrupt)
        except InterruptedError:
            call.interrupt.cancel()
            raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            call.interrupt.cancel()
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
