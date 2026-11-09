"""仅在调用方显式范围内累计本地存储耗时；不保存正文、身份或持久状态。"""
from __future__ import annotations

import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class _StageTiming:
    values: dict[str, float] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)
    closed: bool = False


_TIMING: ContextVar[_StageTiming | None] = ContextVar("local_store_stage_timing", default=None)


@contextmanager
def local_store_stage_timing_scope(stages: dict[str, float]):
    """退出时只合并数字；复制到其它执行上下文的旧范围不能继续改最终结果。"""
    timing = _StageTiming()
    token = _TIMING.set(timing)
    try:
        yield
    finally:
        _TIMING.reset(token)
        with timing.lock:
            timing.closed = True
            for name, milliseconds in timing.values.items():
                stages[name] = round(stages.get(name, 0.0) + milliseconds, 3)


@contextmanager
def local_store_stage(name: str):
    """无诊断范围时不读时钟；失败路径也计入已花费时间，不吞业务异常。"""
    timing = _TIMING.get()
    if timing is None or timing.closed:
        yield
        return
    started = time.monotonic()
    try:
        yield
    finally:
        milliseconds = max(0.0, (time.monotonic() - started) * 1000)
        with timing.lock:
            if not timing.closed:
                timing.values[name] = timing.values.get(name, 0.0) + milliseconds


class LocalStoreConnectionTimingStack(ExitStack):
    """计量原资源栈退出；异常参数、回调次序和抑制结果全部由原ExitStack处理。"""

    def __exit__(self, exc_type, exc_value, traceback):
        with local_store_stage("local_store_connection_release_ms"):
            return super().__exit__(exc_type, exc_value, traceback)
