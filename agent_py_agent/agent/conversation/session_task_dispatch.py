# LLM: 会话任务派发的唯一副作用入口；模型工具与宿主 IM 共用原 SessionTask/guidance/wake 和配额，不依赖 agent_core 或 Gateway。
# 模块用途: 按已授权的结构化计划耐久派发任务，保留原幂等、链深、正文引用与失败回执。
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..runtime_errors import DataCorruptionError, is_structured_error_code, runtime_error_report
from ..tooling.models import ToolHandlerOutcome
from .session_messaging import (
    SESSION_IDENTITY_UNAVAILABLE,
    SESSION_TASK_CHAIN_LIMIT,
    SESSION_TASK_ORIGIN_KIND,
    SESSION_TASK_RATE_LIMIT,
    SESSION_TASK_STATUS_FIELD,
)
from .session_tasks import SessionTaskDraft

SESSION_TASK_TOOL_NAME = "create_session_task"


class SessionTaskDispatchError(RuntimeError):
    """只运输原派发回执；异常正文不提供任务身份或副作用状态。"""

    def __init__(self, outcome: ToolHandlerOutcome) -> None:
        self.outcome = outcome
        self.error_code = outcome.error_code
        self.effect_outcome = outcome.effect_outcome
        # 仅运输派发口显式的安全标量，不把原参数或内部路径带到业务错误。
        try:
            details = json.loads(outcome.output).get("details", {})
        except (ValueError, TypeError, AttributeError):
            details = {}
        self.details = {key: details[key] for key in ("exception_type", "cause_code", "stage")
            if isinstance(details, dict) and isinstance(details.get(key), str)}
        super().__init__(outcome.error_code)


# LLM: 只读原ConversationStore；已知ID核canonical，无ID必须读完原分页并拒绝冲突，不产生派发副作用。
# 函数用途: 找回原任务，只有完整权威记录证明没有任务时才返回空。
def reconcile_session_task_dispatch(
    store: object, *, owner_id: str, sender_thread_id: str, target_thread_id: str,
    dedupe_key: str, task_id: str | None = None,
) -> object | None:
    """只读核对原canonical任务；索引缺失/损坏不能证明任务不存在。"""
    from .background_team_lifecycle import SessionTaskCoverageUnknown, session_task_pages

    identity = {"sender_thread_id": sender_thread_id, "target_thread_id": target_thread_id, "dedupe_key": dedupe_key}
    if task_id is not None:
        identity["task_id"] = task_id
    if any(type(value) is not str or not value.strip() for value in (owner_id, *identity.values())):
        raise PermissionError("SESSION_TASK_DISPATCH_IDENTITY_MISMATCH")
    try:
        for thread_id in (sender_thread_id, target_thread_id):
            if store.threads.require(thread_id).owner_id != owner_id:
                raise PermissionError("SESSION_TASK_DISPATCH_OWNER_MISMATCH")
        tasks = store.session_tasks
        # 已知ID优先直接读canonical；不因无关或损坏的查找索引丢失准确身份。
        known = tasks.load(task_id) if task_id is not None else None
        if known is not None:
            return _checked_dispatch_task(known, owner_id, identity)
        # 无ID时索引命中也不能证明唯一性；完整canonical链必须排除同键的第二条责任。
        matched = None
        for items in session_task_pages(tasks):
            for task in items:
                if task.dedupe_key == dedupe_key or task_id is not None and task.task_id == task_id:
                    checked = _checked_dispatch_task(task, owner_id, identity)
                    if matched is not None and matched.task_id != checked.task_id:
                        raise PermissionError("SESSION_TASK_DISPATCH_IDENTITY_CONFLICT")
                    matched = checked
        return matched
    except PermissionError:
        raise
    except (OSError, ValueError, TypeError, KeyError, SessionTaskCoverageUnknown) as exc:
        raise SessionTaskDispatchError(session_task_dispatch_error(
            "原派发结果尚未能完整核实。", error_code="TOOL_EXECUTION_FAILED", effect_outcome="unknown",
            session_task_dispatch=identity)) from exc


