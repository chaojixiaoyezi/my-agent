# LLM: 会话间派活正常结束时的自动回报。判定只用结构化字段：
#   目标回合的 request id（来自 request.task_id）→ 在 SessionTaskStore 里按 conversation_request_id
#   找回它属于哪条任务 → 只在"当前不是终态"且"这次回合确实结束"时推进到 done/failed，并把
#   结构化结果（状态、产物 refs、摘要）作为一条来源明确的消息投回发送方的 guidance 队列。
#   幂等：状态机只允许 accepted → done|failed；委派终态只重放同task/status结果，guidance与wake共用原dedupe key。
#   普通回报仍是附加动作；IM委派回报失败由runtime保原执行wake待补账，typed来源错误原样传播。
# 模块用途: 把目标任务回合的正常结束变成发送方可见的结构化回报。
from __future__ import annotations

import time
from dataclasses import dataclass

from ..backends.bounded_call import BoundedCallExitUnconfirmedError
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from ..common.tool_write_context import ToolWriteContextUnavailable
from .business_observability import public_result_refs
from .session_messaging import SESSION_TASK_STATUS_FIELD
from .session_tasks import (
    SESSION_TASK_DONE,
    SESSION_TASK_FAILED,
    SESSION_TASK_TERMINAL_STATUSES,
    SessionTaskUpdate,
)

# 回报正文只写结构化事实；来源用与取消通知相同的 session_task 标记，注入时按宿主事件呈现。
_RESULT_ORIGIN_KIND = "session_task"


# LLM: 同一task/status结果沿唯一旧键去重；纯公式不读账、不签权，producer和主sender复核共用。
# 函数用途: 保持原终态回报身份，重试不生成第二条guidance或wake。
def result_report_key(task):
    return f"session_task_result:{task.task_id}:{task.status}"


def _business_sender_receives_wakes(source: object) -> bool:
    """只有通道会话或显式后台身份主动接收业务回报，普通线程保留 next_turn 投递。"""
    from .background_team_lifecycle import background_identity

    return bool(source.channel_bindings or source.metadata.get("business_controller") is True
                or background_identity(source) is not None)


# LLM: 命中条件全部是结构化事实：任务记录里绑定的 request id 必须与本次回合一致，且任务尚未到终态。
#   同一回合只会有唯一一条任务记录（bind_turn 拒绝改绑），所以不会误报别的任务。
# 函数用途: 按准确回合找会话任务；显式终态读取仅供原委派结果补账，不复活执行。
def task_for_turn(agent: object, turn_id: str, *, include_terminal: bool = False) -> object | None:
    bound_turn = str(turn_id or "").strip()
    if not bound_turn:
        return None
    store = getattr(agent, "conversation_store", None)
    tasks = getattr(store, "session_tasks", None)
    if tasks is None:
        return None
    # 原生派活以canonical TaskID作turnID；先按精确主键读，不为每次结果补账扫描Owner历史。
    load = getattr(tasks, 'load', None)
    if callable(load):
        try:
            direct = load(bound_turn)
        except (OSError, ValueError, TypeError):
            return None
        if direct is not None and direct.conversation_request_id == bound_turn:
            if not include_terminal and direct.status in SESSION_TASK_TERMINAL_STATUSES:
                return None
            return direct
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
        if not include_terminal and str(getattr(task, "status", "") or "") in SESSION_TASK_TERMINAL_STATUSES:
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
    # 与 result_refs 同一批交付项的稳定身份（path/artifact_id/sha256/size_bytes）；公开投影用它，不用路径。
    result_artifacts: tuple[dict, ...] = ()
    delivery_artifact_diagnostics: tuple[dict, ...] = ()


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
    # 仅已完成原TaskLink才可发表受管订单终态，避免等待片占用唯一结果回执。
    # 复用已解析的原任务，不额外扫描历史，也不由模型回复判断是否还有工作。
    if outcome.ok and isinstance((getattr(task, "metadata", None) or {}).get("business_context"), dict):
        link, error = agent.conversation_store.tasks.link_report(task.target_thread_id, task.task_id)
        if error is not None or link is None or link.status != "completed":
            return None
    target_status = SESSION_TASK_DONE if outcome.ok else SESSION_TASK_FAILED
    try:
        updated = tasks.advance(
            str(getattr(task, "task_id", "") or ""),
            SessionTaskUpdate(
                status=target_status,
                summary=str(outcome.summary or ""),
                result_refs=tuple(outcome.result_refs or ()),
                result_artifacts=tuple(outcome.result_artifacts or ()),
                delivery_artifact_diagnostics=tuple(outcome.delivery_artifact_diagnostics or ()),
            ),
        )
    except (OSError, ValueError, TypeError):
        return None
    return updated


