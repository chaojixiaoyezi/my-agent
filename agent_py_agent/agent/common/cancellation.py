# LLM: 跨 UI、Gateway、Agent 和工具的唯一进程内取消原语；保持 ContextVar、类型身份和回调语义，同步检查所有直接消费者。
# 模块用途: 让一次调用的取消信号在各层传递，不管理持久 Goal、任务权限或进程清理。

from __future__ import annotations

"""One cancellation token propagated through every cancellable tool boundary."""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

_CURRENT_TOKEN: ContextVar[CancellationToken | None] = ContextVar(
    "my_agent_tool_cancellation_token",
    default=None,
)


# LLM: 共用的线程安全取消状态；不替代持久 attempt 权限，绑定和消费者必须引用同一类。
# 类用途: 保存单次执行的取消标志、外部检查与阻塞操作退出回调。
@dataclass
class CancellationToken:
    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    _external_check: Callable[[], bool] | None = field(default=None, repr=False)
    _callback_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _callbacks: dict[int, Callable[[], object]] = field(default_factory=dict, repr=False)
    reason: str = ""

    # LLM: 设置事件并在锁外调用当前回调；回调应幂等，异常不阻止其余操作退出。
    # 函数用途: 记录取消原因并通知本令牌关联的等待操作。
    def cancel(self, reason: str = "cancelled") -> None:
        self.reason = str(reason or "cancelled").strip() or "cancelled"
        self._event.set()
        with self._callback_lock:
            callbacks = tuple(self._callbacks.values())
        for callback in callbacks:
            try:
                callback()
            except Exception:
                continue

    # LLM: 保持外部检查异常时视作取消的关闭语义；检查函数不携带权限。
    # 函数用途: 查询本地事件或宿主传入的取消状态。
    @property
    def cancelled(self) -> bool:
        if self._event.is_set():
            return True
        check = self._external_check
        if check is None:
            return False
        try:
            return bool(check())
        except Exception:
            return True

    # LLM: 统一抛出当前模块的 ToolCancelled；异常类型必须与所有捕获方一致。
    # 函数用途: 在执行边界阻止已经取消的调用继续。
    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ToolCancelled(self.reason or "cancelled")

    # LLM: 只等待本令牌事件，不轮询外部检查；保持原 timeout 与返回值语义。
    # 函数用途: 让等待方有界等待显式取消通知。
    def wait(self, timeout: float | None = None) -> bool:
        """Wait for cancellation without exposing the token's event object."""

        return self._event.wait(timeout)

    # LLM: 登记与取消检查共用锁，退出作用域必撤销；已取消时立即在锁外通知。
    # 函数用途: 为一次阻塞操作临时挂接幂等退出回调，防止残留到下一次调用。
    @contextmanager
    def register_callback(self, callback: Callable[[], object]) -> Iterator[None]:
        """Install an idempotent abort hook for one blocking operation."""

        key = id(callback)
        run_now = False
        with self._callback_lock:
            if self.cancelled:
                run_now = True
            else:
                self._callbacks[key] = callback
        if run_now:
            try:
                callback()
            except Exception:
                pass
        try:
            yield
        finally:
            with self._callback_lock:
                self._callbacks.pop(key, None)


# LLM: 跨层共用取消异常，不新增旧模块别名或第二个异常类型。
# 类用途: 将已取消与普通工具失败分开报告。
class ToolCancelled(RuntimeError):
    pass


# LLM: 只接受唯一 CancellationToken 类型，嵌套退出恢复原 ContextVar；不跨线程自动复制。
# 函数用途: 在当前执行上下文临时绑定宿主取消令牌。
@contextmanager
def bind_cancellation_token(token: object) -> Iterator[None]:
    """Bind the host token only for the current handler thread/context."""

    value = token if isinstance(token, CancellationToken) else None
    context_token = _CURRENT_TOKEN.set(value)
    try:
        yield
    finally:
        _CURRENT_TOKEN.reset(context_token)


# LLM: 只读当前 ContextVar，不创建令牌或推断其他任务身份。
# 函数用途: 让原工具链取得本次调用绑定的取消状态。
def current_cancellation_token() -> CancellationToken | None:
    return _CURRENT_TOKEN.get()


# LLM: 未绑定令牌时保持未取消；不查询持久状态或全局任务。
# 函数用途: 供轮询执行路径轻量判断本次调用是否应停止。
def cancellation_requested() -> bool:
    token = current_cancellation_token()
    return bool(token and token.cancelled)


# LLM: 复用绑定令牌的异常语义，未绑定时不改变原执行路径。
# 函数用途: 在可取消工具边界检查并抛出统一取消异常。
def raise_if_cancelled() -> None:
    token = current_cancellation_token()
    if token is not None:
        token.raise_if_cancelled()


# LLM: 只向当前令牌登记临时回调，退出必撤销；未绑定时保留原无取消执行。
# 函数用途: 让阻塞工具接收本次调用的取消通知。
@contextmanager
def register_cancellation_callback(
    callback: Callable[[], object],
) -> Iterator[None]:
    token = current_cancellation_token()
    if token is None:
        yield
        return
    with token.register_callback(callback):
        yield


__all__ = [
    "CancellationToken",
    "ToolCancelled",
    "bind_cancellation_token",
    "cancellation_requested",
    "current_cancellation_token",
    "raise_if_cancelled",
    "register_cancellation_callback",
]
