# LLM: 唤醒毒丸的领域收尾与"领域已是终态"判定的唯一位置（第 3 步 C4）。只按唤醒信封的结构化 reason 与 metadata 分派：
#   - 派活（session_task）：会话任务收成 failed 并带宿主原因码（failure_code），沿 report_task_result 回报发送方；
#   - 会话消息（session_message）：回执不动（2026-09-29 3a 以"做法 2"取代原裁定 b）。毒丸隔离的是唤醒这条执行路径，不代表
#     消息本身有毒；被释放或还没认领的消息仍能交给目标的下一次回合，消息的去留只由回执层自己的上限
#     （SESSION_MESSAGE_RELEASE_LIMIT_REACHED）决定。宿主提示的 details 带 message_dedupe_key 与回执当前状态，注明消息仍待投递；
#   - 其它 reason：不改领域状态。
#   四个入口：唤醒结案（failed_permanently）后的收尾、领取后准入判为"来源已放弃"时的收尾（派活正文达到释放上限）、
#   长时间不计数提醒，以及上一片随进程消失时按它的回合号收尾补充消息（C6，settle_abandoned_turn）。
#   宿主提示来源 wake_poison、code 为原因码，同一会话同一原因码只留最新一条。
#   全部在后台路径上调用：领域写失败只打结构化日志、不抛异常，唤醒结案与否不受影响。第 4 步的人工重放与拒绝提示用
#   wake_domain_status / wake_domain_terminal 判定"领域已是终态、不必重放"，与收尾同一处判据。改动须同步 test_wake_domain_closeout.py、
#   test_wake_attempt_wiring.py 与 WAKE_POISON_PILL 第 6 节。
# 模块用途: 唤醒被结案或来源已放弃时，把派活任务收成 failed 并回报发送方，给会话留一条宿主提示（会话消息只提示、不改回执）。
from __future__ import annotations

import json
from typing import Any

from .background_claim import SESSION_TASK_BODY_ABANDONED_ADMISSION
from .host_notices import HostNotice, host_notice, queue_host_notice
from .models import SESSION_MESSAGE_WAKE_REASON, SESSION_TASK_WAKE_REASON, WakeSignal
from .session_messaging import SESSION_MESSAGE_KEY_FIELD, SESSION_MESSAGE_RELEASE_LIMIT_REACHED
from .session_task_report import report_task_result
from .session_tasks import (
    SESSION_TASK_CANCELLED,
    SESSION_TASK_FAILED,
    SESSION_TASK_TERMINAL_STATUSES,
    SessionTaskUpdate,
)
from .wake_poison import QuarantineDecision, WakeStallAlert

# 唤醒被毒丸结案时派活任务的失败原因码（登记在 ERROR_CONTRACTS）。
SESSION_TASK_WAKE_QUARANTINED = "SESSION_TASK_WAKE_QUARANTINED"
# 宿主提示的来源；code 用唤醒原因码，同一会话同一原因码只留最新一条。
WAKE_POISON_NOTICE_SOURCE = "wake_poison"
# 会话消息回执里"已经结束、不必再投递"的状态：已消费或已拒绝。
_SESSION_MESSAGE_TERMINAL_STATUSES = frozenset({"consumed", "rejected"})
_LOG_PREFIX = "[background-wake-poison]"


# LLM: 唤醒已按 failed_permanently 结案后调用；signal 是结案时落盘的那份信封（读不出时是选批时冻结的副本），只读它的
#   结构化字段。先收领域状态再留宿主提示，任何一步失败都不影响另一步。副作用：写会话任务与回报、写会话线程。
# 函数用途: 唤醒被毒丸结案后，把对应的派活任务收成 failed（会话消息留待下一次回合），并提醒这个会话。
def close_out_quarantined_wake(agent: Any, signal: WakeSignal, decision: QuarantineDecision) -> None:
    reason = _reason(signal)
    text = (f"后台唤醒（{reason or '未知来源'}）同一原因连续失败 {decision.same_cause_count} 次，已停止自动重试。"
            f"原因码：{decision.reason_code}。")
    details: dict[str, str] = {}
    if reason == SESSION_TASK_WAKE_REASON:
        _fail_session_task(agent, signal, SESSION_TASK_WAKE_QUARANTINED)
    elif reason == SESSION_MESSAGE_WAKE_REASON:
        details = _pending_message_details(agent, signal)
        text += "这条会话消息仍待投递，会交给这个会话的下一次回合。" if details else ""
    _notify(agent, signal, host_notice(WAKE_POISON_NOTICE_SOURCE, decision.reason_code, text, details=details))


