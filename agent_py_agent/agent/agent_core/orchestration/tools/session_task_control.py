# LLM: 会话间派活的状态查询与取消工具。查询是只读；取消走 SessionTaskStore 的结构化状态变更
#   （不解析自然语言），并把"已取消"作为一条结构化消息回给发送方。
#   取消只能从非终态发起：SessionTaskStore 的状态机保证终态不被改写，这里不重复实现迁移规则。
# 模块用途: 让派活的会话能查任务状态、取消尚未结束的任务。
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ....conversation.session_tasks import (
    SESSION_TASK_CANCELLED,
    SESSION_TASK_TERMINAL_STATUSES,
    SessionTaskUpdate,
)
from ....runtime_errors import runtime_error_report
from ....tooling.models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

if TYPE_CHECKING:
    from ....core import SimpleAgent

_GET_NAME = "get_session_task"
_CANCEL_NAME = "cancel_session_task"


# LLM: 只承载一个 task_id；发送方身份从 runner 上下文取。
# 类用途: 保存一次任务查询或取消的参数。
@dataclass(frozen=True)
class _TaskRef:
    task_id: str


# LLM: 失败形状（错误码 + 影响面 + 面向用户的说明）打包传递，避免每个失败点都重复长参数列表。
# 类用途: 描述一次会话任务操作的机器可读失败。
@dataclass(frozen=True)
class _TaskFailure:
    error_code: str
    message: str
    tool: str = ""
    details: dict[str, object] | None = None
    effect_outcome: str = "not_started"


# LLM: 只读工具：返回任务的权威状态、正文引用与结果 refs；不改任何状态。
# 类用途: 查询一条会话任务的当前状态。
class GetSessionTaskTool(BaseTool):
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(
            mode="declared", static_scopes=("current_owner_session_tasks",)
        ),
        mutates_workspace=False,
        promotes_task=False,
    )

    # LLM: model_spec 是实例属性，保持统一装配方式。
    # 函数用途: 绑定当前 Agent，并生成给模型看的参数说明。
    def __init__(self, agent: SimpleAgent) -> None:
        self.agent = agent
        self.model_spec = build_get_session_task_model_spec()

    # LLM: 只读本 owner 的 store；任务不存在返回结构化失败，不猜任务。
    # 函数用途: 读取一条会话任务并返回结构化状态。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        ref = _task_ref(params, _GET_NAME)
        if isinstance(ref, ToolHandlerOutcome):
            return ref
        store = _session_task_store(self.agent)
        if isinstance(store, ToolHandlerOutcome):
            return store
        task = _load_task(store, ref.task_id, _GET_NAME)
        if isinstance(task, ToolHandlerOutcome):
            return task
        return ToolHandlerOutcome(
            _GET_NAME, True, json.dumps(_task_payload(task), ensure_ascii=False, indent=2)
        )


# LLM: 取消是结构化控制动作：只在非终态生效；成功后把"已取消"作为一条来源明确的消息回给发送方。
#   已经是终态时按幂等处理——返回当前状态，不改写（状态机保证终态不被覆盖）。
# 类用途: 取消一条尚未结束的会话任务，并通知派活的会话。
class CancelSessionTaskTool(BaseTool):
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        idempotency_policy=IdempotencyPolicy("operation"),
        concurrency_policy=ConcurrencyPolicy("serial"),
        resource_scopes=ResourceScopePolicy(
            parameter_names=("task_id",),
            parameter_kinds={"task_id": "logical"},
            resource_domains={"task_id": "session_task"},
        ),
        mutates_workspace=False,
        promotes_task=False,
    )

    # LLM: model_spec 是实例属性，保持统一装配方式。
    # 函数用途: 绑定当前 Agent，并生成给模型看的参数说明。
    def __init__(self, agent: SimpleAgent) -> None:
        self.agent = agent
        self.model_spec = build_cancel_session_task_model_spec()

    # LLM: 终态直接返回当前状态（不改写）；非终态按目标是否已接手分流：已接手就对它绑定的那个回合发
    #   /stop 同款精确停止控制，还在排队就撤掉目标队列里的正文。两条路径都推进到 cancelled，
    #   回执只按实际结果写"已停止""已撤销"或"未确认"。
    # 函数用途: 取消一条会话任务、真的叫停目标并把实际结果通知给派活的会话。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        resolved = _resolve_cancel_target(self.agent, params)
        if isinstance(resolved, ToolHandlerOutcome):
            return resolved
        payload = _cancel_and_describe(self.agent, resolved)
        if isinstance(payload, ToolHandlerOutcome):
            return payload
        return ToolHandlerOutcome(_CANCEL_NAME, True, json.dumps(payload, ensure_ascii=False, indent=2))


