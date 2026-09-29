# LLM: 会话间发消息的模型工具。身份只从当前 runner 上下文取（agent.home_paths 的 owner 三元组 + 当前会话
#   thread_id），目标会话是模型显式参数；冲突或不合法时返回稳定错误码，不静默猜、不冒充别的会话。
#   投递只做两件事：向目标 thread 的 guidance 队列幂等写入一条来源结构化的消息；目标空闲时再触发一次唤醒。
#   成功只代表"已耐久排队"，不夸大成目标模型已接收或执行；文本与状态解析一律交给结构化回执。
# 模块用途: 让管理员会话能把一条消息发给本 owner 的另一个会话，并让空闲目标被唤醒后在宿主事件里看到来源。
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ....conversation.session_messaging import (
    SESSION_IDENTITY_UNAVAILABLE,
    SESSION_KIND_MESSAGE,
    SESSION_MESSAGE_ORIGIN_KIND,
    SESSION_NO_CURRENT_THREAD,
    SESSION_TASK_RATE_LIMIT,
    SessionMessagingRequest,
    decide_session_messaging,
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
from ....user_space.owner_resolver import OwnerIdentity

if TYPE_CHECKING:
    from ....core import SimpleAgent

_TOOL_NAME = "send_session_message"


# LLM: 只承载两个模型参数；发送方与来源信息是宿主内部字段，不进模型输入面。
# 类用途: 保存一次会话间发消息的目标会话与正文。
@dataclass(frozen=True)
class _SendInput:
    target_thread_id: str
    message: str


# LLM: 这是会话间消息的唯一模型入口；不要在这里加轮询、推进、验收或子代理语义。
#   权限判定在 conversation.session_messaging，存储与唤醒分别复用 guidance 与 wake 领域组件。
# 类用途: 把一条来源结构化、可幂等重试的会话消息写入目标会话，并按目标忙/闲决定是否唤醒。
class SendSessionMessageTool(BaseTool):
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        idempotency_policy=IdempotencyPolicy("operation"),
        concurrency_policy=ConcurrencyPolicy("serial"),
        resource_scopes=ResourceScopePolicy(
            parameter_names=("target_thread_id",),
            parameter_kinds={"target_thread_id": "logical"},
            resource_domains={"target_thread_id": "conversation_thread"},
        ),
        mutates_workspace=False,
        promotes_task=False,
    )

    # LLM: model_spec 是实例属性，保持与现有工具注册器一致的装配方式。
    # 函数用途: 绑定当前 Agent，并生成给模型看的最小参数说明。
    def __init__(self, agent: SimpleAgent) -> None:
        self.agent = agent
        self.model_spec = build_send_session_message_model_spec()

    # LLM: 顺序固定：校验参数 → 解析目标 thread → 取结构化 owner 开关 → 判定 → 写 guidance → 视忙闲唤醒。
    #   每一步失败都返回稳定错误码；成功的 effect_outcome 只承诺耐久入队。
    # 函数用途: 校验、授权并把一条会话消息耐久写入目标会话，空闲目标额外触发一次唤醒。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        parsed = _send_input(params)
        if isinstance(parsed, ToolHandlerOutcome):
            return parsed

        context = _sender_context(self.agent)
        if isinstance(context, ToolHandlerOutcome):
            return context
        sender_identity, sender_thread_id = context

        target_thread, target_owner = _load_target_thread(
            self.agent, parsed.target_thread_id, sender_identity=sender_identity
        )
        if isinstance(target_thread, ToolHandlerOutcome):
            return target_thread

        from ....capability.config import CapabilityConfig
        from ....capability.runtime_config_reload import capability_config_for_agent

        # 走统一入口：agent 上没有 capability_config 属性；读不到或损坏时也落到默认值（每对每小时 60），不能落到"不限制"。
        config = capability_config_for_agent(self.agent) or CapabilityConfig()
        decision = decide_session_messaging(
            SessionMessagingRequest(
                sender_identity=sender_identity,
                sender_thread_id=sender_thread_id,
                target_thread_id=parsed.target_thread_id,
                target_owner_identity=target_owner,
                kind=SESSION_KIND_MESSAGE,
                messaging_admin_enabled=bool(config.session_messaging_admin_enabled),
                messaging_user_enabled=bool(config.session_messaging_user_enabled),
                task_admin_enabled=bool(config.session_task_admin_enabled),
                target_channel=_thread_channel(target_thread),
            )
        )
        if not decision.allowed:
            return _denied(decision.error_code, decision.scope_warnings)

        limit_error = _pair_limit_error(
            self.agent,
            sender_thread_id,
            parsed.target_thread_id,
            config,
        )
        if limit_error is not None:
            return limit_error

        return _queue_and_wake(
            self.agent,
            parsed,
            sender_thread_id=sender_thread_id,
            target_thread=target_thread,
        )


