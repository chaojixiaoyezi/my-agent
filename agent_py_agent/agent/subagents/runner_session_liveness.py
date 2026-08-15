# LLM: runner 会话的「耐久判活」(dispatch 路稳定性专项,真机实锤 §8-2/§7-7 坑A):
#   进程内线程注册表 _background_subagent_dispatches 挂在 agent 实例上,而 gateway 的
#   唤醒轮跑在后台线程私有池的【另一个 agent 实例】(cli/gateway_loops.py `_owner_pool`
#   注释"后台线程私有池")——注册表判活对唤醒轮恒为空 → 活着的子代理被出口孤儿回收
#   整批误判为死、abandon+requeue,编队/多子代理任务反复被屠。本模块给出跨实例、
#   跨进程都成立的判活事实源:runner_session_lease 每 ~5s 把心跳写进任务权威 store
#   (session_pool._heartbeat_loop),读它即可。契约:只读任务属性,零 IO、零信号、
#   不改状态;判据全结构化(status/时间戳)。改动时同步 session_pool.py、
#   background_liveness.py、tests/test_runner_session_liveness.py。
# 模块用途: 回答"这个子代理 run 是不是还有活着的 runner 在别处跑"——不依赖当前
#   进程/实例的内存注册表,靠盘上心跳事实判断。
from __future__ import annotations

import time

# 新鲜阈值下限:心跳间隔默认 5s(session_pool._runner_session_heartbeat_interval),
# 6 拍容忍 GC/磁盘抖动;interval 更大时按 6×interval 放宽。
_MIN_FRESH_SECONDS = 45.0
_FRESH_INTERVAL_MULTIPLIER = 6.0


def runner_session_of(task: object) -> dict:
    """取任务属性里的 runner_session(兼容对象/dict 两种形态;非 dict 归一空 dict)。"""
    attrs = getattr(task, "attributes", None)
    if attrs is None and isinstance(task, dict):
        attrs = task.get("attributes")
    if not isinstance(attrs, dict):
        return {}
    session = attrs.get("runner_session")
    return session if isinstance(session, dict) else {}


def has_fresh_runner_session(task: object, *, now: float | None = None) -> bool:
    """run 是否有「还在跳的 runner 会话」:status=running 且心跳未过期。

    completed/failed 会话(runner 已收尾)不算;心跳过期(宿主进程被杀等硬死亡)
    不算——过期窗内的硬死亡最多延迟一个阈值周期被判死,由周期性 supervision 兜底。
    """
    session = runner_session_of(task)
    if str(session.get("status") or "").strip() != "running":
        return False
    try:
        heartbeat_at = float(session.get("heartbeat_at") or 0.0)
    except (TypeError, ValueError):
        return False
    if heartbeat_at <= 0:
        return False
    moment = time.time() if now is None else now
    return (moment - heartbeat_at) <= _fresh_window_seconds(session)


def _fresh_window_seconds(session: dict) -> float:
    try:
        interval = float(session.get("interval_seconds") or 0.0)
    except (TypeError, ValueError):
        interval = 0.0
    return max(_MIN_FRESH_SECONDS, interval * _FRESH_INTERVAL_MULTIPLIER)


__all__ = ["has_fresh_runner_session", "runner_session_of"]