# LLM: 回报是发送方可见的结构化消息：走 guidance 幂等入队，dedupe_key 用任务 id + 终态，
#   重复收口不会写第二条。投递失败不改变任务已到终态的事实（不回滚状态机）。metadata 带 SESSION_TASK_STATUS_FIELD：
#   认领判定靠它把回报和派活正文区分开（正文只许它自己的派活回合认领，回报发送方哪一回合都能认领），不能去掉。任务带宿主判定的
#   failure_code 时，metadata 记 session_task_failure_code（只在非空时出现，旧回报的 metadata 形状不变）。
# 函数用途: 把真实终态幂等回报原sender；IM委派补同key原wake，主sender模型发通知，重复补账不再记配对额度。
def report_task_result(agent: object, task: object) -> bool:
    task_id = str(getattr(task, "task_id", "") or "").strip()
    status = str(getattr(task, "status", "") or "").strip()
    sender = str(getattr(task, "sender_thread_id", "") or "").strip()
    if not task_id or not status or not sender:
        return False
    store = getattr(agent, "conversation_store", None)
    from .execution_threads import IM_TASK_EXECUTION_KEY, execution_origin_thread

    delegated = False
    threads = getattr(store, "threads", None)
    if threads is not None:
        target, error = threads.load_report(str(getattr(task, "target_thread_id", "") or ""))
        if error:
            return False
        if target is not None and IM_TASK_EXECUTION_KEY in target.metadata:
            try:
                source = execution_origin_thread(threads.require, target)
            except (ValueError, OSError, RuntimeError):
                return False
            if source.thread_id != sender:
                return False
            from .im_delegation_delivery import delegated_delivery_task

            canonical = delegated_delivery_task(agent, target.thread_id)
            if canonical != task or canonical.status not in {SESSION_TASK_DONE, SESSION_TASK_FAILED}:
                return False
            delegated = True
    guidance = getattr(store, "guidance", None)
    if guidance is None:
        return False
    # 业务上下文不代表来源线程可主动续跑；有真实接收路径时才沿委派同一 wake 形状与去重键唤醒。
    business = not delegated and isinstance((getattr(task, "metadata", None) or {}).get("business_context"), dict)
    summary = str(getattr(task, "summary", "") or "").strip()
    refs = tuple(str(item) for item in (getattr(task, "result_refs", ()) or ()))
    failure_code = str(getattr(task, "failure_code", "") or "").strip()
    lines = [
        f"你派出的任务 {task_id} 已结束：状态 {status}。",
    ]
    if failure_code:
        lines.append(f"宿主判定的失败原因码：{failure_code}")
    if summary:
        lines.append(f"摘要：{summary}")
    if refs:
        lines.append("产物：" + "、".join(refs))
    lines.append("这条消息由宿主自动发出，是结构化结果，不是目标会话的原话。")
    dedupe_key = result_report_key(task)
    target_thread_id = str(getattr(task, "target_thread_id", "") or "")
    try:
        # 同一任务的检查、发布与计数沿原task责任门串行；只有本次新建了回报回执才计一次会话对额度，
        #   重放、并发重入与部分提交后的补账都不再计（回执是唯一权威，计数写失败只影响节流精度）。
        with _report_guard(store, target_thread_id, task_id, blocking=not delegated):
            return _publish_report(store, guidance, threads, task, sender, lines, dedupe_key,
                                   delegated=delegated, business=business, failure_code=failure_code)
    except (RequestExecutionContextUnavailable, RequestExecutionOutcomeUncertainError,
            BoundedCallExitUnconfirmedError, ToolWriteContextUnavailable):
        raise
    except Exception:  # noqa: BLE001 - 普通失败由委派caller保原wake重试，非委派仍为附加报告。
        return False