# LLM: 每对会话每小时限额只按结构化计数与配置判定；0 表示不限制，没有计数器时同样放行。
#   在真正投递前判断，避免被拒的消息占用配额。
# 函数用途: 判断这次发消息是否超出每对会话限额，超出时返回结构化失败。
def _pair_limit_error(
    agent: object, sender_thread_id: str, target_thread_id: str, config: object
) -> ToolHandlerOutcome | None:
    from ....conversation.session_pair_rate import pair_limit_reached

    limit = int(config.session_pair_hourly_limit or 0)
    store = getattr(agent, "conversation_store", None)
    if not pair_limit_reached(store, sender_thread_id, target_thread_id, limit):
        return None
    return _error(
        f"这一对会话每小时的消息条数已达上限（{limit}）；消息没有投递。",
        error_code=SESSION_TASK_RATE_LIMIT,
        details={"limit": limit, "sender_thread_id": sender_thread_id, "target_thread_id": target_thread_id},
        effect_outcome="not_started",
    )


# LLM: 缺目标或缺正文都是无副作用的协议失败；不从当前会话或历史正文里推断目标。
# 函数用途: 读取并校验 send_session_message 的两个模型参数。
def _send_input(params: dict[str, object]) -> _SendInput | ToolHandlerOutcome:
    target = str(params.get("target_thread_id") or "").strip()
    message = str(params.get("message") or "").strip()
    if not target:
        return _error(
            "缺少 target_thread_id（要接收消息的会话编号）。",
            error_code="TOOL_PARAMETER_REQUIRED",
            effect_outcome="not_started",
        )
    if not message:
        return _error(
            "缺少 message（要发给目标会话的正文）。",
            error_code="TOOL_PARAMETER_REQUIRED",
            effect_outcome="not_started",
        )
    return _SendInput(target_thread_id=target, message=message)


# LLM: 发送方身份只从 agent.home_paths 的结构化 owner 三元组和当前会话 thread_id 取；
#   身份三元组任一缺失一律 fail closed（SESSION_IDENTITY_UNAVAILABLE），绝不默认成 main/local；
#   没有当前会话（独立命令）返回 SESSION_NO_CURRENT_THREAD。
# 函数用途: 解析当前 runner 的发送方 owner 身份与所在会话编号。
def _sender_context(agent: object) -> tuple[OwnerIdentity, str] | ToolHandlerOutcome:
    home = getattr(agent, "home_paths", None)
    provider = str(getattr(home, "owner_provider", "") or "").strip()
    owner_kind = str(getattr(home, "owner_kind", "") or "").strip()
    owner_id = str(getattr(home, "owner_id", "") or "").strip()
    if not (provider and owner_kind and owner_id):
        return _error(
            "无法确定当前会话的 owner 身份；消息没有投递。",
            error_code=SESSION_IDENTITY_UNAVAILABLE,
            effect_outcome="not_started",
        )
    identity = OwnerIdentity(provider=provider, owner_kind=owner_kind, owner_id=owner_id)
    thread_id = _current_thread_id(agent)
    if not thread_id:
        return _error(
            "当前上下文没有会话，不能发送会话消息。",
            error_code=SESSION_NO_CURRENT_THREAD,
            effect_outcome="not_started",
        )
    return identity, thread_id