def _checked_dispatch_task(task, owner_id, identity):
    from .business_contracts import BusinessContextIntent, BusinessExecutionContext

    if (any(getattr(task, name) != identity[name] for name in ("sender_thread_id", "target_thread_id", "dedupe_key"))
            or "task_id" in identity and task.task_id != identity["task_id"]):
        raise PermissionError("SESSION_TASK_DISPATCH_IDENTITY_MISMATCH")
    raw_context = task.metadata.get("business_context")
    raw_intent = task.metadata.get("business_context_intent")
    if raw_context is not None or raw_intent is not None:
        try:
            context = BusinessExecutionContext.from_dict(raw_context)
            intent = BusinessContextIntent.from_dict(raw_intent)
        except (TypeError, ValueError, KeyError) as exc:
            raise PermissionError("SESSION_TASK_DISPATCH_BUSINESS_MISMATCH") from exc
        if (context.owner_id != owner_id or intent.owner_id != owner_id or context.task_id != task.task_id
                or not context.space_id or intent.requested_space_id not in (None, context.space_id)
                or intent.project_ref != context.project_ref or intent.binding_id != context.binding_id):
            raise PermissionError("SESSION_TASK_DISPATCH_BUSINESS_MISMATCH")
    return task


# LLM: 每对会话每小时限额（与发消息共用同一计数与配置键）；0 表示不限制，没有计数器时放行。
#   在真正建记录前判断，避免被拒的任务占用配额。
# 函数用途: 判断这次派活是否超出每对会话限额，超出时返回结构化失败。
def session_task_pair_limit_error(
    agent: object, sender_thread_id: str, target_thread_id: str, config: object
) -> ToolHandlerOutcome | None:
    from .session_pair_rate import pair_limit_reached

    limit = int(config.session_pair_hourly_limit or 0)
    store = getattr(agent, "conversation_store", None)
    if not pair_limit_reached(store, sender_thread_id, target_thread_id, limit):
        return None
    return session_task_dispatch_error(
        f"这一对会话每小时的条数已达上限（{limit}）；任务没有派发。",
        error_code=SESSION_TASK_RATE_LIMIT,
        details={"limit": limit, "sender_thread_id": sender_thread_id, "target_thread_id": target_thread_id},
    )



# LLM: 链深按既有 SessionTask 的 origin_task_id 结构化计算；本工具不接收模型给的深度参数。
#   超过配置上限拒绝 SESSION_TASK_CHAIN_LIMIT；0 表示不限制。
# 函数用途: 校验这次派活是否超出派活链深度上限。
def _chain_limit_error(store: object, origin_task_id: str, config: object) -> ToolHandlerOutcome | None:
    limit = int(config.session_task_max_chain_depth or 0)
    if limit <= 0:
        return None
    parent = None
    if origin_task_id:
        try:
            parent = store.session_tasks.load(origin_task_id)
        except (ValueError, TypeError):
            parent = None
    if parent is None:
        return None
    depth = store.session_tasks.chain_depth(parent) + 1
    if depth >= limit:
        return session_task_dispatch_error(
            f"派活链深度达到上限（{limit}）；任务没有派发。",
            error_code=SESSION_TASK_CHAIN_LIMIT,
            details={"depth": depth, "limit": limit},
        )
    return None

# LLM: 派发身份来自宿主已授权上下文；正文仅作为原 guidance 输入，不决定权限、状态或链深。
# 类用途: 保存模型工具与宿主后台任务共用的一次派发计划，不执行 IO。
@dataclass(frozen=True)
class SessionTaskDispatchPlan:
    sender_thread_id: str
    target_thread_id: str
    goal: str
    origin_task_id: str = ""
    idempotency_key: str = ""