# LLM: 原发布正文：guidance 幂等入队，按原规则补 wake；只在本次新建回执时计会话对额度。调用方已持准确 task 责任门。
# 函数用途: 发布一条终态结果回报并只为新发布计数，返回是否已发布或已在账。
def _publish_report(store, guidance, threads, task, sender, lines, dedupe_key, *, delegated, business, failure_code):
    task_id = str(getattr(task, "task_id", "") or "")
    status = str(getattr(task, "status", "") or "")
    previous = guidance.receipt(dedupe_key)
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
                SESSION_TASK_STATUS_FIELD: status,
                **({"session_task_failure_code": failure_code} if failure_code else {}),
            },
        },
        dedupe_key=dedupe_key,
        **({"blocking": False} if delegated else {}),
    )
    if previous is None:
        _record_pair(store, task.target_thread_id, sender)
    if delegated or business:
        from .session_messaging import SESSION_MESSAGE_KEY_FIELD

        source = threads.require(sender)
        if delegated and source.status != "active":
            return False
    if (delegated or business) and source.status == "active" and (
        delegated or _business_sender_receives_wakes(source)
    ):
        store.wakes.raise_signal({
            "thread_id": sender, "urgency": "normal", "reason": "session_message",
            "summary": "收到原委派任务的终态结果", "dedupe_key": dedupe_key, "retain_handled": True,
            "metadata": {"origin_kind": "session_message", "origin_thread_id": task.target_thread_id,
                         SESSION_MESSAGE_KEY_FIELD: dedupe_key},
        }, blocking=False)
    return True


# LLM: 复用原 wake 账本的准确 task 责任门（同线程同身份可重入）；没有 wake 账本的旧/测试 Store 不加锁，仍走原幂等键。
# 函数用途: 串行化同一任务的回报检查、发布与计数。
def _report_guard(store, target_thread_id, task_id, *, blocking):
    from contextlib import nullcontext

    guard = getattr(getattr(store, "wakes", None), "task_responsibility_guard", None)
    if not callable(guard) or not target_thread_id or not task_id:
        return nullcontext()
    return guard(target_thread_id, task_id, blocking=blocking)


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


# LLM: 只读原终态/结果回执/同key wake完整发布，不将guidance consumed当业务再次执行资格；无新扫描或存储。
# 函数用途: 判断已结束委派是否只欠原sender回报，供原claim准入保留纯补账路径。
def delegated_result_report_pending(agent, thread_id, *, task_id=""):
    from .im_delegation_delivery import delegated_delivery_task

    task = delegated_delivery_task(agent, thread_id, task_id=task_id)
    if task is None or task.status not in {SESSION_TASK_DONE, SESSION_TASK_FAILED}:
        return False
    key = result_report_key(task)
    return (agent.conversation_store.guidance.receipt(key) is None
            or agent.conversation_store.wakes.delivery_receipt(task.sender_thread_id, key) not in {"pending", "handled"})


# LLM: canonical终态是唯一结果来源；只重放同task/status摘要与原refs，不调用模型、改状态、创建孩子或清P3。失败留原wake待下次claim。
# 函数用途: 在真实后台执行入口修终态后guidance/wake部分提交，返回原结果供内部留档。
def recover_delegated_result(agent, thread_id, *, task_id=""):
    from .background_execution import BackgroundCapacityDeferred, BackgroundExecutionResult
    from .im_delegation_delivery import delegated_delivery_task

    task = delegated_delivery_task(agent, thread_id, task_id=task_id)
    if task is None or task.status not in {SESSION_TASK_DONE, SESSION_TASK_FAILED}:
        return None
    if not report_task_result(agent, task):
        raise BackgroundCapacityDeferred('delegated_result_report_pending')
    return BackgroundExecutionResult(response=task.summary, tool_call_count=0, tool_success_count=0,
        material_progress_count=0, delivery_artifacts=(), message_tool_deliveries=(),
        operation_verification={}, assistant_commentaries=(), display_snapshot={},
        runtime_status="ok" if task.status == SESSION_TASK_DONE else "failed")


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
    "public_result_refs",
    "report_task_result",
    "task_for_turn",
]
