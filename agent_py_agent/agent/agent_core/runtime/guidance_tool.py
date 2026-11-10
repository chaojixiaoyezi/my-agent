# LLM: 这是模型可见的递归代理消息入口；保持与 会话运行时 send_input 相同的最小关系，
# 只允许当前代理向自己可见的单个下级 run 追加消息，不在这里暴露 thread/task/case
# 广播、轮询、推进、验收或调度语义。用户给主代理的插入继续走 active turn 输入链。
# 模块用途: 让主代理或子代理给一个正在协作的直接下级补充要求；成功只代表消息
# 已耐久排队，目标是否已提交给模型或被模型消费要以消息回执状态为准。
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...runtime_errors import runtime_error_report
from ...subagents.models import TaskStatus, task_status_in
from ...tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from ..orchestration.management_continuity import (
    ManagedChildAuthorization,
    ManagedChildDenied,
    authorize_managed_child,
    route_to_authorized_parent,
)

if TYPE_CHECKING:
    from ...core import SimpleAgent

_TOOL_NAME = "send_guidance"


# LLM: Model parameters are normalized once before authorization. Sender and
# metadata remain host-internal fields and therefore are not part of this
# public input value.
# 类用途: 保存模型提供的一个明确目标和一条非空补充消息。
@dataclass(frozen=True)
class _GuidanceInput:
    target: str
    message: str


# LLM: 工具只接受一个 target run 和一段 message；不要重新加入批量 scope、广播、
# priority 或 delivery 等模型控制面。底层 ConversationStore 仍可服务用户输入与 CLI。
# 类用途: 把一条补充要求安全写入指定下级的消息箱，并返回可追踪的 guidance_id。
class SendGuidanceTool(BaseTool):
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(
            parameter_names=("target",),
            parameter_kinds={"target": "logical"},
            resource_domains={"target": "agent_run"},
        ),
        # __operation_id 由执行器按调用注入，只用于无权转交的幂等键；模型看不到也不能伪造。
        input_policy=ToolInputPolicy(internal_parameters=("sender", "metadata", "__operation_id")),
    )

    # LLM: model_spec 是实例属性，便于现有工具注册器保持统一装配方式。
    # 函数用途: 绑定当前 Agent，并生成给模型看的最小参数说明。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.model_spec = build_send_guidance_model_spec()

    # LLM: 投递前必须过共享授权门并读取目标的权威 TaskStatus；只有仍可能进入执行轮的
    # 下级才可入队，终态/暂停/失败目标必须同步拒绝，不能制造永远无法消费的假 pending。
    # 成功只确认 queue append，不能声称 provider 已确认或目标模型已执行。
    # 授权身份来自 management_continuity：原请求方，或被宿主事实证明的定时检查所延续的原父任务。
    # 同 owner 现存孩子被拒时只把内容作为“请求”转交其直接父级，回执如实写 routed，不写目标已收到。
    # 函数用途: 校验目标、消息和子代理状态，确认它仍可工作后再把补充要求耐久排队。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        parsed = _guidance_input(params)
        if isinstance(parsed, ToolHandlerOutcome):
            return parsed
        authorized = _authorized_guidance_target(self.agent, parsed, params)
        if isinstance(authorized, ToolHandlerOutcome):
            return authorized
        if status_error := _guidance_status_error(authorized.task, parsed.target):
            return status_error
        return _queue_guidance(
            self.agent,
            params,
            parsed,
            authorization=authorized,
        )


# LLM: Missing target/message are protocol failures with no side effects. Do
# not infer either field from current children, model prose, or prior calls.
# 函数用途: 读取并校验 send_guidance 的两个模型参数。
def _guidance_input(
    params: dict[str, object],
) -> _GuidanceInput | ToolHandlerOutcome:
    target = str(params.get("target") or "").strip()
    message = str(params.get("message") or "").strip()
    if not target:
        return _guidance_error(
            "缺少 target（要接收消息的子代理 run_id）。",
            error_code="TOOL_PARAMETER_REQUIRED",
            effect_outcome="not_started",
        )
    if not message:
        return _guidance_error(
            "缺少 message（要补充给子代理的具体要求）。",
            error_code="TOOL_PARAMETER_REQUIRED",
            effect_outcome="not_started",
        )
    return _GuidanceInput(target=target, message=message)


