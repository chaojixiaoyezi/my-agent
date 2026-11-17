# LLM: gc.callbacks 的 start/stop 在触发回收的线程里成对调用，且同一时刻只有一次回收在进行（解释器的 collecting 标志串行化），
#   所以回调内不取锁：取锁会让“持锁线程分配内存→触发回收→回调再取同一把锁”自锁。读方只对 deque 做一次 tuple 快照。
#   时钟在导入时绑定原函数：回收可能发生在任何代码里，回调不能消费别处替换过的 time.monotonic（测试常用有限序列替身）。
#   自由线程运行时的回收会停下所有线程，所以“某段时间内的停顿”按进程算，与发起线程无关。只计量，不改阈值、不调用 gc.freeze。
# 模块用途: 进程级 GC 停顿计量，供模型调用 stages_ms.gc_pause_ms 与诊断快照读取（S7 / L-09）。
"""Process-wide stop-the-world GC pause meter."""
from __future__ import annotations

import gc
from collections import deque
from time import monotonic as _monotonic

# 只保留最近这么多次回收的单调起止时刻，足够覆盖一次模型请求的准备窗口；更早的停顿只留在累计和直方图里。
_RECENT_PAUSE_LIMIT_COUNT = 512
# 直方图各档上界（毫秒），超过最后一档计入溢出档。
_PAUSE_BUCKET_BOUNDS_MS = (1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0)

_recent: deque[tuple[float, float]] = deque(maxlen=_RECENT_PAUSE_LIMIT_COUNT)
_buckets = [0] * (len(_PAUSE_BUCKET_BOUNDS_MS) + 1)
_state = {"installed": False, "started_at": None, "count": 0, "total_seconds": 0.0, "max_seconds": 0.0}


# LLM: 回调必须极小且不抛异常；没有成对 start 的 stop（安装发生在回收中途）直接忽略。
# 函数用途: 记录一次回收的单调起止时刻、累计耗时与所属直方图档。
def _on_gc(phase: str, _info: dict) -> None:
    now = _monotonic()
    if phase == "start":
        _state["started_at"] = now
        return
    started, _state["started_at"] = _state["started_at"], None
    if started is None or now < started:
        return
    pause = now - started
    _recent.append((started, now))
    _state["count"] += 1
    _state["total_seconds"] += pause
    _state["max_seconds"] = max(_state["max_seconds"], pause)
    millis = pause * 1000.0
    _buckets[next((index for index, bound in enumerate(_PAUSE_BUCKET_BOUNDS_MS) if millis <= bound),
                  len(_PAUSE_BUCKET_BOUNDS_MS))] += 1


# LLM: 幂等；首次调用之前的停顿不会补记。
# 函数用途: 在本进程登记一次 GC 停顿回调。
def install() -> None:
    if _state["installed"]:
        return
    _state["installed"] = True
    gc.callbacks.append(_on_gc)


# LLM: 按与 [started, finished] 的重叠部分求和；未安装或窗口无效时返回 None（未知，不是零）。
#   自由线程运行时快照可能撞上别的线程的回收回调正在追加，deque 报“迭代中被修改”时同样如实返回未知；
#   不改用 _recent.copy()，它在高频回收下拖慢吞吐。
# 函数用途: 返回某段单调时间窗口内整个进程的 GC 停顿毫秒数。
def pause_ms_between(started: float, finished: float) -> float | None:
    if not _state["installed"] or not finished > started > 0.0:
        return None
    try:
        recent = tuple(_recent)
    except RuntimeError:
        return None
    total = sum(max(0.0, min(end, finished) - max(begin, started))
                for begin, end in recent if end > started and begin < finished)
    return round(total * 1000.0, 3)


# 函数用途: 返回累计次数、总/最大停顿和直方图，供诊断读取。
def snapshot() -> dict:
    bounds = [f"le_{bound:g}ms" for bound in _PAUSE_BUCKET_BOUNDS_MS] + [f"gt_{_PAUSE_BUCKET_BOUNDS_MS[-1]:g}ms"]
    return {"installed": _state["installed"], "count": _state["count"],
            "total_ms": round(_state["total_seconds"] * 1000.0, 3), "max_ms": round(_state["max_seconds"] * 1000.0, 3),
            "buckets": dict(zip(bounds, list(_buckets), strict=True))}