# LLM: 一次取消的结构化结果：终态记录 + 是否已通知发送方 + 停止/撤队列的实际结果 + 面向用户的说明。
# 类用途: 保存一次取消的实际影响面，供回执如实呈现。
@dataclass(frozen=True)
class _CancelOutcome:
    task: object
    notified: bool
    details: dict[str, object]
    message: str


# LLM: 校验参数 → 取存储 → 读任务 → 终态直接返回当前状态（不改写）。
# 函数用途: 解析本次要取消的任务；不能取消时返回结构化结果（失败或"已是终态"）。
def _resolve_cancel_target(agent: object, params: dict[str, object]) -> object | ToolHandlerOutcome:
    ref = _task_ref(params, _CANCEL_NAME)
    if isinstance(ref, ToolHandlerOutcome):
        return ref
    store = _session_task_store(agent)
    if isinstance(store, ToolHandlerOutcome):
        return store
    task = _load_task(store, ref.task_id, _CANCEL_NAME)
    if isinstance(task, ToolHandlerOutcome):
        return task
    if str(getattr(task, "status", "") or "") in SESSION_TASK_TERMINAL_STATUSES:
        return _terminal_task_outcome(task)
    return task


# LLM: 已是终态的任务按幂等处理：只回报当前状态，不改写、不发控制。
# 函数用途: 把"任务已是终态"转成一次成功的只读回执。
def _terminal_task_outcome(task: object) -> ToolHandlerOutcome:
    payload = _task_payload(task)
    payload["already_terminal"] = True
    payload["message"] = "任务已经是终态，未做改动。"
    return ToolHandlerOutcome(_CANCEL_NAME, True, json.dumps(payload, ensure_ascii=False, indent=2))


# LLM: 取消的实际动作 + 结果描述；异常按"状态未改变"的结构化失败返回，不谎称已停止。
# 函数用途: 执行取消并生成模型可读的回执载荷。
def _cancel_and_describe(agent: object, task: object) -> dict[str, object] | ToolHandlerOutcome:
    store = _session_task_store(agent)
    if isinstance(store, ToolHandlerOutcome):
        return store
    bound_turn_id = str(getattr(task, "conversation_request_id", "") or "").strip()
    try:
        outcome = _apply_cancel(agent, store, task, bound_turn_id)
    except Exception as exc:  # noqa: BLE001
        return _failure(
            _TaskFailure(
                error_code="TOOL_EXECUTION_FAILED",
                message="取消会话任务失败；状态未改变。",
                tool=_CANCEL_NAME,
                details=runtime_error_report(exc, context="cancel_session_task.advance"),
                effect_outcome="unknown",
            )
        )
    payload = _task_payload(outcome.task)
    payload["notified_sender"] = outcome.notified
    payload.update(outcome.details)
    payload["message"] = outcome.message
    return payload


