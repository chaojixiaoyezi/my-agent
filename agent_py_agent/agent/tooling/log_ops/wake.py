
from __future__ import annotations

"""按需唤醒门控 —— 确定性判断是否该拉短命 LLM 研判 run(几个月不挂常驻 LLM,有事才醒,省烧)。

采集 daemon 常驻(确定性,几月续接);研判层按需唤醒:cron 周期调 evaluate_wake,只在 ①紧急信号(高危
候选,秒级)②候选攒够一批 ③距上次研判太久(保底) 时才唤醒,否则不动。跨 run 状态全落盘(研判游标 +
上次研判时间),醒来从游标续,不重不漏。配合无限期值守 = 省烧的长跑(无终止条件只认喊停)。
"""

import math
import time
from dataclasses import dataclass
from typing import Any

from ...common.json_io import read_json_object, write_json_file_atomic
from .store import LogOpsStore

_DEFAULT_BATCH = 20
_DEFAULT_MAX_IDLE = 900.0


def read_review_state(store: LogOpsStore) -> dict[str, Any]:
    """研判侧跨 run 游标:已研判到第几条候选 / 已上报数 / 上次研判时间。"""
    return read_json_object(store.review_path)


def write_review_state(store: LogOpsStore, *, cursor: int, reported: int, at: float) -> None:
    """推进研判游标(短命 run 处理完一批后调,下次唤醒不重复同批)。"""
    store.ensure_dirs()
    write_json_file_atomic(
        store.review_path,
        {"review_cursor": int(cursor), "reported": int(reported), "reviewed_at": float(at)},
    )


def clear_urgent(store: LogOpsStore) -> int:
    """清空紧急队列(高危信号已被研判 run 处理),否则 evaluate_wake 会反复因 urgent 唤醒。返回清掉条数。"""
    n = store.count_urgent()
    try:
        store.urgent_path.unlink()
    except OSError:
        pass
    return n


@dataclass
class WakeDecision:
    """一次唤醒门控的判定:醒不醒 + 为啥 + 各计数(供调用方记账/日志)。"""

    should_wake: bool
    reason: str
    urgent: int = 0
    pending: int = 0
    idle_seconds: float = 0.0
    review_cursor: int = 0
    total_candidates: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_wake": self.should_wake,
            "reason": self.reason,
            "urgent": self.urgent,
            "pending": self.pending,
            "idle_seconds": None if self.idle_seconds == math.inf else round(self.idle_seconds, 1),
            "review_cursor": self.review_cursor,
            "total_candidates": self.total_candidates,
        }


def evaluate_wake(
    store: LogOpsStore,
    *,
    now: float | None = None,
    batch_threshold: int = _DEFAULT_BATCH,
    max_idle_seconds: float = _DEFAULT_MAX_IDLE,
) -> WakeDecision:
    """确定性唤醒门控。判据(任一即唤醒):①紧急队列非空(高危秒级)②未研判候选≥batch ③有候选且距上次研判≥max_idle(保底)。
    都不满足→不唤醒(省 LLM)。跨 run 全靠落盘的研判游标 + 上次研判时间,无状态可重入。"""
    current = time.time() if now is None else now
    total = store.count_candidates()
    state = read_review_state(store)
    cursor = int(state.get("review_cursor") or 0)
    reviewed_at = float(state.get("reviewed_at") or 0.0)
    pending = max(0, total - cursor)
    idle = (current - reviewed_at) if reviewed_at else math.inf
    base = {
        "urgent": store.count_urgent(),
        "pending": pending,
        "idle_seconds": idle,
        "review_cursor": cursor,
        "total_candidates": total,
    }
    if base["urgent"] > 0:
        return WakeDecision(True, "urgent_signal", **base)
    if pending >= batch_threshold:
        return WakeDecision(True, "batch_full", **base)
    if pending > 0 and idle >= max_idle_seconds:
        return WakeDecision(True, "idle_timeout", **base)
    return WakeDecision(False, "no_work", **base)


__all__ = ["WakeDecision", "evaluate_wake", "read_review_state", "write_review_state", "clear_urgent"]
