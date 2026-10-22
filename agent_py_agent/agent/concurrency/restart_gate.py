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
_executing_ops: dict[int, dict[str, object]] = {}
_next_token = 0
# 重启闸门轮询等待的秒数；重启间隔不足时按这个切片反复探测，避免忙等。
_WAIT_SLICE_SECONDS = 0.2


# LLM: 关门仍等待或协作中断；仅准入时在同一condition锁内登记/计数，finally按唯一token清理，标签由调用方限定为宿主身份而非参数正文。
# 函数用途: 包住副作用工具的领取与执行，并登记尚未退出的工具身份；True为已准入，False为等待中断。
@contextmanager
def tool_execution_admission(label: dict[str, object] | None = None) -> Iterator[bool]:
    global _executing, _next_token
    admitted, token = False, 0
    with _condition:
        while _closed:
            if is_interrupted():
                break
            _condition.wait(timeout=_WAIT_SLICE_SECONDS)
        else:
            row = {**dict(label or {}), "admitted_at": time.time()}
            _next_token += 1
            token = _next_token
            _executing_ops[token] = row
            _executing += 1
            admitted = True
    try:
        yield admitted
    finally:
        if admitted:
            with _condition:
                _executing -= 1
                _executing_ops.pop(token, None)
                _condition.notify_all()


# LLM: 只由重启排空第二段调用；关闭后不影响已在执行的工具。
# 函数用途: 关闭关口，之后新的副作用工具都停在领取之前。
def close_tool_gate() -> None:
    global _closed
    with _condition:
        _closed = True
        _condition.notify_all()


# LLM: 重启取消或测试收尾时调用，等待者继续；已准入工具的计数/登记必须保留，不能重开门就假装排空。
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


# LLM: 同一condition锁内排序并浅拷贝已准入身份，不含工具参数或输出；只供进程级诊断，不持久化、不授予执行权。
# 函数用途: 返回按准入时刻排序的占门工具清单，默认最多20条，非正上限返回空列表。
def executing_tool_snapshot(limit: int = 20) -> list[dict[str, object]]:
    with _condition:
        rows = sorted(_executing_ops.values(), key=lambda row: float(row.get("admitted_at") or 0.0))
        return [dict(row) for row in rows[:max(0, limit)]]


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
    "executing_tool_snapshot",
    "open_tool_gate",
    "tool_execution_admission",
    "tool_gate_closed",
    "wait_until_no_executing_tools",
]