# LLM: 已接手 → 对绑定的那个回合发 /stop 同款精确停止控制；还在排队 → 撤掉目标队列里的正文。
#   两条路径都推进到 cancelled；只有停止控制真的确认生效时文案才说"已停止"。
# 函数用途: 真的叫停目标（或撤队列）并把任务推进到已取消。
def _apply_cancel(
    agent: object, store: object, task: object, bound_turn_id: str
) -> _CancelOutcome:
    if bound_turn_id:
        stop = _stop_target_turn(agent, task)
        confirmed = bool(stop.confirmed)
        details: dict[str, object] = {
            "stop_confirmed": confirmed,
            "stop_message": str(stop.message or ""),
        }
        message = (
            "任务已取消：已停止目标上正在执行这个任务的回合。"
            if confirmed
            else "任务已取消；对目标执行这个任务的回合，停止控制尚未确认。"
        )
    else:
        withdrawn, note = _withdraw_queued_body(agent, task)
        details = {"withdrawn_from_queue": withdrawn}
        if note:
            details["withdraw_note"] = note
        message = (
            "任务还没有开始执行；已从目标队列撤销，不会再被执行。"
            if withdrawn
            else "任务还没有开始执行；已标记取消，但目标队列条目未确认撤销。"
        )
    cancelled = store.advance(
        str(getattr(task, "task_id", "") or ""), SessionTaskUpdate(status=SESSION_TASK_CANCELLED)
    )
    return _CancelOutcome(
        task=cancelled,
        notified=_notify_sender(agent, cancelled),
        details=details,
        message=message,
    )


# LLM: 只按任务已绑定的回合号发停止控制：回合号来自目标会话确认消费任务正文时写入的记录，
#   不读模型参数、不猜"目标当前那一个回合"。目标 thread 只用于取结构化渠道身份。
# 函数用途: 请求停止目标会话上执行这个任务的精确回合。
def _stop_target_turn(agent: object, task: object) -> object:
    from ....gateway_parts.session_task_stop import SessionTaskStopOutcome, stop_session_task_turn

    target_thread = None
    store = getattr(agent, "conversation_store", None)
    threads = getattr(store, "threads", None)
    target_thread_id = str(getattr(task, "target_thread_id", "") or "").strip()
    if threads is not None and target_thread_id:
        try:
            target_thread, _load_error = threads.load_report(target_thread_id)
        except Exception:  # noqa: BLE001 - 读不到目标会话就不能确认停止
            target_thread = None
    try:
        return stop_session_task_turn(
            agent,
            target_thread=target_thread,
            turn_id=str(getattr(task, "conversation_request_id", "") or ""),
        )
    except Exception:  # noqa: BLE001 - 异常不等于目标已停止
        return SessionTaskStopOutcome(False, "停止控制执行失败；目标是否已停止未确认。")


# LLM: 任务还没被目标接手时，撤队列就是把正文那条 guidance 的回执标成 rejected——回执一旦不是 pending，
#   注入层就不会再取到它，任务不会再被执行。已越过认领边界（reserved/submitted/consumed）时如实返回未撤销，
#   让回执写明"可能已经开始执行"，绝不谎称已撤销。
# 函数用途: 把还没开始执行的会话任务正文从目标队列撤掉。
def _withdraw_queued_body(agent: object, task: object) -> tuple[bool, str]:
    store = getattr(agent, "conversation_store", None)
    guidance = getattr(store, "guidance", None)
    key = str(getattr(task, "body_dedupe_key", "") or "").strip()
    if guidance is None or not key:
        return False, "没有可定位的队列条目。"
    try:
        receipt = guidance.receipt(key)
        if receipt is None:
            return False, "目标队列里找不到这条任务的正文。"
        if receipt.status == "pending":
            guidance.mark_status(key, "rejected")
            return True, ""
        if receipt.status == "rejected":
            return True, ""
    except Exception:  # noqa: BLE001 - 撤队列失败不影响取消本身已记录
        return False, "撤销队列条目失败。"
    return False, "目标已经开始处理这条正文，取消只能标记状态。"