# LLM: Direct-child authorization is the sole relationship gate. Storage and
# permission errors remain distinguishable structured failures and never fall
# back to an owner-wide scan. 管理身份可以是被证明的定时延续，但仍逐个走原直属下级门。
# 函数用途: 核对目标确实是当前管理身份的直属下级，并把异常转换成稳定错误码或转交回执。
def _authorized_guidance_target(
    agent: object,
    guidance: _GuidanceInput,
    params: dict[str, object],
) -> ManagedChildAuthorization | ToolHandlerOutcome:
    target = guidance.target
    try:
        return authorize_managed_child(agent, operation="send_guidance", run_id=target)
    except FileNotFoundError:
        return _guidance_error(
            f"目标子代理不存在：{target}。请使用 create_subagents 回执或生命周期事件里的真实 run_id。",
            error_code="TOOL_INVALID_ARGUMENTS",
            effect_outcome="not_started",
        )
    except ManagedChildDenied as exc:
        return _denied_or_routed(agent, exc, guidance, params)
    except PermissionError as exc:
        return _guidance_error(
            f"无权给目标子代理发送消息：{exc}",
            error_code="TOOL_PERMISSION_DENIED",
            effect_outcome="not_started",
        )
    except OSError as exc:
        return _guidance_error(
            "读取目标子代理失败；消息没有投递。",
            error_code="TOOL_EXECUTION_FAILED",
            details=runtime_error_report(exc, context="send_guidance.target"),
            effect_outcome="not_started",
        )


# LLM: 跨 owner、不存在或无宿主 operation_id 时保持原 TOOL_PERMISSION_DENIED 且不写任何东西；同 owner 现存孩子
#   才转交给其直接父级。转交成功是 ok=True + delivery=routed_to_authorized_parent，明确孩子没有收到；
#   转交写入异常结果未知，返回 effect_outcome=unknown，不能谎称没有副作用。
# 函数用途: 把直属下级门拒绝转换成诚实的拒绝或转交回执。
def _denied_or_routed(
    agent: object,
    denied: ManagedChildDenied,
    guidance: _GuidanceInput,
    params: dict[str, object],
) -> ToolHandlerOutcome:
    details: dict[str, object] = {}
    if denied.authorized_parent_run_id:
        details["authorized_parent_run_id"] = denied.authorized_parent_run_id
    if denied.warnings:
        details["scope_warnings"] = list(denied.warnings)
    try:
        route = route_to_authorized_parent(
            agent,
            denied,
            child_run_id=guidance.target,
            message=guidance.message,
            operation_key=str(params.get("__operation_id") or ""),
        )
    except Exception as exc:  # noqa: BLE001 - 转交写入结果未知必须如实上报
        return _guidance_error(
            "无权直接给目标子代理发送消息；转交给有权父级时写入失败，结果未知。",
            error_code="TOOL_EXECUTION_FAILED",
            details={**details, **runtime_error_report(exc, context="send_guidance.route")},
            effect_outcome="unknown",
        )
    if not route or route.get("routed") is not True:
        if route:
            details["route"] = route
        return _guidance_error(
            f"无权给目标子代理发送消息：{denied}",
            error_code="TOOL_PERMISSION_DENIED",
            details=details or None,
            effect_outcome="not_started",
        )
    payload = {
        "ok": True,
        "target": guidance.target,
        "delivery": "routed_to_authorized_parent",
        "delivered_to_target": False,
        "provider_acknowledged": False,
        "consumed": False,
        "route": route,
        **details,
        "message": (
            "你不是该子代理的直接父级，消息没有发给它；已作为转交请求耐久交给它的直接父级，"
            "由父级决定是否转发。route.receipt_status 是父级邮箱的真实回执状态。"
        ),
    }
    return ToolHandlerOutcome(_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False, indent=2))


# LLM: Eligibility is an exact TaskStatus decision. Terminal and temporarily
# non-runnable states return different codes so callers can replace or wait;
# the status message itself never changes lifecycle.
# 函数用途: 拒绝已经结束或当前没有执行轮的目标，避免留下永远无法消费的消息。
def _guidance_status_error(
    task: object,
    target: str,
) -> ToolHandlerOutcome | None:
    status = str(getattr(task, "status", "") or "").strip()
    eligible_statuses = {
        TaskStatus.PLANNING.value,
        TaskStatus.PENDING.value,
        TaskStatus.RUNNING.value,
        TaskStatus.BLOCKED.value,
    }
    if task_status_in(status, eligible_statuses):
        return None
    terminal = task_status_in(
        status,
        {
            TaskStatus.DONE.value,
            TaskStatus.CANCELLED.value,
            TaskStatus.ABANDONED.value,
            TaskStatus.TAKEN_OVER.value,
        },
    )
    return _guidance_error(
        (
            "目标子代理已经结束，不能把消息假装排进一个不会再消费的队列；"
            "如需继续这项工作，请创建职责明确的后续或替代子代理。"
            if terminal
            else "目标子代理当前没有可接收消息的执行轮；消息没有排队。"
        ),
        error_code=(
            "SUBAGENT_GUIDANCE_TARGET_TERMINAL"
            if terminal
            else "SUBAGENT_GUIDANCE_TARGET_NOT_RUNNING"
        ),
        details={
            "run_id": target,
            "status": status,
            "recommended_action": (
                "create_replacement_subagent" if terminal else "wait_or_recover_agent"
            ),
        },
        effect_outcome="not_started",
    )


