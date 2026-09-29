# LLM: 定时执行"一轮结束后会话任务仍是 active"的唯一收口，以及存量 waiting 的结构化出口。
#   只读结构化事实：后续工作事实来自 SchedulerService.follow_up_facts（组合根注入 conversation.task_follow_up），
#   结算码只按报告的 runtime_reason 选，不解析回复正文。没有后续工作时把任务 CAS 成 blocked、排宿主提示、run 记 failed，
#   job 周期不动。后续工作某一项读不出来时按"仍有后续工作"继续等（fail closed），打节流的结构化告警；只剩读不出、
#   又停满宽限期的 6 倍，才按 SCHEDULED_TASK_FOLLOW_UP_UNREADABLE 结算，避免一个坏记录让任务永远等下去。
#   管理员的人工出口是 /endtask（只要求执行树里没有正在跑的执行，不看后续工作事实）。改动时联查 scheduler/service.py 的 reconcile_waiting_run、conversation/runtime._finish_scheduler_wake_claim
#   与 test_scheduler_waiting_deadlock。
# 模块用途: 决定一轮没做完的定时执行是继续等待后续事件，还是结算成受阻、放 job 按周期继续跑（2026-09-29 修复永久 waiting 死锁）。
from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .repository import SchedulerConflictError, SchedulerNotFoundError, SchedulerRunFinish

if TYPE_CHECKING:
    from ..conversation.task_follow_up import FollowUpFacts
    from .service import SchedulerRunClaim, SchedulerService

logger = logging.getLogger(__name__)

# 一轮结束后任务仍是 active、又没有任何后续工作时的结算码：工具结果无法确认 / 其它原因没做完 / 存量 waiting 被对账解开。
SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN = "SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN"
SCHEDULED_TASK_UNFINISHED = "SCHEDULED_TASK_UNFINISHED"
SCHEDULED_TASK_WAITING_WITHOUT_FOLLOW_UP = "SCHEDULED_TASK_WAITING_WITHOUT_FOLLOW_UP"
# 后续工作事实只剩"读不出来"、且停满宽限期的 6 倍时的结算码（登记在 ERROR_CONTRACTS）。
SCHEDULED_TASK_FOLLOW_UP_UNREADABLE = "SCHEDULED_TASK_FOLLOW_UP_UNREADABLE"
# 存量 waiting 至少停这么久才允许按"没有后续工作"结算：子代理刚结束、它的生命周期唤醒还没发出时，
# 后续工作事实会短暂看不到；10 分钟远大于这段发布间隙，卡死的 run 往往已经停了几个小时。
_WAITING_WITHOUT_FOLLOW_UP_GRACE_SECONDS = 600.0
# 读不出来比"确认没有"更可能是暂时的（锁、半写文件），所以多等几轮：宽限期的 6 倍。
_FOLLOW_UP_UNREADABLE_DEADLINE_FACTOR = 6
# 同一个 run 的"后续工作读不出来"告警至少隔这么久才再打一次，避免对账每拍刷日志。
_UNREADABLE_WARNING_INTERVAL_SECONDS = 600.0
# 进程内的告警节流表：run_id → 上次告警时间；超过这个条数时清掉已过节流窗口的条目。
_UNREADABLE_WARNING_TRACK_LIMIT = 256
_unreadable_warned_at: dict[str, float] = {}
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
#   6 倍，按 SCHEDULED_TASK_FOLLOW_UP_UNREADABLE 结算。结算前都先把任务 CAS 成 blocked、排宿主提示；
#   CAS 成功但结算冲突时，下次对账会按 blocked→failed 收口。
# 函数用途: 解开没有后续工作（或后续工作长期读不出）却一直停在 waiting 的定时执行，让 job 恢复按周期派发；
#   会写调度账本、任务状态和宿主提示。
def settle_stale_waiting(
    service: SchedulerService, run: dict[str, object], *, now: float | None,
) -> dict[str, object] | None:
    current = float(time.time() if now is None else now)
    waited = current - float(run.get("waiting_since") or 0.0)
    if waited < _WAITING_WITHOUT_FOLLOW_UP_GRACE_SECONDS:
        return None
    code = _stale_settlement_code(_follow_up(service, run, (), now=current), waited)
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


# LLM: 还有后续工作或判定不可用（None）→ 空串（继续等）；确认没有 → WAITING_WITHOUT_FOLLOW_UP；
#   只剩读不出的项目 → 停满宽限期 6 倍才给 FOLLOW_UP_UNREADABLE，否则继续等。
# 函数用途: 按后续工作事实和已等待时长选出存量 waiting 的结算码。
def _stale_settlement_code(facts: FollowUpFacts | None, waited: float) -> str:
    if facts is None or facts.present:
        return ""
    if not facts.unreadable:
        return SCHEDULED_TASK_WAITING_WITHOUT_FOLLOW_UP
    deadline = _WAITING_WITHOUT_FOLLOW_UP_GRACE_SECONDS * _FOLLOW_UP_UNREADABLE_DEADLINE_FACTOR
    return SCHEDULED_TASK_FOLLOW_UP_UNREADABLE if waited >= deadline else ""


# LLM: 只转发给 service.follow_up_facts（组合根注入的 task_follow_up 查询）；未注入时返回 None，调用方一律保持等待
#   （这是装配缺陷，不是读取失败，不走"读不出来"的限期结算）。有读不出的项目时打节流的结构化告警。
# 函数用途: 读取一次定时执行对应会话任务的后续工作事实。
def _follow_up(
    service: SchedulerService, run: dict[str, object], ignore_wake_ids: Iterable[str], *, now: float | None,
) -> FollowUpFacts | None:
    if service.follow_up_facts is None:
        return None
    facts = service.follow_up_facts(str(run["thread_id"]), str(run["run_id"]), tuple(ignore_wake_ids))
    if facts.unreadable:
        _warn_unreadable(str(run["run_id"]), facts, float(time.time() if now is None else now))
    return facts


# LLM: 同一个 run 每 _UNREADABLE_WARNING_INTERVAL_SECONDS 最多一条；告警只带 run_id、读不出的项目与错误码、
#   已确认存在的事实码，不带路径或正文。节流表是进程内的，超过上限时清掉过期条目。副作用：写日志、改节流表。
# 函数用途: 记录一次"后续工作事实读不出来"的结构化告警，避免对账每拍刷屏。
def _warn_unreadable(run_id: str, facts: FollowUpFacts, now: float) -> None:
    last = _unreadable_warned_at.get(run_id)
    if last is not None and now - last < _UNREADABLE_WARNING_INTERVAL_SECONDS:
        return
    if len(_unreadable_warned_at) >= _UNREADABLE_WARNING_TRACK_LIMIT:
        for key in [key for key, at in _unreadable_warned_at.items() if now - at >= _UNREADABLE_WARNING_INTERVAL_SECONDS]:
            _unreadable_warned_at.pop(key, None)
    _unreadable_warned_at[run_id] = now
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
    "SchedulerRunOutcome",
    "close_active_run",
    "settle_stale_waiting",
]