# LLM: 领取后准入判为"来源已放弃"时，由 background_claim 在结案唤醒（retire_source）之前调用；只处理派活正文达到释放上限
#   （SESSION_TASK_BODY_ABANDONED_ADMISSION）：任务收成 failed 带 SESSION_MESSAGE_RELEASE_LIMIT_REACHED 并回报发送方。
#   会话消息达到上限时回执本身已是 rejected，没有别的领域状态要收。副作用：写会话任务与回报。
# 函数用途: 派活正文反复没消费被放弃时，把任务收成 failed 并告诉派活方。
def close_out_abandoned_source(agent: Any, kwargs: dict, admission: str) -> None:
    signal = kwargs.get("wake_signal") if isinstance(kwargs, dict) else None
    if admission != SESSION_TASK_BODY_ABANDONED_ADMISSION or not isinstance(signal, WakeSignal):
        return
    if _reason(signal) == SESSION_TASK_WAKE_REASON:
        _fail_session_task(agent, signal, SESSION_MESSAGE_RELEASE_LIMIT_REACHED)


# LLM: 连续不计数满提醒窗口（store 已记下这次提醒）时调用，只留宿主提示，不改领域状态。副作用：写会话线程。
# 函数用途: 提醒会话某条后台唤醒长时间因同一原因没能执行。
def notify_stalled_wake(agent: Any, signal: WakeSignal, alert: WakeStallAlert) -> None:
    _notify(agent, signal, host_notice(
        WAKE_POISON_NOTICE_SOURCE, alert.reason_code,
        f"后台唤醒（{_reason(signal) or '未知来源'}）已连续 {alert.uncounted_count} 次没能执行，仍在按退避重试。"
        f"原因码：{alert.reason_code}。"))


# LLM: 领域终态的唯一判定（第 4 步 /wakes 的重放判定与拒绝提示直接导入它，不留本地副本）：派活任务已 done/failed/cancelled 时
#   返回任务状态，会话消息回执已 consumed/rejected 时返回回执状态，其它都返回空串（没结束、其它 reason、读不到记录）。reason 按
#   去空白、小写比较，与领域收尾同一口径。读坏账抛出的异常原样交给调用方。只读。
# 函数用途: 返回一条唤醒对应的派活任务或会话消息的终态状态，没结束时返回空串。
def wake_domain_status(store: Any, signal: WakeSignal) -> str:
    reason = _reason(signal)
    if reason == SESSION_TASK_WAKE_REASON:
        task = store.session_tasks.load(_metadata(signal).get("session_task_id") or "")
        return task.status if task is not None and task.status in SESSION_TASK_TERMINAL_STATUSES else ""
    if reason == SESSION_MESSAGE_WAKE_REASON:
        key = str(_metadata(signal).get(SESSION_MESSAGE_KEY_FIELD) or "").strip()
        receipt = store.guidance.receipt(key) if key else None
        return receipt.status if receipt is not None and receipt.status in _SESSION_MESSAGE_TERMINAL_STATUSES else ""
    return ""


# LLM: C6：尝试账识别出上一片随进程消失（preflight/begin 的 abandoned）且在途记录带回合号时调用。按那一片的回合号收尾它认领过、
#   还没消费的补充消息，与后台片异常结束同一套规则（runtime._settle_unconsumed_background_turn_input）：会话消息退回给下一回合、
#   steer 拒绝、已提交（结果未知）的不动；派活正文只在任务没被取消时退回，给同一任务号的重跑（任务是否取消读 wake_domain_status，
#   与运行时的取消判定读同一条任务记录）。进程死亡没有异常对象，failure 传 None：这次释放不计次；反复死亡由毒丸按
#   attempt:abandoned 计数、满 5 次结案兜底。空回合号不收尾（旧账没有回合号）。收尾失败只打日志，不影响预检或本次尝试。
# 函数用途: 把一片随进程消失的回合认领过但没消费的补充消息退回，让下一回合正常投递；会写补充消息回执、删回合索引。
def settle_abandoned_turn(agent: Any, signal: WakeSignal, turn_id: str) -> None:
    turn = str(turn_id or "").strip()
    store = getattr(agent, "conversation_store", None)
    if not turn or store is None:
        return
    try:
        release_body = wake_domain_status(store, signal) != SESSION_TASK_CANCELLED
        store.guidance.recovery.reject_pending(turn, reject_reserved=True, release_task_body=release_body)
    except Exception as exc:  # noqa: BLE001 - 收尾失败只影响这一片的补充消息，不能拖垮预检或本次尝试
        _log("wake_abandoned_turn_settle_failed", signal, {"error_type": type(exc).__name__})


