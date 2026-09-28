# LLM: 会话间派活正常结束时的自动回报。判定只用结构化字段：
#   目标回合的 request id（来自 request.task_id）→ 在 SessionTaskStore 里按 conversation_request_id
#   找回它属于哪条任务 → 只在"当前不是终态"且"这次回合确实结束"时推进到 done/failed，并把
#   结构化结果（状态、产物 refs、摘要）作为一条来源明确的消息投回发送方的 guidance 队列。
#   幂等：状态机只允许 accepted → done|failed，重复收口不会写第二条回报（终态后直接跳过）。
#   失败不能影响回合本身已经完成的交付：调用方只保留摘要，不抛异常。
# 模块用途: 把目标任务回合的正常结束变成发送方可见的结构化回报。
from __future__ import annotations

import time
from dataclasses import dataclass

from .session_tasks import (
    SESSION_TASK_DONE,
    SESSION_TASK_FAILED,
    SESSION_TASK_TERMINAL_STATUSES,
    SessionTaskUpdate,
)

# 回报正文只写结构化事实；来源用与取消通知相同的 session_task 标记，注入时按宿主事件呈现。
_RESULT_ORIGIN_KIND = "session_task"


# LLM: 命中条件全部是结构化事实：任务记录里绑定的 request id 必须与本次回合一致，且任务尚未到终态。
#   同一回合只会有唯一一条任务记录（bind_turn 拒绝改绑），所以不会误报别的任务。
# 函数用途: 按本次回合的 request id 找回它对应的、尚未结束的会话任务。
def task_for_turn(agent: object, turn_id: str) -> object | None:
    bound_turn = str(turn_id or "").strip()
    if not bound_turn:
        return None
    store = getattr(agent, "conversation_store", None)
    tasks = getattr(store, "session_tasks", None)
    if tasks is None:
        return None
    try:
        loaded, load_errors = tasks.list_report(limit=0)
    except (OSError, ValueError, TypeError):
        return None
    if load_errors:
        # 读坏账时不猜归属：宁可不回报，也不把结果记到错的任务上。
        return None
    for task in loaded:
        if str(getattr(task, "conversation_request_id", "") or "").strip() != bound_turn:
            continue
        if str(getattr(task, "status", "") or "") in SESSION_TASK_TERMINAL_STATUSES:
            continue
        return task
    return None


# LLM: 回合结果只承载结构化事实（成功与否 + 摘要 + 产物 refs），不含正文副本。
# 类用途: 保存一次回合结束时的结果，供任务终态与回报使用。
@dataclass(frozen=True)
class TaskTurnOutcome:
    ok: bool
    summary: str = ""
    result_refs: tuple[str, ...] = ()


# LLM: 只在任务已经绑到某个回合时才推进；status 只允许 done/failed（状态机保证 accepted → 二者之一）。
#   summary 与 result_refs 是结构化结果，正文不复制。
# 函数用途: 把一条会话任务按回合结果推进到终态。
def finish_task_for_turn(
    agent: object,
    turn_id: str,
    outcome: TaskTurnOutcome,
) -> object | None:
    task = task_for_turn(agent, turn_id)
    if task is None:
        return None
    tasks = agent.conversation_store.session_tasks
    target_status = SESSION_TASK_DONE if outcome.ok else SESSION_TASK_FAILED
    try:
        updated = tasks.advance(
            str(getattr(task, "task_id", "") or ""),
            SessionTaskUpdate(
                status=target_status,
                summary=str(outcome.summary or ""),
                result_refs=tuple(outcome.result_refs or ()),
            ),
        )
    except (OSError, ValueError, TypeError):
        return None
    return updated


# LLM: 回报是发送方可见的结构化消息：走 guidance 幂等入队，dedupe_key 用任务 id + 终态，
#   重复收口不会写第二条。投递失败不改变任务已到终态的事实（不回滚状态机）。
# 函数用途: 把任务终态作为一条来源明确的消息排队回发送方。
def report_task_result(agent: object, task: object) -> bool:
    task_id = str(getattr(task, "task_id", "") or "").strip()
    status = str(getattr(task, "status", "") or "").strip()
    sender = str(getattr(task, "sender_thread_id", "") or "").strip()
    if not task_id or not status or not sender:
        return False
    store = getattr(agent, "conversation_store", None)
    guidance = getattr(store, "guidance", None)
    if guidance is None:
        return False
    summary = str(getattr(task, "summary", "") or "").strip()
    refs = tuple(str(item) for item in (getattr(task, "result_refs", ()) or ()))
    lines = [
        f"你派出的任务 {task_id} 已结束：状态 {status}。",
    ]
    if summary:
        lines.append(f"摘要：{summary}")
    if refs:
        lines.append("产物：" + "、".join(refs))
    lines.append("这条消息由宿主自动发出，是结构化结果，不是目标会话的原话。")
    try:
        guidance.append_once(
            {
                "target_type": "thread",
                "target_id": sender,
                "message": "\n".join(lines),
                "sender": str(getattr(task, "target_thread_id", "") or ""),
                "priority": "normal",
                "delivery": "next_turn",
                "metadata": {
                    "origin_kind": _RESULT_ORIGIN_KIND,
                    "origin_thread_id": str(getattr(task, "target_thread_id", "") or ""),
                    "session_task_id": task_id,
                    "session_task_status": status,
                },
            },
            dedupe_key=f"session_task_result:{task_id}:{status}",
        )
    except Exception:  # noqa: BLE001 - 回报失败不能影响回合已完成的事实
        return False
    _record_pair(store, str(getattr(task, "target_thread_id", "") or ""), sender)
    return True


# LLM: 回报是宿主自动发出、不会被拒的消息，但必须占用每对会话配额，否则回报可以绕过限额。
#   计数写失败只影响节流精度，不影响回报已投递的事实。
# 函数用途: 把一条任务回报计入发送方与目标方之间的配额。
def _record_pair(store: object, sender_thread_id: str, target_thread_id: str) -> None:
    from .session_pair_rate import record_pair_message

    record_pair_message(store, sender_thread_id, target_thread_id)


# LLM: 一次收口只做"推进 + 回报"两件事，且只在真的推进了这条任务时回报。
#   已经是终态、不是会话任务回合、或读账失败都安静返回 None（不影响回合交付）。
# 函数用途: 在目标回合正常结束时推进会话任务并把结构化结果回报给发送方。
def close_out_turn(agent: object, turn_id: str, outcome: TaskTurnOutcome) -> object | None:
    updated = finish_task_for_turn(agent, turn_id, outcome)
    if updated is None:
        return None
    report_task_result(agent, updated)
    return updated


# LLM: 回合终态的判定只看结构化字段，不解析正文；异常与中断都算失败，正常结束才算成功。
# 函数用途: 把回合的结束原因映射成任务终态。
def outcome_from_error(error: object, *, interrupted: bool = False) -> bool:
    if interrupted:
        return False
    return not bool(error)


__all__ = [
    "TaskTurnOutcome",
    "close_out_turn",
    "finish_task_for_turn",
    "outcome_from_error",
    "report_task_result",
    "task_for_turn",
]
