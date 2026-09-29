# LLM: 定时执行"一轮结束后会话任务仍是 active"的唯一收口，以及存量 waiting 的结构化出口。
#   只读结构化事实：后续工作事实来自 SchedulerService.follow_up（组合根注入的 SchedulerFollowUpPolicy，查询 conversation.task_follow_up），
#   结算码只按报告的 runtime_reason 选，不解析回复正文。没有后续工作时把任务 CAS 成 blocked、排宿主提示、run 记 failed，
#   job 周期不动。后续工作某一项读不出来时按"仍有后续工作"继续等（fail closed），打节流的结构化告警；只剩读不出、
#   又停满宽限期的 6 倍，才按 SCHEDULED_TASK_FOLLOW_UP_UNREADABLE 结算，避免一个坏记录让任务永远等下去。
#   管理员的人工出口是 /endtask（只要求执行树里没有正在跑的执行，不看后续工作事实）。改动时联查 scheduler/service.py 的 reconcile_waiting_run、conversation/runtime._finish_scheduler_wake_claim
#   与 test_scheduler_waiting_deadlock。
# 模块用途: 决定一轮没做完的定时执行是继续等待后续事件，还是结算成受阻、放 job 按周期继续跑（2026-09-29 修复永久 waiting 死锁）。
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .repository import SchedulerConflictError, SchedulerNotFoundError, SchedulerRunFinish

if TYPE_CHECKING:
    from ..conversation.task_follow_up import FollowUpFacts, FollowUpQuery
    from .service import SchedulerRunClaim, SchedulerService

logger = logging.getLogger(__name__)

# 一轮结束后任务仍是 active、又没有任何后续工作时的结算码：工具结果无法确认 / 其它原因没做完 / 存量 waiting 被对账解开。
SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN = "SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN"
SCHEDULED_TASK_UNFINISHED = "SCHEDULED_TASK_UNFINISHED"
SCHEDULED_TASK_WAITING_WITHOUT_FOLLOW_UP = "SCHEDULED_TASK_WAITING_WITHOUT_FOLLOW_UP"
# 后续工作事实只剩"读不出来"、且停满宽限期的 6 倍时的结算码（登记在 ERROR_CONTRACTS）。
SCHEDULED_TASK_FOLLOW_UP_UNREADABLE = "SCHEDULED_TASK_FOLLOW_UP_UNREADABLE"
# 宽限期下限：存量 waiting 至少停这么久才允许按"没有后续工作"结算。实际宽限期取
# max(下限, 5 × orphan_supervision_interval_seconds)：静默死亡的子代理要等孤儿巡查发现，巡查间隔越长宽限期越长。
_WAITING_GRACE_FLOOR_SECONDS = 600.0
_WAITING_GRACE_SUPERVISION_FACTOR = 5
# 存量 waiting 的后续工作判定每个 run 最多隔这么久算一次：后台 1 秒一拍，正常的长等待不能每拍全量重算。
_STALE_CHECK_INTERVAL_SECONDS = 60.0
# 读不出来比"确认没有"更可能是暂时的（锁、半写文件），所以多等几轮：宽限期的 6 倍。
_FOLLOW_UP_UNREADABLE_DEADLINE_FACTOR = 6
# 同一个 run 的"后续工作读不出来"告警至少隔这么久才再打一次，避免对账每拍刷日志。
_UNREADABLE_WARNING_INTERVAL_SECONDS = 600.0
# 进程内的节流表（run_id → 上次时间）超过这个条数时清掉已过窗口的条目。
_THROTTLE_TRACK_LIMIT = 256
_unreadable_warned_at: dict[str, float] = {}
_stale_checked_at: dict[str, float] = {}
# run_id → 首次观察到"只剩读不出"的时间；有会自己消失的后续工作或读全时清掉，进程重启归零。
# 读不出的限期从这里起算，不从 waiting_since 起算：并发处理移走唤醒这类瞬时读错误只会让计时从头开始。
_unreadable_since: dict[str, float] = {}
_TOOL_OUTCOME_UNKNOWN_REASON = "TOOL_OPERATION_OUTCOME_UNKNOWN"
_SCHEDULER_NOTICE_TEXT = {
    SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN: (
        "定时任务 {job} 上一轮有一个工具的执行结果无法确认（可能已经生效），系统没有自动重做，"
        "这一轮已停下并标为受阻。请人工确认是否需要重做；该定时任务之后仍按周期运行。"
    ),
    SCHEDULED_TASK_UNFINISHED: (
        "定时任务 {job} 上一轮没有完成，也没有待处理的后续工作，这一轮已停下并标为受阻。"
        "请查看原因；该定时任务之后仍按周期运行。"
    ),
    SCHEDULED_TASK_WAITING_WITHOUT_FOLLOW_UP: (
        "定时任务 {job} 有一轮长时间停在等待状态、却没有任何后续工作，已结束这一轮并标为受阻；"
        "该定时任务之后仍按周期运行。"
    ),
    SCHEDULED_TASK_FOLLOW_UP_UNREADABLE: (
        "定时任务 {job} 有一轮长时间停在等待状态，但判断它还有没有后续工作所需的记录一直读不出来（可能有文件损坏），"
        "已结束这一轮并标为受阻。请检查相关记录；该定时任务之后仍按周期运行。"
    ),
}