# LLM: 人工重放前的判定：领域已是终态就不必重放；就是 wake_domain_status 是否非空。只读。
# 函数用途: 判断一条唤醒对应的派活任务或会话消息是否已经结束。
def wake_domain_terminal(store: Any, signal: WakeSignal) -> bool:
    return bool(wake_domain_status(store, signal))


# LLM: 只推进非终态任务（状态机允许 queued/accepted → failed），已终态不动、不重复回报；回报沿 report_task_result 的幂等键。
# 函数用途: 把派活任务收成 failed 并带原因码，再回报发送方。
def _fail_session_task(agent: Any, signal: WakeSignal, failure_code: str) -> None:
    task_id = str(_metadata(signal).get("session_task_id") or "").strip()
    tasks = agent.conversation_store.session_tasks
    try:
        current = tasks.load(task_id) if task_id else None
        if current is None or current.status in SESSION_TASK_TERMINAL_STATUSES:
            return
        failed = tasks.advance(task_id, SessionTaskUpdate(status=SESSION_TASK_FAILED, failure_code=failure_code))
    except (OSError, ValueError, TypeError) as exc:
        _log("wake_domain_closeout_failed", signal, {"domain": "session_task", "error_type": type(exc).__name__})
        return
    report_task_result(agent, failed)


# LLM: 只读回执，不改它（做法 2）。消息还没结束（不是 consumed/rejected）时返回 message_dedupe_key 与回执当前状态，供提示的
#   details 注明"仍待投递"；已结束、没有键或读不出时返回空（提示里就不写这句）。
# 函数用途: 取被隔离的消息唤醒对应的那条消息仍待投递的结构化事实。
def _pending_message_details(agent: Any, signal: WakeSignal) -> dict[str, str]:
    key = str(_metadata(signal).get(SESSION_MESSAGE_KEY_FIELD) or "").strip()
    try:
        receipt = agent.conversation_store.guidance.receipt(key) if key else None
    except Exception as exc:  # noqa: BLE001 - 读不出回执只少一句提示，不影响结案
        _log("wake_domain_closeout_failed", signal, {"domain": "session_message", "error_type": type(exc).__name__})
        return {}
    if receipt is None or receipt.status in _SESSION_MESSAGE_TERMINAL_STATUSES:
        return {}
    return {SESSION_MESSAGE_KEY_FIELD: key, "message_receipt_status": receipt.status}


# 函数用途: 给唤醒所在会话留一条宿主提示（同来源同原因码替换旧的）；写不进去只返回。
def _notify(agent: Any, signal: WakeSignal, notice: HostNotice) -> None:
    queue_host_notice(agent.conversation_store, signal.thread_id, notice, replace_same_code=True)


# 函数用途: 唤醒信封的 reason（小写、去空白）。
def _reason(signal: WakeSignal) -> str:
    return str(signal.reason or "").strip().lower()


# 函数用途: 唤醒信封的 metadata；不是字典时按空处理。
def _metadata(signal: WakeSignal) -> dict:
    return signal.metadata if isinstance(signal.metadata, dict) else {}


# 函数用途: 输出一行结构化日志（与 wake_attempt_tracking 同一前缀），只含结构化字段。
def _log(event: str, signal: WakeSignal, fields: dict) -> None:
    print(f"{_LOG_PREFIX} " + json.dumps(
        {"event": event, "wake_signal_id": signal.wake_signal_id, "thread_id": signal.thread_id, **fields},
        ensure_ascii=False, sort_keys=True), flush=True)


__all__ = [
    "SESSION_TASK_WAKE_QUARANTINED",
    "WAKE_POISON_NOTICE_SOURCE",
    "close_out_abandoned_source",
    "close_out_quarantined_wake",
    "notify_stalled_wake",
    "settle_abandoned_turn",
    "wake_domain_status",
    "wake_domain_terminal",
]
