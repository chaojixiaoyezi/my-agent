# LLM: 展示观察只挂在真实等待边界，工具和模型共用同一时钟；不能续租执行权或制造业务进展，关闭后不得再发布。
# 模块用途: 为没有可见输出的工具或模型请求提供有界等待心跳，不调用模型、不建立持久状态。
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..common.cancellation import CancellationToken

_DISPLAY_JOIN_SECONDS = 0.1


# LLM: 每次真实等待独占实例；身份与阶段由宿主冻结，单调时间仅说明请求未返回，展示异常不能影响执行结果。
# 类用途: 在工具或模型真正开始后启动轻量观察线程，返回、异常、取消或外层放弃时停止刷新。
@dataclass
class ActivityHeartbeat:
    event_fields: dict[str, object]
    sink: Callable[[dict[str, object]], None] | None = None
    interval_seconds: float = 120.0
    cancellation_token: CancellationToken | None = None
    guarded_sink: Callable[..., None] | None = None
    _stopped: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _started_at: float = field(default=0.0, init=False)

    # LLM: 展示身份由可信调用方传入，创建时拷贝；不得保存 prompt、工具参数、模型增量或凭据。
    # 函数用途: 冻结轻量等待事件的结构化身份，避免运行期间被其它调用替换。
    def __post_init__(self) -> None:
        self.event_fields = dict(self.event_fields)

    # LLM: 调用方通过前置门后才调用；工具重放、审批等待、模型发出前拒绝不启动线程。
    # 函数用途: 为实际进入 handler 或 generate 的调用开始计时，短等待不会产生消息。
    def start(self) -> None:
        if (self._thread is not None or self._stopped.is_set() or not (callable(self.sink) or callable(self.guarded_sink)) or self.interval_seconds <= 0
                or self.cancellation_token is not None and self.cancellation_token.cancelled):
            return
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="execution-liveness", daemon=True)
        try:
            self._thread.start()
        except RuntimeError:
            self._thread = None  # 展示线程不可用不能阻止真实工具执行。

    # LLM: stop 不等待已进入外部 sink 的回调；guard 必须先停止新事件，再取消真实 transport。
    # 函数用途: 立即关闭观察的发布资格，不阻塞用户停止或超时。
    def stop(self) -> None:
        self._stopped.set()

    # LLM: 关闭状态与 token 是展示资格，不替代执行权；下一落盘/投影边界复核同一个谓词。
    # 函数用途: 判断本观察是否仍能追加新进度，供 writer 的 typed 接口读取。
    def is_current(self) -> bool:
        return not self._stopped.is_set() and not (self.cancellation_token is not None and self.cancellation_token.cancelled)

    # LLM: join 有短固定上界；已进入第三方 sink/I/O 的回调不可撤销，False 不宣称线程已回收。
    # 函数用途: 停止新发布并有界收回观察，不能用展示线程阻塞模型取消或终态。
    def close(self) -> bool:
        self.stop()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=_DISPLAY_JOIN_SECONDS)
        return self._thread is None or not self._thread.is_alive()

    # LLM: 时间来自 monotonic，取消读取原 token；只公开宿主等待身份和时长，不读取参数、文件、思考或 stdout。
    # 函数用途: 按原间隔刷新真实经过时间，连续三次展示失败才停止；成功清零，取消立即失权。
    def _run(self) -> None:
        failures = 0
        while not self._stopped.wait(self.interval_seconds):
            if not self.is_current():
                return
            event = {**self.event_fields,
                     "elapsed_seconds": round(max(0.0, time.monotonic() - self._started_at), 3)}
            try:
                if self.guarded_sink is not None:
                    self.guarded_sink(event, still_current=self.is_current)
                else:
                    self.sink(event)
                failures = 0
            except Exception:  # noqa: BLE001 - 展示故障不得改变真实工具结果
                failures += 1
                if failures >= 3:
                    return
