# LLM: A4 持续型委派语义(底座提升):持续/驻守型任务派给子代理时,生命周期语义要
#   跟着下去——盯守类真机实锤:子代理按"做完即退"产出首批发现就 DONE,整任务停摆。
#   本模块是该语义的唯一事实源:声明(create_subagents 的 long_running +
#   service_window_seconds,经 attributes 透传落任务)→ 消费(①子代理收口层:窗口未
#   走完不因"落了一次产物"被系统提前收口;②父代理 wake 侧:窗口未走完就终态 → 载荷带
#   结构化事实,提示重派/接管)。纯结构化:attributes + created_at,零自然语言判断。
# 模块用途: 计算持续型任务声明的值守窗口还剩多少秒(0=无窗口语义/已走完)。
from __future__ import annotations

import time


def service_window_remaining_seconds(task: object, *, now: float | None = None) -> float:
    """声明缺失/非 long_running/时间戳缺失 → 0(无窗口语义,行为与旧完全一致)。
    锚点用任务创建时刻:接管/重派会生成新任务,窗口随之重新起算,不因重试漂移。"""
    attrs = getattr(task, "attributes", {}) or {}
    if not isinstance(attrs, dict) or not attrs.get("long_running"):
        return 0.0
    try:
        window = float(attrs.get("service_window_seconds") or 0)
    except (TypeError, ValueError):
        return 0.0
    anchor = float(getattr(task, "created_at", 0.0) or 0.0)
    if window <= 0 or anchor <= 0:
        return 0.0
    return max(0.0, anchor + window - (time.time() if now is None else now))


__all__ = ["service_window_remaining_seconds"]