# LLM: 宿主自动派工只允许准确来源下的同 owner 独立线程，不开放模型跨会话管理员权限；沿原记录/正文/唤醒及配额。
# 函数用途: 将冻结IM工作项及typed原输入送入同owner后台线程，重放必须同引用快照，不再唤醒或扣额。
def dispatch_owned_background_task(agent, *, source_thread_id: str, target_thread, goal: str, config, host_message_context=None,
                                   business_context_intent=None, business_context_factory=None,
                                   business_context_validator=None):
    from .execution_threads import EXECUTION_ORIGIN_KEY, execution_origin_thread
    from .session_tasks import SESSION_TASK_TERMINAL_STATUSES

    source = execution_origin_thread(agent.conversation_store.threads.require, target_thread)
    if (source.thread_id != source_thread_id or target_thread.owner_id != str(agent.home_paths.owner_id)
            or target_thread.metadata[EXECUTION_ORIGIN_KEY]["kind"] != "delegated"):
        return session_task_dispatch_error("后台派工身份不一致。", error_code=SESSION_IDENTITY_UNAVAILABLE)
    plan = SessionTaskDispatchPlan(source_thread_id, target_thread.thread_id, goal)
    previous = agent.conversation_store.session_tasks.find_by_dedupe_key(_task_dedupe_key(plan))
    from .host_message_context import CONTEXT_KEY, HostMessageContext

    if host_message_context is not None:
        actor = host_message_context.actor if isinstance(host_message_context, HostMessageContext) else None
        origin = target_thread.metadata[EXECUTION_ORIGIN_KEY]
        if (actor is None or actor.owner_id != str(agent.home_paths.owner_id) or actor.source_thread_id != source_thread_id
                or actor.entry_request_id != origin.get('operation_key') or actor.binding() != origin.get('binding')):
            return session_task_dispatch_error('原派活引用身份不一致。', error_code=SESSION_IDENTITY_UNAVAILABLE)
    snapshot = host_message_context.to_record() if host_message_context is not None else None
    if previous is not None and previous.metadata.get(CONTEXT_KEY) != snapshot:
        return session_task_dispatch_error('原派活引用输入冲突。', error_code=SESSION_IDENTITY_UNAVAILABLE)
    if (previous is not None and previous.metadata.get('business_context_intent') is not None
            and business_context_intent is None):
        return session_task_dispatch_error('原派活受理空间快照缺失。', error_code=SESSION_IDENTITY_UNAVAILABLE)
    if business_context_intent is not None and previous is not None:
        from .business_contracts import BusinessContextIntent, BusinessExecutionContext

        original = BusinessContextIntent.from_dict(previous.metadata.get('business_context_intent'))
        if (original.owner_id != business_context_intent.owner_id
                or original.requested_space_id != business_context_intent.requested_space_id):
            return session_task_dispatch_error('原派活空间意图冲突。', error_code=SESSION_IDENTITY_UNAVAILABLE)
        if not callable(business_context_validator):
            return session_task_dispatch_error('原派活空间缺少权限复核。', error_code=SESSION_IDENTITY_UNAVAILABLE)
        business_context_validator(BusinessExecutionContext.from_dict(previous.metadata.get('business_context')))
        business_context_intent = original
    if previous is not None and previous.status in SESSION_TASK_TERMINAL_STATUSES:
        return ToolHandlerOutcome(SESSION_TASK_TOOL_NAME, True, json.dumps({"task_id": previous.task_id, "status": previous.status}))
    if previous is None and (error := session_task_pair_limit_error(agent, source_thread_id, target_thread.thread_id, config)):
        return error
    return dispatch_session_task(agent, plan, target_thread, config, count_pair=previous is None,
                                 host_message_context=host_message_context,
                                 business_context_intent=business_context_intent,
                                 business_context_factory=business_context_factory)



