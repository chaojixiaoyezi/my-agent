"""摘要等待展示心跳：复用共享调度器，只重报真实覆盖数，不签发完成事实。"""
from __future__ import annotations

import math
import threading
from contextlib import contextmanager

from ..concurrency.daemon_executor import DeferredCall, SharedDeferredCalls

_SCHEDULER = SharedDeferredCalls('compact-progress')


class _Pulse:
    def __init__(self, callback, interval):
        self.callback, self.interval = callback, interval
        self.lock = threading.RLock()
        self.covered, self.total = 0, 1
        self.closed = False
        self.call = None

    def schedule(self):
        self.call = DeferredCall(self.tick)
        _SCHEDULER.start(self.call, self.interval)

    def report(self, covered, total):
        with self.lock:
            if not self.closed:
                self.covered, self.total = covered, total
                self._emit()

    def _emit(self):
        try:
            self.callback(self.covered, self.total)
        except Exception:
            # 展示错误不拥有压缩完成/恢复权，保持原进度回调的 best-effort 语义。
            pass

    def tick(self):
        with self.lock:
            if not self.closed:
                self._emit()
                self.schedule()

    def close(self):
        # 等待本项在途回调，然后取消下一项；离开摘要调用后不能再发表旧进度。
        with self.lock:
            self.closed = True
            if self.call is not None:
                self.call.cancel()
            self.callback = None


@contextmanager
def compact_progress_pulse(callback, *, interval):
    """只有真实展示回调才订阅，共享常驻线程避免每次压缩重新创建线程。"""
    if callback is None or not math.isfinite(interval) or interval <= 0:
        yield callback
        return
    pulse = _Pulse(callback, interval)
    pulse.schedule()
    try:
        yield pulse.report
    finally:
        pulse.close()