# LLM: facts 是组合根绑定的 task_follow_up_facts（已按 grace_seconds 绑好子代理“刚终态”窗口）；
#   grace_seconds 是存量 waiting 的宽限期，由 waiting_grace_seconds 从配置推出。
# 类用途: 定时执行收口使用的后续工作判定与宽限期。
@dataclass(frozen=True)
class SchedulerFollowUpPolicy:
    facts: Callable[[FollowUpQuery], FollowUpFacts]
    grace_seconds: float = _WAITING_GRACE_FLOOR_SECONDS


# LLM: 只用现有配置 orphan_supervision_interval_seconds 推出宽限期（0 或负数=巡查关闭，按下限）；不新增配置项。
# 函数用途: 计算存量 waiting 的宽限期：max(600, 5 × 孤儿巡查间隔)。
def waiting_grace_seconds(orphan_supervision_interval_seconds: object) -> float:
    try:
        interval = float(orphan_supervision_interval_seconds or 0)
    except (TypeError, ValueError):
        interval = 0.0
    return max(_WAITING_GRACE_FLOOR_SECONDS, _WAITING_GRACE_SUPERVISION_FACTOR * interval)


# LLM: 全部来自同一片后台执行报告的结构化字段；runtime_reason 只用于选择结算码，不解析回复正文。
#   ignore_wake_ids 是调用方正在处理的唤醒，判定后续工作时排除它自身。
# 类用途: 一轮定时执行结束时，收口需要的执行结果事实。
@dataclass(frozen=True)
class SchedulerRunOutcome:
    response: str = ""
    delivery_status: str = ""
    delivery_reason: str = ""
    runtime_status: str = ""
    runtime_reason: str = ""
    ignore_wake_ids: tuple[str, ...] = ()


# LLM: 只有存在结构化的后续工作事实才进 waiting（service.park_waiting）；没有后续工作就把会话任务 CAS 成 blocked、
#   排一条宿主提示，再把这次定时执行记为 failed（service.finish），job 下一周期照常派发。
#   结算码按 runtime_reason 选：工具结果无法确认 → SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN，其它 → SCHEDULED_TASK_UNFINISHED。
#   任务状态 CAS 没成功（别处已改或任务权威读写失败）时只释放租约，交给原领取/对账路径按新状态收口。
# 函数用途: 一轮定时执行结束后任务仍是 active 时调用，决定继续等待还是结算成受阻；会写调度账本、任务状态和宿主提示。
def close_active_run(
    service: SchedulerService, claim: SchedulerRunClaim, outcome: SchedulerRunOutcome, *, now: float | None = None,
) -> dict[str, object] | None:
    facts = _follow_up(service, claim.run, outcome.ignore_wake_ids, now=now)
    if facts is None or facts.present or facts.unreadable:
        return service.park_waiting(claim, response=outcome.response, delivery_status=outcome.delivery_status,
                                    delivery_reason=outcome.delivery_reason, now=now)
    code = (SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN if outcome.runtime_reason == _TOOL_OUTCOME_UNKNOWN_REASON
            else SCHEDULED_TASK_UNFINISHED)
    if not _block_task(service, claim.run, code, now=now):
        service.release(claim, now=now)
        return None
    return service.finish(
        claim, status="failed", response=outcome.response, delivery_status=outcome.delivery_status,
        delivery_reason=outcome.delivery_reason, error_code=code,
        error_message=f"scheduled task left active without follow-up work "
                      f"(runtime_status={outcome.runtime_status or 'unknown'}, "
                      f"runtime_reason={outcome.runtime_reason or 'none'})",
        now=now,
    )


