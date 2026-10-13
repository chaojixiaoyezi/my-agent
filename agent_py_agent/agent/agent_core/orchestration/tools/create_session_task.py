# LLM: 会话间派活的模型工具。身份只从当前 runner 上下文取；目标会话是显式参数。派活与发消息共用
#   conversation.session_messaging 的权限判定，区别只在 kind=task，并额外落一条 SessionTaskStore 权威记录。
#   任务正文只存一份（作为目标 thread 的 guidance 条目），SessionTaskStore 只引用它的 id。
#   权限由本工具判定；耐久派发、链深、幂等与配额统一归 conversation.session_task_dispatch，不另建路径。
# 模块用途: 让管理员会话把任务派给另一个本地会话，并在结束前提供状态查询与取消入口。
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ....conversation.session_messaging import (
    SESSION_IDENTITY_UNAVAILABLE,
    SESSION_KIND_TASK,
    SESSION_MESSAGE_HOST_EVENT_MARKER,
    SessionMessagingRequest,
    decide_session_messaging,
)
from ....conversation.session_task_dispatch import (
    SESSION_TASK_TOOL_NAME,
    SessionTaskDispatchPlan,
    dispatch_session_task,
    session_task_dispatch_error,
    session_task_pair_limit_error,
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



# LLM: 只承载模型可传的两个参数；发送方与来源信息是宿主内部字段。
# 类用途: 保存一次会话间派活的参数。
@dataclass(frozen=True)
class _CreateTaskInput:
    target_thread_id: str
    goal: str


# LLM: 这是会话间派活的唯一模型入口；不要在这里加轮询、推进或子代理语义。
# 类用途: 校验、授权并把一条任务派给另一个本地会话，同时落权威记录与投递正文。
class CreateSessionTaskTool(BaseTool):
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
        self.model_spec = build_create_session_task_model_spec()

    # LLM: 顺序：校验参数 → 取身份 → 读目标 → 权限判定 → 建记录 → 投正文 → 唤醒。
    #   链深在权限判定后按 origin_task_id 结构化计算；超限拒绝，不新建记录。
    # 函数用途: 校验、授权后调用唯一领域派发入口，写入原任务账、正文队列及唤醒。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        parsed = _create_input(params)
        if isinstance(parsed, ToolHandlerOutcome):
            return parsed

        context = _sender_context(self.agent)
        if isinstance(context, ToolHandlerOutcome):
            return context
        sender_identity, sender_thread_id = context

        target = _load_target(self.agent, parsed.target_thread_id, sender_identity=sender_identity)
        if isinstance(target, ToolHandlerOutcome):
            return target
        target_thread, target_owner = target

        from ....capability.config import CapabilityConfig
        from ....capability.runtime_config_reload import capability_config_for_agent

        # 配置读不到或损坏时也落到默认值（链深 4、每对每小时 60），不能落到"不限制"。
        config = capability_config_for_agent(self.agent) or CapabilityConfig()
        decision = decide_session_messaging(
            SessionMessagingRequest(
                sender_identity=sender_identity,
                sender_thread_id=sender_thread_id,
                target_thread_id=parsed.target_thread_id,
                target_owner_identity=target_owner,
                kind=SESSION_KIND_TASK,
                messaging_admin_enabled=bool(config.session_messaging_admin_enabled),
                messaging_user_enabled=bool(config.session_messaging_user_enabled),
                task_admin_enabled=bool(config.session_task_admin_enabled),
                target_channel=_thread_channel(target_thread),
            )
        )
        if not decision.allowed:
            return _denied(decision.error_code, decision.scope_warnings)

        limit_error = session_task_pair_limit_error(self.agent, sender_thread_id, parsed.target_thread_id, config)
        if limit_error is not None:
            return limit_error

        return dispatch_session_task(
            self.agent,
            SessionTaskDispatchPlan(sender_thread_id, parsed.target_thread_id, parsed.goal, _origin_task_id(self.agent)),
            target_thread,
            config,
        )


# LLM: 缺目标或缺任务描述都是无副作用的协议失败；不从当前会话或历史正文里推断。
# 函数用途: 读取并校验 create_session_task 的两个模型参数。
def _create_input(params: dict[str, object]) -> _CreateTaskInput | ToolHandlerOutcome:
    target = str(params.get("target_thread_id") or "").strip()
    goal = str(params.get("goal") or "").strip()
    if not target:
        return session_task_dispatch_error("缺少 target_thread_id（要接收任务的会话编号）。", error_code="TOOL_PARAMETER_REQUIRED")
    if not goal:
        return session_task_dispatch_error("缺少 goal（要派给目标会话的任务描述）。", error_code="TOOL_PARAMETER_REQUIRED")
    return _CreateTaskInput(target_thread_id=target, goal=goal)


# LLM: 身份只从 home_paths 的结构化三元组取；任一为空 fail closed（与发消息一致）。
# 函数用途: 解析当前 runner 的发送方身份与所在会话。
def _sender_context(agent: object) -> tuple[OwnerIdentity, str] | ToolHandlerOutcome:
    home = getattr(agent, "home_paths", None)
    provider = str(getattr(home, "owner_provider", "") or "").strip()
    owner_kind = str(getattr(home, "owner_kind", "") or "").strip()
    owner_id = str(getattr(home, "owner_id", "") or "").strip()
    if not (provider and owner_kind and owner_id):
        return session_task_dispatch_error(
            "无法确定当前会话的 owner 身份；任务没有派发。", error_code=SESSION_IDENTITY_UNAVAILABLE
        )
    thread_id = _current_thread_id(agent)
    if not thread_id:
        return session_task_dispatch_error("当前上下文没有会话，不能派发会话任务。", error_code="SESSION_NO_CURRENT_THREAD")
    return OwnerIdentity(provider=provider, owner_kind=owner_kind, owner_id=owner_id), thread_id


# LLM: 目标必须在本 owner 的 store 里读到；读不到按越界处理（与不存在共用），能读到即同 owner。
# 函数用途: 读取目标会话，供权限判定使用。
def _load_target(
    agent: object, thread_id: str, *, sender_identity: OwnerIdentity
) -> tuple[object, OwnerIdentity] | tuple[None, None] | ToolHandlerOutcome:
    store = getattr(agent, "conversation_store", None)
    threads = getattr(store, "threads", None)
    if threads is None:
        return session_task_dispatch_error("当前运行环境没有会话存储，不能派发会话任务。", error_code="TOOL_EXECUTION_FAILED")
    try:
        thread, load_error = threads.load_report(thread_id)
    except Exception as exc:  # noqa: BLE001
        return session_task_dispatch_error(
            "读取目标会话失败；任务没有派发。",
            error_code="TOOL_EXECUTION_FAILED",
            details=runtime_error_report(exc, context="create_session_task.target"),
        )
    if load_error is not None or thread is None:
        return None, None
    return thread, sender_identity


# LLM: 链来源只读宿主写入的结构化 task_attributes；本工具不接受模型传入的链深或父任务。
#   属性由 conversation 运行时按派活唤醒信封写入（conversation_session_task_id），
#   这样链深守卫在真实链路上才有输入——不能依赖任何手工赋值。
# 函数用途: 取当前回合所属的会话任务编号（不是派活回合则为空串）。
def _origin_task_id(agent: object) -> str:
    from ....conversation.authority import (
        CONVERSATION_SESSION_TASK_ID_ATTR,
        current_conversation_task_attributes,
    )

    attributes = current_conversation_task_attributes(agent)
    return str(attributes.get(CONVERSATION_SESSION_TASK_ID_ATTR) or "").strip()


# LLM: 目标会话渠道从 channel_bindings 取；供权限判定按白名单拒绝 IM 目标。
# 函数用途: 读取目标会话渠道。
def _thread_channel(thread: object) -> str:
    bindings = getattr(thread, "channel_bindings", ()) or ()
    for binding in bindings:
        channel = str(getattr(binding, "channel", "") or "").strip()
        if channel:
            return channel
    return ""


# LLM: 会话身份只读 task_attributes 的结构化 conversation_thread_id。
# 函数用途: 取当前会话线程编号；无会话上下文返回空串。
def _current_thread_id(agent: object) -> str:
    from ....conversation.authority import current_conversation_task_attributes

    return str(current_conversation_task_attributes(agent).get("conversation_thread_id") or "").strip()


# LLM: 模型 schema 只暴露两个参数；成功仅为 queued，模型不得复述成"目标已接受/已完成"。
# 函数用途: 定义模型能看到的 create_session_task 参数与语义。
def build_create_session_task_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name=SESSION_TASK_TOOL_NAME,
        description=(
            "把一个任务派给同一用户下的另一个本地会话。成功只证明任务已耐久排队并记录，"
            "不证明目标已接受或执行；目标空闲会被唤醒，结束时把结果作为消息回给当前会话。"
            "任务正文会以宿主事件呈现，不会冒充用户原话。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target_thread_id": {
                    "type": "string",
                    "description": "接收任务的会话编号（来自 /sessions threads 或结构化列表）。",
                },
                "goal": {
                    "type": "string",
                    "description": "要派给该会话的任务描述（正文只存一份，会作为宿主事件呈现）。",
                },
            },
            "required": ["target_thread_id", "goal"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="orchestration",
            use_cases=("要把一件完整工作交给同用户下的另一个会话", "另一端空闲时叫醒它接手"),
            avoid_when=("只想传一句话用 send_session_message", "要停止已派任务用 cancel_session_task"),
            keywords=("派任务", "会话任务", "delegate", "session task"),
            examples=(
                '{"tool":"create_session_task","target_thread_id":"thread-abc","goal":"按 inputs/x.md 生成报告并把路径发我。"}',
            ),
        ),
    )


# LLM: 权限拒绝走同一结构化失败形状。
# 函数用途: 把权限拒绝转成模型可读的稳定失败。
def _denied(error_code: str, warnings: tuple[str, ...]) -> ToolHandlerOutcome:
    return session_task_dispatch_error(
        "当前身份或目标范围不允许派发会话任务。",
        error_code=error_code,
        details={"scope_warnings": list(warnings)} if warnings else None,
    )


__all__ = ["CreateSessionTaskTool", "build_create_session_task_model_spec"]