# LLM: 建记录 → 投正文（guidance，幂等）→ 目标 active 时唤醒。正文只存一份，记录里只留它的 id。
#   记录先于正文写入，绑定准确 guidance id 后发布唤醒；只有显式委派目标需要写线程策略引用。
# 函数用途: 在原记录冻结typed输入位置并投递正文；引用不进guidance正文/模型attrs，唤醒仍沿原队列。
def dispatch_session_task(
    agent: object,
    plan: SessionTaskDispatchPlan,
    target_thread: object,
    config: object,
    *, count_pair: bool = True, host_message_context=None, host_channel_actor=None,
    business_context_intent=None, business_context_factory=None,
) -> ToolHandlerOutcome:
    store = agent.conversation_store
    if host_channel_actor is not None:
        from .business_access import BusinessError
        from .host_actor import HostActorError, HostChannelActor, _validate_scope

        try:
            from pathlib import Path

            home = Path(agent.home_paths.owner_home_dir).resolve()
            for identifier in (plan.sender_thread_id, plan.target_thread_id):
                thread = store.threads.require(identifier)
                if thread.owner_id != str(agent.home_paths.owner_id) or Path(thread.owner_home).resolve() != home:
                    raise HostActorError("CHANNEL_ACTOR_SCOPE_MISMATCH")
            if (not isinstance(host_channel_actor, HostChannelActor)
                    or host_channel_actor.owner_id != str(agent.home_paths.owner_id)
                    or host_channel_actor.source_thread_id != plan.sender_thread_id
                    or target_thread.thread_id != plan.target_thread_id):
                raise HostActorError("CHANNEL_ACTOR_SCOPE_MISMATCH")
            _validate_scope(agent, plan.target_thread_id, host_channel_actor)
            previous = store.session_tasks.find_by_dedupe_key(_task_dedupe_key(plan))
            if previous is not None:
                link, error = store.tasks.load_report(previous.task_id)
                if error and error.get('error_type') != 'FileNotFoundError':
                    raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
                if link is not None:
                    if link.thread_id != plan.target_thread_id:
                        raise HostActorError("CHANNEL_ACTOR_SCOPE_MISMATCH")
                    if link.host_channel_actor is not None:
                        frozen = HostChannelActor.from_record(link.host_channel_actor)
                        if (frozen.owner_id != host_channel_actor.owner_id
                                or frozen.source_thread_id != host_channel_actor.source_thread_id
                                or frozen.binding() != host_channel_actor.binding()):
                            raise HostActorError("CHANNEL_ACTOR_IDENTITY_CONFLICT")
                        _validate_scope(agent, plan.target_thread_id, frozen)
        except (HostActorError, DataCorruptionError, KeyError, TypeError, ValueError) as error:
            raise BusinessError("CAPABILITY_DENIED", effect_outcome="not_started", details={
                "reason": getattr(error, 'error_code', 'CHANNEL_ACTOR_IDENTITY_INVALID'),
                "side_effect_state": "not_started", "task_created": False,
                "next_action": "从原发送会话核对当前发起人与目标的可信绑定；不得通过模型参数提供宿主身份。"}) from None
    if limit_error := _chain_limit_error(store, plan.origin_task_id, config):
        return limit_error

    dedupe_key = _task_dedupe_key(plan)
    body_dedupe_key = f"body:{dedupe_key}"
    task = None
    try:
        # 记录先建（拿到任务编号），正文再带着自己的任务编号入队：正文写入时就知道它属于哪条任务，
        # 接收回合据此只认领自己的那条。记录里仍只引用正文 id，正文只存一份。
        task = store.session_tasks.create(
            SessionTaskDraft(
                sender_thread_id=plan.sender_thread_id,
                target_thread_id=plan.target_thread_id,
                goal=plan.goal,
                body_guidance_id="",
                body_dedupe_key=body_dedupe_key,
                dedupe_key=dedupe_key,
                origin_task_id=plan.origin_task_id,
            ), host_message_context=host_message_context,
            **({"business_context_intent": business_context_intent,
                "business_context_factory": business_context_factory}
               if business_context_intent is not None or business_context_factory is not None else {}),
        )
        from .session_tasks import SESSION_TASK_TERMINAL_STATUSES

        # 终态重放不补actor、改策略或发布正文；原失败/取消保持原对象，不因修复而复活。
        if task.status in SESSION_TASK_TERMINAL_STATUSES:
            return ToolHandlerOutcome(SESSION_TASK_TOOL_NAME, True, json.dumps({
                "task_id": task.task_id, "status": task.status, "target_thread_id": task.target_thread_id}))
        if host_channel_actor is not None:
            # canonical任务已成交，正文/wake尚未发布；原TaskLink冻结宿主actor，重放只保留原entry身份。
            from .host_actor import HostActorError, HostChannelActor

            link, error = store.tasks.load_report(task.task_id)
            if error and error.get('error_type') != 'FileNotFoundError':
                raise HostActorError("CHANNEL_ACTOR_IDENTITY_INVALID")
            frozen_actor = host_channel_actor
            if link is not None:
                if link.thread_id != plan.target_thread_id:
                    raise HostActorError("CHANNEL_ACTOR_SCOPE_MISMATCH")
                if link.host_channel_actor is not None:
                    frozen_actor = HostChannelActor.from_record(link.host_channel_actor)
                    if (frozen_actor.owner_id != host_channel_actor.owner_id
                            or frozen_actor.source_thread_id != host_channel_actor.source_thread_id
                            or frozen_actor.binding() != host_channel_actor.binding()):
                        raise HostActorError("CHANNEL_ACTOR_IDENTITY_CONFLICT")
            store.tasks.bind({'task_id': task.task_id, 'thread_id': plan.target_thread_id, 'goal': plan.goal},
                             host_channel_actor=frozen_actor)
        from .execution_threads import IM_TASK_EXECUTION_KEY, bind_delegated_task

        # 普通会话没有宿主委派策略，不需要线程策略写入；模型派活仍沿原正文/唤醒流程。
        if IM_TASK_EXECUTION_KEY in (getattr(target_thread, "metadata", None) or {}):
            bind_delegated_task(store, plan.target_thread_id, task.task_id)
        body = _queue_body(store, plan, body_dedupe_key, task.task_id)
        bound = store.session_tasks.bind_body(task.task_id, body.guidance_id)
        if bound is not None:
            task = bound
        if task.status in SESSION_TASK_TERMINAL_STATUSES:
            # 绑定后的最新记录已终态（如入队前/绑定中提交的取消）：不再发布唤醒，只结清这条迟到正文或保留其责任。
            _settle_late_task_body(agent, task, body)
            wake_id = ""
        else:
            wake_id = _maybe_wake(store, target_thread, plan.sender_thread_id, task.task_id)
    except Exception as exc:  # noqa: BLE001
        cause_code = getattr(exc, "code", "") or getattr(exc, "error_code", "")
        identity = {"sender_thread_id": plan.sender_thread_id, "target_thread_id": plan.target_thread_id,
                    "dedupe_key": dedupe_key}
        if task is not None:
            identity["task_id"] = task.task_id
        return session_task_dispatch_error(
            "派发回执未知；可能已耐久受理，须按原身份对账。",
            error_code="TOOL_EXECUTION_FAILED",
            details={**runtime_error_report(exc, context="create_session_task.dispatch"),
                "exception_type": type(exc).__name__,
                "cause_code": cause_code if is_structured_error_code(cause_code) else "",
                "stage": "create_task" if task is None else "queue_body_bind_or_wake"},
            effect_outcome="unknown",
            session_task_dispatch=identity,
        )
    # 派发成功后计入每对会话配额（与消息、回报共用同一计数）。
    if count_pair:
        _record_pair(store, plan.sender_thread_id, plan.target_thread_id)
    payload = {
        "ok": True,
        "task_id": task.task_id,
        "status": task.status,
        "target_thread_id": plan.target_thread_id,
        "body_guidance_id": task.body_guidance_id,
        "wake_signal_id": wake_id,
        "message": (
            "任务已耐久排队并记录；这不代表目标已经接受或执行。目标空闲会被唤醒，"
            "忙碌则在下一个回合边界注入；结束时会把结果作为消息回给你。"
        ),
    }
    return ToolHandlerOutcome(SESSION_TASK_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False, indent=2))