# LLM: 存量 waiting 的出口，只由 reconcile_waiting_run 在任务仍为 active 时调用：waiting_since 早于宽限期、
#   且确认没有后续工作时，按 SCHEDULED_TASK_WAITING_WITHOUT_FOLLOW_UP 结算；只剩读不出来的项目时要停满宽限期的
#   6 倍（从首次观察到"只剩读不出"算起，见 _unreadable_since），按 SCHEDULED_TASK_FOLLOW_UP_UNREADABLE 结算。
#   过了宽限期，GRACE_BOUND_FACTS（不会自己消失的事实）不再计入。
#   同一个 run 的判定每 _STALE_CHECK_INTERVAL_SECONDS 最多算一次。结算前都先把任务 CAS 成 blocked、排宿主提示；
#   CAS 成功但结算冲突时，下次对账会按 blocked→failed 收口。
# 函数用途: 解开没有后续工作（或后续工作长期读不出）却一直停在 waiting 的定时执行，让 job 恢复按周期派发；
#   会写调度账本、任务状态和宿主提示。
def settle_stale_waiting(
    service: SchedulerService, run: dict[str, object], *, now: float | None,
) -> dict[str, object] | None:
    current = float(time.time() if now is None else now)
    waited = current - float(run.get("waiting_since") or 0.0)
    grace = _grace(service)
    if waited < grace or _throttled(_stale_checked_at, str(run["run_id"]), current, _STALE_CHECK_INTERVAL_SECONDS):
        return None
    facts = _follow_up(service, run, (), now=current)
    unreadable_for = current - _unreadable_since.get(str(run["run_id"]), current)
    code = _stale_settlement_code(facts, unreadable_for / grace)
    if not code or not _block_task(service, run, code, now=current):
        return None
    try:
        return service.repository.finish_waiting_run(str(run["run_id"]), SchedulerRunFinish(
            status="failed", response=str(run.get("response") or ""),
            delivery_status=str(run.get("delivery_status") or ""),
            delivery_reason=str(run.get("delivery_reason") or ""),
            error_code=code, error_message="scheduled task was waiting without readable follow-up work", now=current,
        ))
    except (SchedulerConflictError, SchedulerNotFoundError):
        return None


# LLM: 只在已过宽限期时调用，unreadable_periods = 连续"只剩读不出"的时长 / 宽限期（宽限期由配置推出，门槛和
#   6 倍用的是同一个值）。还有会自己消失的后续工作或判定不可用（None）→ 空串（继续等）；GRACE_BOUND_FACTS 已过宽限期，
#   不再计入；确认没有 → WAITING_WITHOUT_FOLLOW_UP；只剩读不出 → 满 6 个宽限期才给 FOLLOW_UP_UNREADABLE，否则继续等。
# 函数用途: 按后续工作事实和"只剩读不出"已持续的宽限期数选出存量 waiting 的结算码。
def _stale_settlement_code(facts: FollowUpFacts | None, unreadable_periods: float) -> str:
    if facts is None or _has_lasting_follow_up(facts):
        return ""
    if not facts.unreadable:
        return SCHEDULED_TASK_WAITING_WITHOUT_FOLLOW_UP
    return SCHEDULED_TASK_FOLLOW_UP_UNREADABLE if unreadable_periods >= _FOLLOW_UP_UNREADABLE_DEADLINE_FACTOR else ""


# LLM: 会自己消失（有机制推进）的事实才算"持续的后续工作"；GRACE_BOUND_FACTS 不算。
# 函数用途: 判断事实里有没有会自己推进的后续工作。
def _has_lasting_follow_up(facts: FollowUpFacts) -> bool:
    from ..conversation.task_follow_up import GRACE_BOUND_FACTS

    return any(item not in GRACE_BOUND_FACTS for item in facts.present)


# LLM: 未注入判定策略时按下限；只读 service.follow_up.grace_seconds。
# 函数用途: 读取这次收口使用的存量 waiting 宽限期。
def _grace(service: SchedulerService) -> float:
    policy = getattr(service, "follow_up", None)
    return float(policy.grace_seconds) if policy is not None else _WAITING_GRACE_FLOOR_SECONDS


# LLM: 只转发给 service.follow_up.facts（组合根注入的 task_follow_up 查询）；未注入时返回 None，调用方一律保持等待
#   （这是装配缺陷，不是读取失败，不走"读不出来"的限期结算）。有读不出的项目时打节流的结构化告警。
# 函数用途: 读取一次定时执行对应会话任务的后续工作事实。
def _follow_up(
    service: SchedulerService, run: dict[str, object], ignore_wake_ids: Iterable[str], *, now: float | None,
) -> FollowUpFacts | None:
    from ..conversation.task_follow_up import FollowUpQuery

    policy = getattr(service, "follow_up", None)
    if policy is None:
        return None
    current = float(time.time() if now is None else now)
    facts = policy.facts(FollowUpQuery(str(run["thread_id"]), str(run["run_id"]), tuple(ignore_wake_ids), current))
    _track_unreadable(str(run["run_id"]), facts, current)
    return facts


