# LLM: 进程级工具关口只服务 Gateway 安全重启：排空第二段关闭后，新的有副作用工具在领取执行权之前等待，
# 已领取的继续跑完并计数归零。只读工具不经过这里。等待中收到协作中断就放弃准入，由调用方按“未启动”收口。
# 不跨进程，不持久化，不决定是否重启；开关只由 gateway_parts/restart_service 调用。改动时同步 test_restart_gate.py。
# 模块用途: 让 Gateway 重启前能确认“此刻没有正在执行的副作用工具”，同时不让新工具在重启窗口里开跑。
from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from .interrupt import is_interrupted

_condition = threading.Condition()
_closed = False
_executing = 0
_WAIT_SLICE_SECONDS = 0.2


# LLM: 关口关闭时阻塞调用线程，直到重新打开或本线程被协作中断；进程退出时等待中的守护线程随之结束。
# 函数用途: 包住一次副作用工具的领取与执行；产出 True 表示已准入并计入执行中，False 表示因中断未准入。
@contextmanager
def tool_execution_admission() -> Iterator[bool]:
    global _executing
    admitted = False
    with _condition:
        while _closed:
            if is_interrupted():
                break
            _condition.wait(timeout=_WAIT_SLICE_SECONDS)
        else:
            _executing += 1
            admitted = True
    try:
        yield admitted
    finally:
        if admitted:
            with _condition:
                _executing -= 1
                _condition.notify_all()


# LLM: 只由重启排空第二段调用；关闭后不影响已在执行的工具。
# 函数用途: 关闭关口，之后新的副作用工具都停在领取之前。
def close_tool_gate() -> None:
    global _closed
    with _condition:
        _closed = True
        _condition.notify_all()


# LLM: 重启取消或测试收尾时调用，等待中的线程会立即继续。
# 函数用途: 重新打开关口，让等待中的工具照常开跑。
def open_tool_gate() -> None:
    global _closed
    with _condition:
        _closed = False
        _condition.notify_all()


# LLM: 只读快照，供 /status 投影与排空判定。
# 函数用途: 返回关口是否关闭。
def tool_gate_closed() -> bool:
    with _condition:
        return _closed


# LLM: 只读快照；计数只含已准入、尚未结束的副作用工具。
# 函数用途: 返回此刻正在执行的副作用工具数量。
def executing_tool_count() -> int:
    with _condition:
        return _executing


# LLM: 超时返回 False，不改变关口状态；timeout 为 None 表示一直等。
# 函数用途: 等到执行中的副作用工具数量归零，供重启排空判断能否换进程。
def wait_until_no_executing_tools(timeout: float | None) -> bool:
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    with _condition:
        while _executing > 0:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            _condition.wait(timeout=_WAIT_SLICE_SECONDS if remaining is None else min(_WAIT_SLICE_SECONDS, remaining))
        return True


__all__ = [
    "close_tool_gate",
    "executing_tool_count",
    "open_tool_gate",
    "tool_execution_admission",
    "tool_gate_closed",
    "wait_until_no_executing_tools",
]