# LLM: 目标 thread 必须在本 owner 的 store 里能读到；读不到（不存在、损坏、或不属本 owner）一律返回
#   (None, None)，让权限判定统一按越界码处理，不区分"不存在/无权限"，不泄露存在性。
#   能读到时目标必然同 owner，直接返回发送方身份作为目标身份，不做无法从 v10 thread 记录推导的 provider 猜测。
# 函数用途: 读取目标会话，供权限判定与唤醒使用。
def _load_target_thread(
    agent: object,
    thread_id: str,
    *,
    sender_identity: OwnerIdentity,
) -> tuple[object, OwnerIdentity] | tuple[None, None] | ToolHandlerOutcome:
    store = getattr(agent, "conversation_store", None)
    threads = getattr(store, "threads", None)
    if threads is None:
        return _error(
            "当前运行环境没有会话存储，不能发送会话消息。",
            error_code="TOOL_EXECUTION_FAILED",
            effect_outcome="not_started",
        )
    try:
        thread, load_error = threads.load_report(thread_id)
    except Exception as exc:  # noqa: BLE001 - 统一转成结构化失败
        return _error(
            "读取目标会话失败；消息没有投递。",
            error_code="TOOL_EXECUTION_FAILED",
            details=runtime_error_report(exc, context="send_session_message.target"),
            effect_outcome="not_started",
        )
    if load_error is not None or thread is None:
        return None, None
    return thread, sender_identity


# LLM: 目标会话的渠道从 canonical thread 的 channel_bindings 取第一个非空 channel；没有绑定时返回空串
#   （本机会话）。IM 渠道第一期不作为会话消息目标，判定层返回 SESSION_TARGET_CHANNEL_UNSUPPORTED。
# 函数用途: 读取目标会话的渠道标识，供权限判定拒绝 IM 目标。
def _thread_channel(thread: object) -> str:
    bindings = getattr(thread, "channel_bindings", ()) or ()
    for binding in bindings:
        channel = str(getattr(binding, "channel", "") or "").strip()
        if channel:
            return channel
    return ""


# LLM: 写入用 append_once 带稳定 dedupe_key，保证重试返回同一条；来源类型与发送方 thread 落 metadata，
#   便于注入时渲染成"来自会话 X 的消息"而不是用户原话。目标空闲时补一次 wake，不改其它队列语义。
# 函数用途: 耐久保存一条会话消息，并在目标空闲时唤醒它。
def _queue_and_wake(
    agent: object,
    parsed: _SendInput,
    *,
    sender_thread_id: str,
    target_thread: object,
) -> ToolHandlerOutcome:
    store = agent.conversation_store
    dedupe_key = f"session_message:{sender_thread_id}->{parsed.target_thread_id}"
    metadata = {
        "origin_kind": SESSION_MESSAGE_ORIGIN_KIND,
        "origin_thread_id": sender_thread_id,
    }
    try:
        entry = store.guidance.append_once(
            {
                "target_type": "thread",
                "target_id": parsed.target_thread_id,
                "message": parsed.message,
                "sender": sender_thread_id or "session",
                "priority": "normal",
                "delivery": "next_turn",
                "metadata": metadata,
            },
            dedupe_key=dedupe_key,
        )
        wake_id = _maybe_wake(store, target_thread, sender_thread_id)
    except Exception as exc:  # noqa: BLE001 - 统一转成结构化失败
        return _error(
            "写入目标会话消息箱失败；消息没有投递。",
            error_code="TOOL_EXECUTION_FAILED",
            details=runtime_error_report(exc, context="send_session_message.append"),
            effect_outcome="unknown",
        )

    # 投递成功后计入每对会话配额（与派活、回报共用同一计数），供限额判定。
    _record_pair(store, sender_thread_id, parsed.target_thread_id)

    payload = {
        "ok": True,
        "message_id": entry.guidance_id,
        "target_thread_id": parsed.target_thread_id,
        "delivery": "queued",
        "status": "pending",
        "wake_signal_id": wake_id,
        "delivery_timing": "next_turn_boundary_or_wake",
        "message": (
            "消息已耐久排队；这不代表目标模型已经接收或执行。目标空闲时会以宿主事件被唤醒，"
            "忙碌时在下一个回合边界注入；后续以结构化消息状态和目标输出为准。"
        ),
    }
    return ToolHandlerOutcome(_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False, indent=2))


# LLM: 计数写失败只影响节流精度，不影响已经投递成功的消息，所以这里吞掉异常。
# 函数用途: 把一条已投递的会话消息计入每对会话配额。
def _record_pair(store: object, sender_thread_id: str, target_thread_id: str) -> None:
    from ....conversation.session_pair_rate import record_pair_message

    record_pair_message(store, sender_thread_id, target_thread_id)


