# LLM: 展示观察只挂在真实 handler 边界，不能续租执行权、推断进程健康或制造业务进展；关闭后不得再发布。
# 模块用途: 为没有输出的长工具提供有界运行时间心跳，不调用模型、不建立持久状态。
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..common.cancellation import CancellationToken
from .runtime_contracts import ToolCall


# LLM: 每次执行器调用独占实例；只报告 handler 尚未返回及单调时间，展示异常不能影响工具结果。
# 类用途: 在工具真正开始后启动轻量观察线程，工具返回、异常或取消时停止刷新。
@dataclass
class ToolHandlerLiveness:
    call: ToolCall
    sink: Callable[[dict[str, object]], None] | None = None
    interval_seconds: float = 120.0
    cancellation_token: CancellationToken | None = None
    _stopped: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _started_at: float = field(default=0.0, init=False)

    # LLM: Registry 通过全部前置门后才调用；自动重试沿原同一次观察，拒绝、重放和审批等待不启动线程。
    # 函数用途: 为实际进入 handler 的调用开始计时，短工具不会产生心跳消息。
    def start(self) -> None:
        if self._thread is not None or not callable(self.sink) or self.interval_seconds <= 0:
            return
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="tool-liveness", daemon=True)
        try:
            self._thread.start()
        except RuntimeError:
            self._thread = None  # 展示线程不可用不能阻止真实工具执行。

    # LLM: join 先于 terminal 进度，确保终态之后无迟到心跳；仅等待本实例短展示回调，不等待工具或模型。
    # 函数用途: 关闭观察并收回线程，执行器的异常出口也必须调用。
    def close(self) -> None:
        self._stopped.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()

    # LLM: 时间来自 monotonic，取消读取原 token；只公开工具身份、等待结果和时长，不读取参数、文件或原始 stdout。
    # 函数用途: 按设定间隔刷新同一工具的真实等待状态，取消后不再声明仍在执行。
    def _run(self) -> None:
        while not self._stopped.wait(self.interval_seconds):
            if self.cancellation_token is not None and self.cancellation_token.cancelled:
                return
            event = {
                "call_id": self.call.call_id, "tool": self.call.tool_name,
                "phase": "running", "status": "执行中", "wait_state": "handler_result",
                "elapsed_seconds": round(max(0.0, time.monotonic() - self._started_at), 3),
            }
            try:
                self.sink(event)
            except Exception:  # noqa: BLE001 - 展示故障不得改变真实工具结果
                return
