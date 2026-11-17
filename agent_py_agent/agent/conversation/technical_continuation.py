"""宿主技术截断的有界续跑：回执与结束原因同存原任务，唤醒只引用回执。"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, replace

from ..gateway_parts.io import update_json_file_atomic
from ..turn_end import result_turn_end_reason

WAKE_REASON = "technical_cutoff_continue"
# 用户消息优先（S11）：定时执行的后台工作片在已完成的工具轮边界看到同会话有用户回合排队时，不再调用模型，
#   以宿主原因结束本片，同一定时执行由下面的续跑唤醒接着做；这条路不发技术截断回执，也不占技术续跑次数。
USER_INPUT_YIELD_REASON = "USER_INPUT_YIELD"
USER_INPUT_YIELD_SOURCE = "lane_yield"
USER_INPUT_YIELD_WAKE_REASON = "user_input_yield_continue"
# 续跑唤醒只复制原定时执行的结构化身份与计划正文；冻结投递缓存、批次等字段属于原唤醒，不复制。
_YIELD_CARRIED_METADATA = (
    "scheduler_run_id", "scheduler_job_id", "scheduler_trigger", "scheduler_skill_refs", "scheduler_prompt",
)
# 同一未完成任务遇到有进展的技术截断后允许连续唤醒的最大次数。
_MAX_CONTINUATION_COUNT = 3
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


def _max_continuation_count(agent: object | None = None, *, limit: int | None = None) -> int:
    raw = limit if limit is not None else getattr(
        getattr(agent, "config", None),
        "technical_cutoff_continuation_max_count",
        _MAX_CONTINUATION_COUNT,
    )
    if isinstance(raw, bool):
        return _MAX_CONTINUATION_COUNT
    try:
        parsed = int(_MAX_CONTINUATION_COUNT if raw is None else raw)
    except (TypeError, ValueError):
        return _MAX_CONTINUATION_COUNT
    return max(0, parsed)


def receipt_for(link, *, max_continuations: int | None = None) -> CutoffReceipt | None:
    """只接受完整当前schema；坏记录不得提供执行授权。"""
    value = getattr(link, "cutoff_continuation", None)
    if not isinstance(value, dict) or set(value) != set(CutoffReceipt.__dataclass_fields__):
        return None
    try:
        receipt = CutoffReceipt(**value)
        strings = [getattr(receipt, name) for name in CutoffReceipt.__dataclass_fields__ if name != "sequence"]
        sequence_too_large = (
            max_continuations is not None and max_continuations > 0 and receipt.sequence > max_continuations
        )
        if (any(not isinstance(item, str) for item in strings)
                or not all((receipt.owner_id, receipt.thread_id, receipt.task_id, receipt.user_message_id,
                            receipt.origin_request_id, receipt.run_id, receipt.attempt_id))
                or type(receipt.sequence) is not int or receipt.sequence < 0 or sequence_too_large
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

    max_count = _max_continuation_count(agent)

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
        if cutoff and progress > 0 and (max_count == 0 or count < max_count):
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
        limit_hint = f"达到 {max_count} 片上限或" if max_count > 0 else ""
        queue_host_notice(store, thread_id, host_notice("technical_cutoff", "CUTOFF_CONTINUATION_STOPPED",
            f"本次技术续跑已停止：{limit_hint}本片没有已确认的实际进展。请检查结果后决定下一步。",
            details={"task_id": task_id, "attempt_id": receipt.attempt_id, "sequence": receipt.sequence,
                     "reason": receipt.reason, "max_continuations": max_count}))
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


# LLM: 只对定时执行的主代理工作片生效（任务号就是定时执行号）；前台、子代理、技术续跑链和 Goal 片照旧跑完：
#   技术续跑链的回执会被新用户消息作废（_valid 比对 last_user_message_id），让出后没有可续的权威。
#   技术续跑开关是总开关；车道登记是进程内 O(1) 读取，不碰租约文件。
# 函数用途: 判断当前后台工作片是否应在本工具轮边界把会话车道让给排队的用户消息。
def yields_to_user_input(agent, params) -> bool:
    if (not _enabled(agent) or str(getattr(params, "source", "") or "") != "background_main_agent"
            or str(getattr(params, "context_scope", "") or "").strip().lower() == "task_local"):
        return False
    attrs = getattr(params, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    task_id = str(attrs.get("conversation_task_id") or "").strip()
    store = getattr(agent, "conversation_store", None)
    if store is None or not thread_id or not task_id or str(attrs.get("scheduler_run_id") or "").strip() != task_id:
        return False
    from .run_claim import user_input_turn_on_lane

    return user_input_turn_on_lane(store, thread_id)


# LLM: 让出片的正文为空、不交付；只在本片唤醒已确认、任务仍 active 时发表一次同任务续跑唤醒，去重键带被让出的唤醒号，
#   同一片重入不会重复发表。调用方须在定时收口判定后续工作之前调用：这条待处理唤醒就是会自己推进的后续工作，
#   收口据此把定时执行停在 waiting，而不是结算成受阻。
# 函数用途: 为让出车道的定时工作片发表续跑唤醒，返回是否发表。
def publish_user_input_yield(store, signal, report) -> bool:
    if (str(getattr(report, "runtime_reason", "") or "") != USER_INPUT_YIELD_REASON
            or getattr(report, "wake_handled", False) is not True
            or str(getattr(report, "task_status", "") or "").strip().lower() != "active"):
        return False
    task_id = str(getattr(signal, "root_task_id", "") or "").strip()
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    if not task_id or str(metadata.get("scheduler_run_id") or "").strip() != task_id:
        return False
    from .authority import CONVERSATION_REQUEST_ID_ATTR

    carried = {key: metadata[key] for key in _YIELD_CARRIED_METADATA if key in metadata}
    store.wakes.raise_signal({
        "thread_id": signal.thread_id, "root_task_id": task_id, "urgency": "normal",
        "reason": USER_INPUT_YIELD_WAKE_REASON, "summary": "用户消息处理完后，继续让出车道前的同一定时任务。",
        "dedupe_key": f"user-input-yield:{task_id}:{signal.wake_signal_id}",
        "metadata": {**carried, CONVERSATION_REQUEST_ID_ATTR: task_id,
                     "yielded_wake_signal_id": signal.wake_signal_id},
    })
    return True


# LLM: 续跑唤醒只认宿主写下的同一定时执行身份；原任务已结束、换代或换线程时作废，不复活。
# 函数用途: 判断一条用户让出续跑唤醒是否仍能接着原定时任务执行。
def user_input_yield_wake_valid(store, signal) -> bool:
    task_id = str(getattr(signal, "root_task_id", "") or "").strip()
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    if not task_id or str(metadata.get("scheduler_run_id") or "").strip() != task_id:
        return False
    link = store.tasks.load(task_id)
    return bool(link is not None and link.status == "active" and link.thread_id == signal.thread_id
                and not link.superseded_by_task_id)


# 线程元数据里记录“哪个定时执行正为用户回合让出车道”的键；只存任务编号，不存正文。
USER_INPUT_YIELD_PARKED_KEY = "user_input_yield.v1"


# LLM: 让出片仍持车道时写入，排队的用户回合拿到车道后才构造会话上下文，必然看到这条标记；停放中的定时任务
#   仍是 active，前台用户回合不能把它当成本轮任务接管（否则用户回合会替定时任务干活、收口它）。续跑开始时清除。
# 函数用途: 在会话线程上标记一个为用户回合让出车道、等待续跑的定时执行。
def mark_user_input_yield_parked(store, thread_id: str, task_id: str) -> bool:
    def update(current):
        if user_input_yield_parked_task_id(current) == task_id:
            return current
        metadata = dict(current.metadata or {})
        metadata[USER_INPUT_YIELD_PARKED_KEY] = {"task_id": task_id}
        return replace(current, metadata=metadata)

    return _update_thread(store, thread_id, update)


# LLM: 只清除同一任务的标记；别的任务后来写下的标记原样保留。清除失败不影响续跑，标记只对仍 active 的链接起作用。
# 函数用途: 续跑开始时撤下一个定时执行的让出标记。
def clear_user_input_yield_parked(store, thread_id: str, task_id: str) -> bool:
    def update(current):
        if user_input_yield_parked_task_id(current) != task_id:
            return current
        metadata = dict(current.metadata or {})
        metadata.pop(USER_INPUT_YIELD_PARKED_KEY, None)
        return replace(current, metadata=metadata)

    return _update_thread(store, thread_id, update)


# LLM: 标记只影响前台回合是否接管停放任务；写不进去时退回原有选择，不能让让出或续跑本身失败。
# 函数用途: 在线程锁内应用一次元数据更新，失败返回 False。
def _update_thread(store, thread_id: str, update) -> bool:
    if store is None or not thread_id:
        return False
    try:
        store.threads.update_atomic(thread_id, update)
    except Exception:  # noqa: BLE001 - 标记是接管保护的提示，失败时调用方记日志并照常继续。
        return False
    return True


# LLM: 只在 yields_to_user_input 为真之后、仍持车道时调用；标记写不进去就不让出，本片照旧跑完，
#   宁可用户多等一片，也不让用户回合接管停放中的定时任务。
# 函数用途: 让出车道前把本定时执行标记为停放，返回能否让出。
def park_for_user_input(agent, params) -> bool:
    attrs = getattr(params, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    task_id = str(attrs.get("conversation_task_id") or "").strip()
    if mark_user_input_yield_parked(getattr(agent, "conversation_store", None), thread_id, task_id):
        return True
    logging.getLogger(__name__).warning("USER_INPUT_YIELD_PARK_FAILED thread=%s task=%s", thread_id, task_id)
    return False


# 函数用途: 读出线程上正为用户回合停放的定时执行编号，没有时为空串。
def user_input_yield_parked_task_id(thread) -> str:
    metadata = getattr(thread, "metadata", None)
    entry = metadata.get(USER_INPUT_YIELD_PARKED_KEY) if isinstance(metadata, dict) else None
    return str(entry.get("task_id") or "").strip() if isinstance(entry, dict) else ""


# LLM: 停放标记只对仍 active 的任务链接生效；任务已停止、删除或结束而标记没撤下时，读方当它不存在，
#   不再挡住用户回合的历史与工作区选择。链接读不出不能证明任务已结束，按原标记处理（不接管，方向保守）。
# 函数用途: 读出线程上仍在停放的定时执行编号；任务链接不存在或已不是 active 时为空串。
def active_user_input_yield_parked_task_id(store, thread) -> str:
    task_id = user_input_yield_parked_task_id(thread)
    if not task_id:
        return ""
    try:
        link = store.tasks.load(task_id)
    except Exception:  # noqa: BLE001 - 读失败不是任务已结束的证据，保留原标记的保护。
        return task_id
    return task_id if link is not None and link.status == "active" else ""
