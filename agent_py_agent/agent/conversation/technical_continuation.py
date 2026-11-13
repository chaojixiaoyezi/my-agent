"""宿主技术截断的有界续跑：回执与结束原因同存原任务，唤醒只引用回执。"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..gateway_parts.io import update_json_file_atomic
from ..turn_end import result_turn_end_reason

WAKE_REASON = "technical_cutoff_continue"
_LIMIT = 3
_SOURCES = {
    "TOOL_ROUND_LIMIT_REACHED": ("tool_loop", "unfinished"),
    "MODEL_RESPONSE_TRUNCATED": ("tool_loop", "unfinished"),
    "TOOL_CALL_UNCLOSED": ("tool_protocol_adapter", "unfinished"),
    "CONTEXT_OVERFLOW": ("preflight", "context_overflow"),
}


@dataclass(frozen=True)
class CutoffReceipt:
    owner_id: str
    thread_id: str
    task_id: str
    user_message_id: str
    origin_request_id: str
    run_id: str
    attempt_id: str
    sequence: int
    reason: str
    turn_end_reason: str
    status: str
    goal_id: str = ""

    @property
    def key(self) -> str:
        return f"cutoff:{self.task_id}:{self.attempt_id}"


def receipt_for(link) -> CutoffReceipt | None:
    """只接受完整当前schema；坏记录不得提供执行授权。"""
    value = getattr(link, "cutoff_continuation", None)
    if not isinstance(value, dict) or set(value) != set(CutoffReceipt.__dataclass_fields__):
        return None
    try:
        receipt = CutoffReceipt(**value)
        strings = [getattr(receipt, name) for name in CutoffReceipt.__dataclass_fields__ if name != "sequence"]
        if (any(not isinstance(item, str) for item in strings)
                or not all((receipt.owner_id, receipt.thread_id, receipt.task_id, receipt.user_message_id,
                            receipt.origin_request_id, receipt.run_id, receipt.attempt_id))
                or type(receipt.sequence) is not int or not 0 <= receipt.sequence <= _LIMIT
                or receipt.status not in {"pending", "claimed", "stopped", "finished", "void"}):
            return None
        return receipt
    except (TypeError, ValueError):
        return None


def _enabled(agent) -> bool:
    return getattr(getattr(agent, "config", None), "technical_cutoff_continuation_enabled", False) is True


def _write(store, task_id, update, *, origin=None, publish=None):
    """原任务锁内先发表wake再提交回执；消费者共此锁，不能抢先执行。"""
    from .models import ThreadTaskLink

    result = None
    def apply(data):
        nonlocal result
        link = ThreadTaskLink.from_dict(data)
        if link.task_id != task_id:
            raise ValueError("CUTOFF_TASK_IDENTITY_MISMATCH")
        result = update(link)
        if result is not None and result.status == "pending" and publish is not None:
            publish(result)
        updated = {**data, "cutoff_continuation": asdict(result)} if result is not None else data
        if origin is not None and link.status == "active" and link.thread_id == origin["thread_id"]:
            updated = {**updated, "host_user_origin": origin}
        return updated

    with store.tasks.transition_guard(task_id):
        update_json_file_atomic(store.storage.task_path(task_id), apply, require_existing=True)
    return result


def record_closeout(agent, ctx) -> CutoffReceipt | None:
    """真实主代理收口同次存结束原因与回执；后台只承接原已授权链。"""
    if (not _enabled(agent) or ctx.context_scope == "task_local"
            or (not ctx.do_save and ctx.source != "background_main_agent")):
        return None
    attrs = ctx.task_attributes if isinstance(ctx.task_attributes, dict) else {}
    task_id, thread_id = str(attrs.get("conversation_task_id") or ""), str(attrs.get("conversation_thread_id") or "")
    store = getattr(agent, "conversation_store", None)
    owner = str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or "")
    if not all((store, task_id, thread_id, owner, ctx.run_id, ctx.attempt_id)):
        return None
    if store.tasks.load(task_id) is None:
        return None
    thread = store.threads.require(thread_id)
    response = ctx.final_response
    reason = str(getattr(response, "runtime_reason", "") or "").upper()
    pair = (str(getattr(response, "runtime_source", "") or ""), str(getattr(response, "runtime_status", "") or ""))
    end_reason = result_turn_end_reason(response)
    cutoff = _SOURCES.get(reason) == pair and end_reason not in {"aborted", "error", "blocked"}
    natural = result_turn_end_reason(response) == "completed" and not cutoff
    from .background_execution import material_tool_success_count
    progress = material_tool_success_count(agent, [row for row in ctx.archive_tool_calls or ()
        if isinstance(row, dict) and row.get("handler_executed") is True])
    goal = store.goals.load(thread_id, task_id=task_id)
    if goal is not None and goal.status != "active":
        return None
    from .models import SUBAGENT_LIFECYCLE_WAKE_REASONS
    wake_id = str(attrs.get("background_wake_signal_id") or "")
    wake = store.wakes.pending_one(wake_id) if wake_id else None
    child_event = bool(wake is not None and wake.thread_id == thread_id and wake.root_task_id == task_id
                       and wake.reason in SUBAGENT_LIFECYCLE_WAKE_REASONS)
    direct = (ctx.source in {"gateway", "chat", "run"}
              and ctx.request_id == thread.last_user_request_id and bool(ctx.request_id)
              and thread.owner_id == owner)
    origin = ({"owner_id": owner, "thread_id": thread_id, "task_id": task_id,
               "user_message_id": thread.last_user_message_id, "request_id": ctx.request_id}
              if direct else None)

    def update(link):
        old = receipt_for(link)
        if (link.thread_id != thread_id or link.status != "active" or link.superseded_by_task_id
                or link.work_kind == "audit"
                or thread.owner_id != owner or not thread.last_user_message_id):
            return None
        previous_origin = link.host_user_origin
        inherited_origin = (isinstance(previous_origin, dict) and set(previous_origin) ==
            {"owner_id", "thread_id", "task_id", "user_message_id", "request_id"}
            and previous_origin.get("owner_id") == owner and previous_origin.get("thread_id") == thread_id
            and previous_origin.get("task_id") == task_id
            and previous_origin.get("user_message_id") == thread.last_user_message_id
            and bool(previous_origin.get("request_id")))
        inherited = (ctx.source == "background_main_agent" and inherited_origin
                     and (old is None or old.status in {"claimed", "finished"} or child_event))
        if not direct and not inherited:
            return None
        if old is None and not cutoff:
            return None
        if old is not None and old.attempt_id == ctx.attempt_id:
            return old
        count = old.sequence if inherited and old is not None and not natural and not child_event else 0
        status = "finished" if natural else "stopped"
        if cutoff and progress > 0 and count < _LIMIT:
            status = "pending"
            count += 1
        return CutoffReceipt(owner, thread_id, task_id, thread.last_user_message_id,
            str(ctx.request_id) if direct else str(previous_origin["request_id"]),
            str(ctx.run_id), str(ctx.attempt_id), count, reason,
            result_turn_end_reason(response), status, str(getattr(goal, "goal_id", "") or ""))

    receipt = _write(store, task_id, update, origin=origin,
                     publish=lambda value: _publish_receipt(store, value))
    if receipt is not None and receipt.status == "stopped" and cutoff:
        from .host_notices import host_notice, queue_host_notice
        queue_host_notice(store, thread_id, host_notice("technical_cutoff", "CUTOFF_CONTINUATION_STOPPED",
            "本次技术续跑已停止：达到三片上限或本片没有已确认的实际进展。请检查结果后决定下一步。",
            details={"task_id": task_id, "attempt_id": receipt.attempt_id, "sequence": receipt.sequence,
                     "reason": receipt.reason}))
    return receipt


def _valid(agent, store, link, receipt):
    if not _enabled(agent) or receipt is None or receipt.status not in {"pending", "claimed"}:
        return False
    if receipt.owner_id != str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or ""):
        return False
    if (link.status != "active" or link.superseded_by_task_id or link.task_id != receipt.task_id
            or link.thread_id != receipt.thread_id):
        return False
    thread = store.threads.require(receipt.thread_id)
    if (thread.owner_id != receipt.owner_id or thread.status != "active"
            or thread.last_user_message_id != receipt.user_message_id):
        return False
    goal = store.goals.load(receipt.thread_id, task_id=receipt.task_id)
    return ((goal is None and not receipt.goal_id)
            or (goal is not None and goal.goal_id == receipt.goal_id and goal.status == "active"))


def publish_pending(agent, store, task_id) -> bool:
    """只从有效任务回执发表幂等wake；不创建Goal或定时策略。"""
    link = store.tasks.load(task_id)
    receipt = receipt_for(link)
    if not _valid(agent, store, link, receipt) or receipt.status != "pending":
        return False
    _publish_receipt(store, receipt)
    return True


def _publish_receipt(store, receipt):
    """唯一键沿原prepared/install发布；已处理的旧键不能写成新的pending回执。"""
    signal = store.wakes.raise_signal({"thread_id": receipt.thread_id, "root_task_id": receipt.task_id,
        "urgency": "normal", "reason": WAKE_REASON, "summary": "继续宿主技术截断的同一任务。",
        "dedupe_key": receipt.key, "retain_handled": True,
        "metadata": {"cutoff_receipt_key": receipt.key, "goal_id": receipt.goal_id}})
    if signal.status != "pending":
        raise ValueError("CUTOFF_WAKE_ALREADY_HANDLED: 禁止把已处理唤醒重新提交为待续跑回执")


def claim_wake(agent, store, signal) -> bool:
    """同一wake重试可恢复claim；取消、换代、新用户输入或伪造wake均不领取。"""
    if not signal.root_task_id or store.tasks.load(signal.root_task_id) is None:
        return False
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    def update(link):
        receipt = receipt_for(link)
        if (receipt is None or signal.reason != WAKE_REASON or signal.thread_id != receipt.thread_id
                or metadata.get("cutoff_receipt_key") != receipt.key):
            return None
        if not _valid(agent, store, link, receipt):
            return CutoffReceipt(**{**asdict(receipt), "status": "void"})
        return CutoffReceipt(**{**asdict(receipt), "status": "claimed"})
    result = _write(store, signal.root_task_id, update)
    return result is not None and result.status == "claimed"


def suppress_intermediate(store, *, task_id, run_id, attempt_id) -> bool:
    """交付只认本次精确执行的pending技术回执，不消费用户回复义务。"""
    receipt = receipt_for(store.tasks.load(task_id))
    return bool(receipt and receipt.status == "pending" and receipt.run_id == run_id
                and receipt.attempt_id == attempt_id and receipt.reason in _SOURCES)
