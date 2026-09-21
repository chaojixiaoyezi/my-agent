# LLM: 只拥有按 thread_id 隔离的进程内供应退避；调度器决定实例寿命、配置读取和持久来源消费。
# 修改须联测供应错误分路、失败时间锚点、就绪筛选及恢复，不接收完整 Agent/Store 或持久化第二份状态。
# 模块用途: 临时模型供应故障时暂停该会话的后台尝试，让其他会话继续运行；恢复后清除内存退避。
from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import TypeVar

from ..backends.errors import is_provider_transient_error
from ..runtime_errors import compact_error_message

_Report = TypeVar("_Report")


# LLM: 每个 scheduler 持有一个实例，就绪扫描与三条消费路径必须共享它；只接收数值配置，不查询持久状态。
# 类用途: 按会话记录连续供应失败和下次允许尝试的时间；实例销毁即丢弃冷却，不改变任务或唤醒状态。
class ProviderSupplyBackoff:
    # LLM: 构造时固定 base/cap，保留 base 至少一秒和 cap 不小于 base；调用方负责配置读取时机。
    # 函数用途: 初始化本调度器的两张会话冷却表，不启动计时器或后台任务。
    def __init__(self, *, base_seconds: float = 30.0, max_seconds: float = 900.0):
        self._base = max(1.0, float(base_seconds))
        self._cap = max(self._base, float(max_seconds))
        self._streaks: dict[str, int] = {}
        self._next_attempt_at: dict[str, float] = {}

    # LLM: 只读本实例的 deadline；恰好到期即可尝试，就绪扫描与实际消费使用同一判据。
    # 函数用途: 判断某会话是否已结束供应冷却，不读取时钟、不领取或消费任务。
    def should_attempt(self, thread_id: str, now: float) -> bool:
        return now >= self._next_attempt_at.get(str(thread_id), 0.0)

    # LLM: now 是失败时刻；保持原指数、封顶和日志字段，不能改成持久 policy 的失败计数。
    # 函数用途: 增加该会话的内存失败次数并推迟下次尝试，返回供日志使用的事实。
    def record_failure(self, thread_id: str, now: float) -> dict[str, object]:
        key = str(thread_id)
        streak = self._streaks.get(key, 0) + 1
        self._streaks[key] = streak
        delay = min(self._base * (2 ** (streak - 1)), self._cap)
        self._next_attempt_at[key] = now + delay
        return {
            "thread_id": key,
            "consecutive_failures": streak,
            "retry_delay_seconds": delay,
            "next_attempt_at": now + delay,
        }

    # LLM: 只清该 thread 的两张内存记录，返回原次数以保留恢复事件打印条件；不改持久状态。
    # 函数用途: 消费回调返回非 None 报告后清除供应冷却，下一次失败从第一次开始。
    def record_success(self, thread_id: str) -> int:
        key = str(thread_id)
        self._next_attempt_at.pop(key, None)
        return self._streaks.pop(key, 0)


# LLM: 冷却先于回调；只吸收 typed transient，None 保留状态，任意非 None 报告清账，修改联测三条消费路径。
# 函数用途: 执行一次后台消费并处理供应冷却；会调用传入函数、读取单调时钟、更新内存及打印事件。
def consume_with_supply_guard(
    backoff: ProviderSupplyBackoff,
    thread_id: str,
    now: float,
    run: Callable[[], _Report | None],
) -> _Report | None:
    if not backoff.should_attempt(thread_id, now):
        return None
    started = time.monotonic()
    try:
        report = run()
    except Exception as exc:
        # 回合内短重试可能持续数分钟；按失败时刻起算，避免沿用 tick 起点导致冷却已过期。
        failed_at = now + (time.monotonic() - started)
        if not _absorb_provider_supply_failure(backoff, thread_id, failed_at, exc):
            raise
        return None
    if report is not None:
        _note_supply_recovery(backoff, thread_id)
    return report


# LLM: 仅按 is_provider_transient_error 分路；额度耗尽、配置错误和程序异常交回原调度链，不解析错误文字。
# 函数用途: 为临时供应失败登记内存退避并打印结构化日志，返回是否已处理该异常。
def _absorb_provider_supply_failure(
    backoff: ProviderSupplyBackoff, thread_id: str, now: float, exc: BaseException
) -> bool:
    if not is_provider_transient_error(exc):
        return False
    payload = backoff.record_failure(thread_id, now)
    payload["error_type"] = exc.__class__.__name__
    payload["error"] = compact_error_message(exc)
    _print_supply_event("provider_supply_backoff", payload)
    return True


# LLM: 非 None 报告清供应退避，不证明业务交付成功；只有此前确有失败时输出恢复事件。
# 函数用途: 清除当前会话的冷却记录，并按原次数打印一次恢复日志。
def _note_supply_recovery(backoff: ProviderSupplyBackoff, thread_id: str) -> None:
    failed_attempts = backoff.record_success(thread_id)
    if failed_attempts:
        _print_supply_event(
            "provider_supply_resumed",
            {"thread_id": thread_id, "failed_attempts": failed_attempts},
        )


# LLM: 维持原事件前缀、字段顺序、中文 JSON、排序与 flush；运维日志和回归依赖这些事实。
# 函数用途: 将一次供应退避或恢复事件写到标准输出，不另写持久账本。
def _print_supply_event(event: str, payload: dict[str, object]) -> None:
    body = json.dumps({"event": event, **payload}, ensure_ascii=False, sort_keys=True)
    print(f"[gateway-supply-backoff] {body}", flush=True)