# LLM: 只剩读不出（没有会自己推进的事实）时记下首次观察时间并打节流告警；有这类事实或读全时清掉首次时间。
#   表超过 _THROTTLE_TRACK_LIMIT 条时丢掉最早的一半（进程内状态，丢掉只会让计时从头开始，不会误结算）。
# 函数用途: 维护"只剩读不出"的首次观察时间，并在读不出时打告警。
def _track_unreadable(run_id: str, facts: FollowUpFacts, now: float) -> None:
    if facts.unreadable:
        _warn_unreadable(run_id, facts, now)
    if not facts.unreadable or _has_lasting_follow_up(facts):
        _unreadable_since.pop(run_id, None)
        return
    if run_id not in _unreadable_since and len(_unreadable_since) >= _THROTTLE_TRACK_LIMIT:
        # sorted 在 C 层一次性取完快照，并发插入不会让遍历出错。
        for key, _at in sorted(_unreadable_since.items(), key=lambda item: item[1])[:_THROTTLE_TRACK_LIMIT // 2]:
            _unreadable_since.pop(key, None)
    _unreadable_since.setdefault(run_id, now)


# LLM: 进程内节流：同一 run 距上次记录不足 interval 返回 True（本次跳过）；否则记下本次时间返回 False。
#   表超过 _THROTTLE_TRACK_LIMIT 条时清掉已过窗口的条目。副作用：改节流表。
# 函数用途: 按 run 节流一类动作（告警、存量判定）。
def _throttled(table: dict[str, float], run_id: str, now: float, interval: float) -> bool:
    last = table.get(run_id)
    if last is not None and now - last < interval:
        return True
    if len(table) >= _THROTTLE_TRACK_LIMIT:
        # 先拍快照再遍历：Gateway 跨 owner 并发时别的线程可能同时插入，直接遍历共享 dict 会抛 RuntimeError。
        for key in [key for key, at in list(table.items()) if now - at >= interval]:
            table.pop(key, None)
    table[run_id] = now
    return False


# LLM: 同一个 run 每 _UNREADABLE_WARNING_INTERVAL_SECONDS 最多一条；告警只带 run_id、读不出的项目与错误码、
#   已确认存在的事实码，不带路径或正文。节流表是进程内的，超过上限时清掉过期条目。副作用：写日志、改节流表。
# 函数用途: 记录一次"后续工作事实读不出来"的结构化告警，避免对账每拍刷屏。
def _warn_unreadable(run_id: str, facts: FollowUpFacts, now: float) -> None:
    if _throttled(_unreadable_warned_at, run_id, now, _UNREADABLE_WARNING_INTERVAL_SECONDS):
        return
    logger.warning("scheduled task follow-up facts are unreadable", extra={
        "event": "scheduler_follow_up_unreadable", "run_id": run_id,
        "unreadable": [{"item": item, "error_code": code} for item, code in facts.unreadable],
        "present": list(facts.present)})


# LLM: 只在任务仍为 active 时 CAS 成 blocked（blocked 可由用户恢复，不属于不可复活终态）；成功后给该会话排一条
#   来源为 scheduler:<job_id> 的宿主提示（同一 job 只保留最新一条，不刷屏）。提示写不进去时 queue_host_notice 返回 False，不影响结算。
# 函数用途: 把没做完又没有后续工作的定时任务标为受阻，并告诉用户需要人工处理；会写任务状态和线程待送达提示。
def _block_task(service: SchedulerService, run: dict[str, object], code: str, *, now: float | None) -> bool:
    from ..conversation.host_notices import host_notice, queue_host_notice

    run_id = str(run["run_id"])
    store = service.conversation_store
    try:
        blocked = store.tasks.update_status(
            {"task_id": run_id, "status": "blocked", "expected_status": "active", "now": now})
    except Exception:  # noqa: BLE001 - 任务权威读不出时不结算，交给后续对账
        logger.warning("scheduled task could not be blocked", extra={"run_id": run_id}, exc_info=True)
        return False
    if blocked is None:
        return False
    job = str(run.get("job_id") or "")
    queue_host_notice(store, str(run["thread_id"]),
                      host_notice(f"scheduler:{job}", code, _SCHEDULER_NOTICE_TEXT[code].format(job=job)))
    return True


__all__ = [
    "SCHEDULED_TASK_FOLLOW_UP_UNREADABLE",
    "SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN",
    "SCHEDULED_TASK_UNFINISHED",
    "SCHEDULED_TASK_WAITING_WITHOUT_FOLLOW_UP",
    "SchedulerFollowUpPolicy",
    "SchedulerRunOutcome",
    "close_active_run",
    "settle_stale_waiting",
    "waiting_grace_seconds",
]
