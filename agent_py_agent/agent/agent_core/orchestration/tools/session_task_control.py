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

    # LLM: 终态直接返回当前状态（不改写）；非终态推进到 cancelled 并回报发送方。
    # 函数用途: 取消一条会话任务并把结果通知给派活的会话。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        ref = _task_ref(params, _CANCEL_NAME)
        if isinstance(ref, ToolHandlerOutcome):
            return ref
        store = _session_task_store(self.agent)
        if isinstance(store, ToolHandlerOutcome):
            return store
        task = _load_task(store, ref.task_id, _CANCEL_NAME)
        if isinstance(task, ToolHandlerOutcome):
            return task
        if task.status in SESSION_TASK_TERMINAL_STATUSES:
            payload = _task_payload(task)
            payload["already_terminal"] = True
            payload["message"] = "任务已经是终态，未做改动。"
            return ToolHandlerOutcome(_CANCEL_NAME, True, json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            cancelled = store.advance(task.task_id, status=SESSION_TASK_CANCELLED)
            notified = _notify_sender(self.agent, cancelled)
        except Exception as exc:  # noqa: BLE001
            return _error(
                "取消会话任务失败；状态未改变。",
                tool=_CANCEL_NAME,
                error_code="TOOL_EXECUTION_FAILED",
                details=runtime_error_report(exc, context="cancel_session_task.advance"),
                effect_outcome="unknown",
            )
        payload = _task_payload(cancelled)
        payload["notified_sender"] = notified
        payload["message"] = "任务已取消；这不代表目标已经停止正在执行的工作。"
        return ToolHandlerOutcome(_CANCEL_NAME, True, json.dumps(payload, ensure_ascii=False, indent=2))


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
        return _error("缺少 task_id（要查询或取消的会话任务编号）。", tool=tool, error_code="TOOL_PARAMETER_REQUIRED")
    return _TaskRef(task_id=task_id)


# LLM: store 缺失是环境问题，返回结构化失败，不静默当成"没有任务"。
# 函数用途: 取当前 Agent 的会话任务存储。
def _session_task_store(agent: object) -> object | ToolHandlerOutcome:
    store = getattr(agent, "conversation_store", None)
    tasks = getattr(store, "session_tasks", None)
    if tasks is None:
        return _error(
            "当前运行环境没有会话任务存储。", tool=_GET_NAME, error_code="TOOL_EXECUTION_FAILED"
        )
    return tasks


# LLM: 读不到或读坏都返回结构化失败；不猜任务，也不当成已取消。
# 函数用途: 读取一条会话任务记录。
def _load_task(store: object, task_id: str, tool: str) -> object | ToolHandlerOutcome:
    try:
        task = store.load(task_id)
    except (ValueError, TypeError) as exc:
        return _error(
            "读取会话任务失败。",
            tool=tool,
            error_code="TOOL_EXECUTION_FAILED",
            details=runtime_error_report(exc, context="session_task.load"),
        )
    if task is None:
        return _error(f"找不到会话任务：{task_id}。", tool=tool, error_code="SESSION_TASK_NOT_FOUND")
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
    except Exception:  # noqa: BLE001 - 通知失败不改变取消已生效的事实
        return False
    return True


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
def _error(
    message: str,
    *,
    tool: str,
    error_code: str,
    details: dict[str, object] | None = None,
    effect_outcome: str = "not_started",
) -> ToolHandlerOutcome:
    payload: dict[str, object] = {"ok": False, "error": "session_task_operation_failed", "message": message}
    if details:
        payload["details"] = details
    return ToolHandlerOutcome(
        tool, False, json.dumps(payload, ensure_ascii=False, indent=2),
        error_code=error_code, effect_outcome=effect_outcome,
    )


__all__ = [
    "CancelSessionTaskTool",
    "GetSessionTaskTool",
    "build_cancel_session_task_model_spec",
    "build_get_session_task_model_spec",
]