# LLM: 任务正文只存一份在 guidance；payload 只暴露引用与状态，不复制正文。
# 函数用途: 生成任务的结构化投影，供模型读取。
def _task_payload(task: object) -> dict[str, object]:
    return {
        "ok": True,
        "task_id": str(getattr(task, "task_id", "") or ""),
        "status": str(getattr(task, "status", "") or ""),
        "sender_thread_id": str(getattr(task, "sender_thread_id", "") or ""),
        "target_thread_id": str(getattr(task, "target_thread_id", "") or ""),
        "goal": str(getattr(task, "goal", "") or ""),
        "body_guidance_id": str(getattr(task, "body_guidance_id", "") or ""),
        "result_refs": list(getattr(task, "result_refs", ()) or ()),
        "summary": str(getattr(task, "summary", "") or ""),
        "origin_task_id": str(getattr(task, "origin_task_id", "") or ""),
        "conversation_request_id": str(getattr(task, "conversation_request_id", "") or ""),
    }


# LLM: 取 task_id 参数；缺失即协议失败，不从当前会话或历史里推断任务。
# 函数用途: 读取并校验 task_id 参数。
def _task_ref(params: dict[str, object], tool: str) -> _TaskRef | ToolHandlerOutcome:
    task_id = str(params.get("task_id") or "").strip()
    if not task_id:
        return _failure(
            _TaskFailure(
                error_code="TOOL_PARAMETER_REQUIRED",
                message="缺少 task_id（要查询或取消的会话任务编号）。",
                tool=tool,
            )
        )
    return _TaskRef(task_id=task_id)


# LLM: store 缺失是环境问题，返回结构化失败，不静默当成"没有任务"。
# 函数用途: 取当前 Agent 的会话任务存储。
def _session_task_store(agent: object) -> object | ToolHandlerOutcome:
    store = getattr(agent, "conversation_store", None)
    tasks = getattr(store, "session_tasks", None)
    if tasks is None:
        return _failure(
            _TaskFailure(
                error_code="TOOL_EXECUTION_FAILED",
                message="当前运行环境没有会话任务存储。",
                tool=_GET_NAME,
            )
        )
    return tasks


# LLM: 读不到或读坏都返回结构化失败；不猜任务，也不当成已取消。
# 函数用途: 读取一条会话任务记录。
def _load_task(store: object, task_id: str, tool: str) -> object | ToolHandlerOutcome:
    try:
        task = store.load(task_id)
    except (ValueError, TypeError) as exc:
        return _failure(
            _TaskFailure(
                error_code="TOOL_EXECUTION_FAILED",
                message="读取会话任务失败。",
                tool=tool,
                details=runtime_error_report(exc, context="session_task.load"),
            )
        )
    if task is None:
        return _failure(
            _TaskFailure(
                error_code="SESSION_TASK_NOT_FOUND",
                message=f"找不到会话任务：{task_id}。",
                tool=tool,
            )
        )
    return task


# LLM: 取消后把结构化结果作为一条 message 回给发送方（走 guidance 幂等入队），
#   源标为 session_task_result，注入时同样按宿主事件呈现。失败不隐去取消本身已生效的事实。
# 函数用途: 把取消事实通知给派活的会话。
def _notify_sender(agent: object, task: object) -> bool:
    store = getattr(agent, "conversation_store", None)
    guidance = getattr(store, "guidance", None)
    sender = str(getattr(task, "sender_thread_id", "") or "").strip()
    if guidance is None or not sender:
        return False
    try:
        guidance.append_once(
            {
                "target_type": "thread",
                "target_id": sender,
                "message": f"你派出的任务 {getattr(task, 'task_id', '')} 已被取消。",
                "sender": str(getattr(task, "target_thread_id", "") or ""),
                "priority": "normal",
                "delivery": "next_turn",
                "metadata": {
                    "origin_kind": "session_task",
                    "origin_thread_id": str(getattr(task, "target_thread_id", "") or ""),
                },
            },
            dedupe_key=f"session_task_cancelled:{getattr(task, 'task_id', '')}",
        )
        # 只写消息箱、不唤醒，发送方空闲时这条通知会一直停在 pending，直到它碰巧跑下一轮。
        # 与派活投递同一条语义：目标空闲就唤起它，让它这一轮就能读到取消通知。
        _wake_sender(store, sender, str(getattr(task, "target_thread_id", "") or ""))
    except Exception:  # noqa: BLE001 - 通知失败不改变取消已生效的事实
        return False
    _record_pair(store, str(getattr(task, "target_thread_id", "") or ""), sender)
    return True