# LLM: 计数写失败只影响节流精度，不影响已派发的任务；吞掉异常。
# 函数用途: 把一次已派发的任务计入每对会话配额。
def _record_pair(store: object, sender_thread_id: str, target_thread_id: str) -> None:
    from .session_pair_rate import record_pair_message

    record_pair_message(store, sender_thread_id, target_thread_id)



# LLM: 幂等键要稳定、可重试，又不能把任务正文抄一份进去（"正文只存一份"）。目标、发送方和
#   正文本就是任务身份；正文用摘要入键，键里不再出现原句，重试与内容变化都仍然可区分。
# 函数用途: 生成一条会话任务的稳定幂等键，键里不嵌入完整正文。
def _task_dedupe_key(plan: SessionTaskDispatchPlan) -> str:
    if plan.idempotency_key:
        digest = hashlib.sha256(plan.idempotency_key.encode("utf-8")).hexdigest()
        return f"session_task:{plan.sender_thread_id}->{plan.target_thread_id}:operation:{digest}"
    goal_digest = hashlib.sha256(str(plan.goal or "").encode("utf-8")).hexdigest()[:16]
    return f"session_task:{plan.sender_thread_id}->{plan.target_thread_id}:{goal_digest}"



# LLM: 正文只写原 guidance，任务账本只存该条引用；稳定队列键支持撤销与重放，不冒充用户原话。
# 函数用途: 幂等投递一条带准确 SessionTask 来源的工作正文。
def _queue_body(store: object, plan: SessionTaskDispatchPlan, body_dedupe_key: str, task_id: str) -> object:
    return store.guidance.append_once(
        {
            "target_type": "thread",
            "target_id": plan.target_thread_id,
            "message": plan.goal,
            "sender": plan.sender_thread_id or "session",
            "priority": "normal",
            "delivery": "next_turn",
            "metadata": {
                "origin_kind": SESSION_TASK_ORIGIN_KIND,
                # 正文自带它属于哪条任务：接收回合据此只认领自己的那条，不会把别人
                # （往往是更早的）正文抢来做完。
                "session_task_id": task_id,
                "origin_thread_id": plan.sender_thread_id,
            },
        },
        dedupe_key=body_dedupe_key,
    )