# LLM: 目标空闲才唤醒；status 不是 active 时不叫醒，避免给不会再消费的目标制造假 pending。
#   返回 wake_signal_id 供回执展示，不承诺目标已消费。
# 函数用途: 目标会话仍活跃时发起一次来源明确的唤醒。
def _maybe_wake(store: object, target_thread: object, sender_thread_id: str) -> str:
    status = str(getattr(target_thread, "status", "") or "active").strip()
    if status != "active":
        return ""
    wakes = getattr(store, "wakes", None)
    if wakes is None:
        return ""
    signal = wakes.raise_signal(
        {
            "thread_id": str(getattr(target_thread, "thread_id", "") or ""),
            "urgency": "normal",
            "reason": "session_message",
            "summary": "收到来自另一个会话的消息",
            "metadata": {"origin_kind": "session_message", "origin_thread_id": sender_thread_id},
        }
    )
    return str(getattr(signal, "wake_signal_id", "") or "")


# LLM: 模型 schema 只暴露两个参数；工具成功仅为 queued，模型不得复述成"目标已收到/已执行"。
# 函数用途: 定义模型能看到的 send_session_message 参数、语义与反例。
def build_send_session_message_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name=_TOOL_NAME,
        description=(
            "给同一用户下的另一个会话发一条消息。成功只证明消息已耐久排队，不证明目标会话已接收、"
            "执行或完成；目标空闲会被唤醒，忙碌则下一个回合边界注入。后续以结构化状态为准。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target_thread_id": {
                    "type": "string",
                    "description": "接收消息的会话编号（来自 list_sessions 等结构化来源）。",
                },
                "message": {
                    "type": "string",
                    "description": "要发给该会话的正文；它会以宿主事件呈现，不会冒充用户原话。",
                },
            },
            "required": ["target_thread_id", "message"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="orchestration",
            use_cases=("要把一条信息交给同用户下的另一个会话", "另一个会话空闲时需要被叫醒处理"),
            avoid_when=(
                "要派一个任务让目标会话开一轮用 create_session_task；"
                "要停止已派任务用 cancel_session_task",
            ),
            keywords=("会话消息", "跨会话", "发消息", "session message", "tell"),
            examples=(
                '{"tool":"send_session_message","target_thread_id":"thread-abc","message":"构建完成后把产物路径发我。"}',
            ),
        ),
    )


# LLM: 会话身份只读 task_attributes 的结构化 conversation_thread_id，不从用户文本或工具参数接受会话编号。
# 函数用途: 取当前会话线程编号；独立命令或无会话上下文返回空串。
def _current_thread_id(agent: object) -> str:
    from ....conversation.authority import current_conversation_task_attributes

    return str(current_conversation_task_attributes(agent).get("conversation_thread_id") or "").strip()


# LLM: 所有失败必须带稳定 error_code；details 只承载结构化诊断，不替代主错误。
# 函数用途: 统一生成 send_session_message 的机器可读失败结果。
def _error(
    message: str,
    *,
    error_code: str,
    details: dict[str, object] | None = None,
    effect_outcome: str,
) -> ToolHandlerOutcome:
    payload: dict[str, object] = {
        "ok": False,
        "error": "session_message_not_delivered",
        "message": message,
    }
    if details:
        payload["details"] = details
    return ToolHandlerOutcome(
        _TOOL_NAME, False, json.dumps(payload, ensure_ascii=False, indent=2),
        error_code=error_code, effect_outcome=effect_outcome,
    )


# LLM: 权限拒绝也走同一结构化失败形状，errors 与 scope_warnings 都由判定层给出。
# 函数用途: 把权限判定拒绝转成模型可读的稳定失败。
def _denied(error_code: str, warnings: tuple[str, ...]) -> ToolHandlerOutcome:
    return _error(
        "当前身份或目标范围不允许发送会话消息。",
        error_code=error_code,
        details={"scope_warnings": list(warnings)} if warnings else None,
        effect_outcome="not_started",
    )


__all__ = ["SendSessionMessageTool", "build_send_session_message_model_spec"]