# LLM: 插话持久操作经 guidance 领域组件； Append is the only side-effecting seam. Success means durable queue
# append only; provider acknowledgement and consumption remain false until the
# target runtime records them.
# 函数用途: 把已授权消息写入下级消息箱，并返回不夸大交付状态的排队回执。
def _queue_guidance(
    agent: object,
    params: dict[str, object],
    guidance: _GuidanceInput,
    *,
    authorization: ManagedChildAuthorization,
) -> ToolHandlerOutcome:
    principal = authorization.principal
    sender = str(params.get("sender") or principal.run_id or "main_agent").strip()
    metadata = dict(params.get("metadata")) if isinstance(params.get("metadata"), dict) else {}
    resolution = authorization.scope_resolution()
    if resolution.get("authority") != "self":
        # 代表原父任务发出时保留真实执行者（定时 run）身份，消息来源仍可追溯。
        metadata["management_authority"] = resolution
    try:
        entry = agent.conversation_store.guidance.append(
            {
                "target_type": "agent_run",
                "target_id": guidance.target,
                "message": guidance.message,
                "sender": sender,
                "priority": "normal",
                "delivery": "next_turn",
                "metadata": metadata,
            }
        )
    except OSError as exc:
        return _guidance_error(
            "写入子代理消息箱失败；消息没有投递。",
            error_code="TOOL_EXECUTION_FAILED",
            details=runtime_error_report(exc, context="send_guidance.append"),
            effect_outcome="unknown",
        )
    payload = {
        "ok": True,
        "guidance_id": entry.guidance_id,
        "target": guidance.target,
        "delivery": "queued",
        "status": "pending",
        "provider_acknowledged": False,
        "consumed": False,
        "delivery_timing": "current_tool_boundary_or_next_turn",
        "scope_resolution": resolution,
        "message": (
            "消息已耐久排队；这不代表目标模型已经接收或执行。目标会在当前工具边界"
            "或下一轮安全点读取，后续以结构化消息状态和目标输出为准。"
        ),
    }
    return ToolHandlerOutcome(
        _TOOL_NAME,
        True,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


# LLM: 模型 schema 刻意与 会话运行时 send_input 的核心参数对齐；本项目暂不暴露
# interrupt，因为 cancel_subagents 是独立停止动作。工具成功仅为 queued/pending，
# 模型不得把回执复述成“目标已收到、已执行或已完成”。
# 函数用途: 定义模型能看到的 send_guidance 参数、排队语义、使用场景和反例。
def build_send_guidance_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name=_TOOL_NAME,
        description=(
            "给一个正在运行的下级代理补充消息。成功只证明消息已耐久排队，不证明"
            "目标模型已接收、执行或完成；后续以结构化消息状态和目标输出为准。该工具"
            "不启动、轮询、推动或验收代理；要停止下级请用 cancel_subagents。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "接收消息的直接子代理 run_id（来自 create_subagents 回执或生命周期事件）。",
                },
                "message": {
                    "type": "string",
                    "description": "要插入该子代理当前任务的普通自然语言要求。",
                },
            },
            "required": ["target", "message"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="orchestration",
            use_cases=("用户补充了会影响某个下级的要求", "某个下级需要路径或目标纠偏"),
            avoid_when=(
                "第一次派工使用 create_subagents；只是等待状态变化时结束本回合等宿主事件；"
                "要停止运行中的下级使用 cancel_subagents",
            ),
            keywords=("补充", "纠偏", "插入消息", "steer", "message"),
            examples=(
                '{"tool":"send_guidance","target":"child-1","message":"把输出写到分配给你的目录，完成后继续原任务。"}',
            ),
        ),
    )


# LLM: 所有失败必须带稳定 error_code；details 只承载结构化诊断，不能替代主错误。
# 函数用途: 统一生成 send_guidance 的机器可读失败结果。
def _guidance_error(
    message: str,
    *,
    error_code: str,
    details: dict[str, object] | None = None,
    effect_outcome: str,
) -> ToolHandlerOutcome:
    payload: dict[str, object] = {
        "ok": False,
        "error": "guidance_not_delivered",
        "message": message,
    }
    if details:
        payload["details"] = details
    return ToolHandlerOutcome(
        _TOOL_NAME,
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code=error_code,
        effect_outcome=effect_outcome,
    )