# LLM: 只认本次准确正文的原回执（同键、同条、同目标/发送方、同任务来源，未带派活终态或别的回合），pending经原回执
#   锁内CAS改rejected；读写失败、身份不符或已越过认领边界都不报结清。已取消的业务任务把自身引用并入原取消账
#   （原_save_cancellation_control并集写，不清既有责任），交给原取消重试收尾；不另建清理队列，不碰其它输入。
# 函数用途: 结清终态任务迟到的派活正文，结不清时保留原取消责任。
def _settle_late_task_body(agent: object, task: object, body: object) -> None:
    from ..runtime_errors import DataCorruptionError
    from .session_tasks import SESSION_TASK_CANCELLED

    store, key, settled = agent.conversation_store, task.body_dedupe_key, False
    try:
        receipt = store.guidance.receipt(key) if key else None
        entry = receipt.entry if receipt is not None else None
        metadata = (entry.metadata or {}) if entry is not None else {}
        if (entry is not None and receipt.dedupe_key == key and entry.guidance_id == body.guidance_id
                and entry.guidance_id == task.body_guidance_id and entry.target_type == "thread"
                and entry.target_id == task.target_thread_id and entry.sender == task.sender_thread_id
                and metadata.get("dedupe_key") == key and metadata.get("origin_kind") == SESSION_TASK_ORIGIN_KIND
                and metadata.get("origin_thread_id") == task.sender_thread_id
                and metadata.get("session_task_id") == task.task_id and not metadata.get(SESSION_TASK_STATUS_FIELD)
                and metadata.get("expected_turn_id", "") in ("", task.conversation_request_id)):
            if receipt.status == "pending":
                receipt = store.guidance.mark_status(key, "rejected")
            settled = receipt.status in {"consumed", "rejected"}
    except (OSError, ValueError, KeyError, DataCorruptionError):
        settled = False
    if settled or task.status != SESSION_TASK_CANCELLED or task.metadata.get("business_context") is None:
        return
    from .session_messaging import _cancellation_binding, _save_cancellation_control

    _save_cancellation_control(agent, _cancellation_binding(task), task.metadata.get("business_context"), [task.task_id])


# LLM: 目标仍 active 才唤醒；返回 wake id 供回执展示，不承诺目标已消费。
# 函数用途: 目标会话活跃时发起一次来源明确的派活唤醒。
def _maybe_wake(store: object, target_thread: object, sender_thread_id: str, task_id: str, *, generation: str = "") -> str:
    from .background_team_lifecycle import background_identity_allows_work

    if not background_identity_allows_work(target_thread):
        return ""
    if str(getattr(target_thread, "status", "") or "active").strip() != "active":
        return ""
    wakes = getattr(store, "wakes", None)
    if wakes is None:
        return ""
    signal = wakes.raise_signal(
        {
            "thread_id": str(getattr(target_thread, "thread_id", "") or ""),
            "dedupe_key": f"session_task:{task_id}:{generation or 'dispatch'}",
            "retain_handled": True,
            "urgency": "normal",
            "reason": "session_task",
            "summary": "收到来自另一个会话的任务",
            "metadata": {
                "origin_kind": SESSION_TASK_ORIGIN_KIND,
                "origin_thread_id": sender_thread_id,
                "session_task_id": task_id,
            },
        }
    )
    return str(getattr(signal, "wake_signal_id", "") or "")



# LLM: 所有失败必须带稳定 error_code；details 只承载结构化诊断。
# 函数用途: 统一生成 create_session_task 的机器可读失败。
def session_task_dispatch_error(
    message: str,
    *,
    error_code: str,
    details: dict[str, object] | None = None,
    effect_outcome: str = "not_started",
    session_task_dispatch: dict[str, str] | None = None,
) -> ToolHandlerOutcome:
    payload: dict[str, object] = {"ok": False, "error": "session_task_not_dispatched", "message": message}
    if details:
        payload["details"] = details
    return ToolHandlerOutcome(
        SESSION_TASK_TOOL_NAME, False, json.dumps(payload, ensure_ascii=False, indent=2),
        error_code=error_code, effect_outcome=effect_outcome,
        result_envelope={"session_task_dispatch": dict(session_task_dispatch)} if session_task_dispatch is not None else {},
    )