# LLM: 通知的投递语义必须和派活正文一致（写消息箱 + 空闲时唤醒）。唤醒只表达"有新消息"，
#   通知正文仍走同一条 guidance 注入路径，不另开一条投递通道。
# 函数用途: 发送方会话空闲时，为刚写入的取消通知唤醒它一轮。
def _wake_sender(store: object, sender_thread_id: str, target_thread_id: str) -> str:
    wakes = getattr(store, "wakes", None)
    threads = getattr(store, "threads", None)
    if wakes is None or threads is None:
        return ""
    thread, _error = threads.load_report(sender_thread_id)
    if thread is None or str(getattr(thread, "status", "") or "active").strip() != "active":
        return ""
    signal = wakes.raise_signal(
        {
            "thread_id": sender_thread_id,
            "urgency": "normal",
            "reason": "session_message",
            "summary": "派出的任务已被取消",
            "metadata": {
                "origin_kind": "session_message",
                "origin_thread_id": target_thread_id,
            },
        }
    )
    return str(getattr(signal, "wake_signal_id", "") or "")


# LLM: 取消通知也是宿主自动发出、不会被拒的消息，但必须占用每对会话配额；写失败不影响通知已投递。
# 函数用途: 把一条取消通知计入目标方与发送方之间的配额。
def _record_pair(store: object, sender_thread_id: str, target_thread_id: str) -> None:
    from ....conversation.session_pair_rate import record_pair_message

    record_pair_message(store, sender_thread_id, target_thread_id)


# LLM: 模型 schema 只暴露 task_id；查询是只读，取消是结构化控制动作。
# 函数用途: 定义 get_session_task 的参数与语义。
def build_get_session_task_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name=_GET_NAME,
        description=(
            "查询一条会话间任务的状态、正文引用与结果 refs。只读，不改变任务状态。"
        ),
        input_schema={
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "create_session_task 回执里的 task_id。"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="orchestration",
            use_cases=("想知道自己派出的任务进行到哪一步", "确认任务是否已结束"),
            avoid_when=("要取消任务用 cancel_session_task",),
            keywords=("任务状态", "查询任务", "session task status"),
            examples=('{"tool":"get_session_task","task_id":"stask-abc"}',),
        ),
    )


# LLM: 取消是结构化动作，不接受自然语言原因；模型不得把回执说成"目标已停止执行"。
# 函数用途: 定义 cancel_session_task 的参数与语义。
def build_cancel_session_task_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name=_CANCEL_NAME,
        description=(
            "取消一条尚未结束的会话间任务。已是终态的任务不会被改写，返回当前状态。"
            "成功只证明任务记录已取消并把取消通知排队给派活的会话，不证明目标已停止正在执行的工作。"
        ),
        input_schema={
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "要取消的任务编号。"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="orchestration",
            use_cases=("派出去的任务不再需要", "发现派错了想撤回"),
            avoid_when=("只是想看状态用 get_session_task",),
            keywords=("取消任务", "撤回", "cancel session task"),
            examples=('{"tool":"cancel_session_task","task_id":"stask-abc"}',),
        ),
    )


# LLM: 所有失败必须带稳定 error_code；details 只承载结构化诊断。
# 函数用途: 统一生成这两个工具的机器可读失败。
def _failure(failure: _TaskFailure) -> ToolHandlerOutcome:
    payload: dict[str, object] = {
        "ok": False,
        "error": "session_task_operation_failed",
        "message": failure.message,
    }
    if failure.details:
        payload["details"] = failure.details
    return ToolHandlerOutcome(
        failure.tool, False, json.dumps(payload, ensure_ascii=False, indent=2),
        error_code=failure.error_code, effect_outcome=failure.effect_outcome,
    )


__all__ = [
    "CancelSessionTaskTool",
    "GetSessionTaskTool",
    "build_cancel_session_task_model_spec",
    "build_get_session_task_model_spec",
]
