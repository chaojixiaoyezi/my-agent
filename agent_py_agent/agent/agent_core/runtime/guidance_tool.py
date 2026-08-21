# LLM: 这是模型可见的递归代理消息入口；保持与 会话运行时 send_input 相同的最小关系，
# 只允许当前代理向自己可见的单个下级 run 追加消息，不在这里暴露 thread/task/case
# 广播、轮询、推进、验收或调度语义。用户给主代理的插入继续走 active turn 输入链。
# 模块用途: 让主代理或子代理给一个正在协作的直接下级补充要求，消息在工具边界
# 或下一轮被读取；停止下级使用 cancel_subagents。
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ...runtime_errors import runtime_error_report
from ...subagents.authorization_gate import OperationRequest, authorize_operation
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
from ..orchestration.create_policy import _current_run_id
from ..runner.context import current_subagent_run_id

if TYPE_CHECKING:
    from ...core import SimpleAgent

_TOOL_NAME = "send_guidance"


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
        input_policy=ToolInputPolicy(internal_parameters=("sender", "metadata")),
    )

    # LLM: model_spec 是实例属性，便于现有工具注册器保持统一装配方式。
    # 函数用途: 绑定当前 Agent，并生成给模型看的最小参数说明。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.model_spec = build_send_guidance_model_spec()

    # LLM: 投递前必须过共享授权门，保证递归代理只能操作自己的子树；任何解析、
    # 权限或存储失败都返回结构化错误，不能静默改投父代理、兄弟代理或整棵树。
    # 函数用途: 校验目标和消息、确认目标属于当前代理的下级，然后写入消息箱。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        target = str(params.get("target") or "").strip()
        message = str(params.get("message") or "").strip()
        if not target:
            return _guidance_error(
                "缺少 target（要接收消息的子代理 run_id）。",
                error_code="TOOL_PARAMETER_REQUIRED",
            )
        if not message:
            return _guidance_error(
                "缺少 message（要补充给子代理的具体要求）。",
                error_code="TOOL_PARAMETER_REQUIRED",
            )
        requester_run_id = _current_run_id(self.agent)
        try:
            task = authorize_operation(
                self.agent.subagents,
                OperationRequest(
                    operation="send_guidance",
                    run_id=target,
                    requester_owner=str(
                        getattr(getattr(self.agent, "home_paths", None), "owner_id", "")
                        or ""
                    ),
                    requester_run_id=requester_run_id,
                ),
            )
        except FileNotFoundError:
            return _guidance_error(
                f"目标子代理不存在：{target}。请先用 inspect_agent_tree 查看当前下级。",
                error_code="TOOL_INVALID_ARGUMENTS",
            )
        except PermissionError as exc:
            return _guidance_error(
                f"无权给目标子代理发送消息：{exc}",
                error_code="TOOL_PERMISSION_DENIED",
            )
        except OSError as exc:
            return _guidance_error(
                "读取目标子代理失败；消息没有投递。",
                error_code="TOOL_EXECUTION_FAILED",
                details=runtime_error_report(exc, context="send_guidance.target"),
            )
        if requester_run_id and str(getattr(task, "parent_id", "") or "").strip() != requester_run_id:
            return _guidance_error(
                "只能给自己直接创建的下级发送消息；请让直接下级继续管理它的子代理。",
                error_code="TOOL_PERMISSION_DENIED",
            )

        sender = str(
            params.get("sender")
            or current_subagent_run_id(self.agent)
            or requester_run_id
            or "main_agent"
        ).strip()
        metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
        try:
            entry = self.agent.conversation_store.append_guidance(
                {
                    "target_type": "agent_run",
                    "target_id": target,
                    "message": message,
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
            )
        payload = {
            "ok": True,
            "guidance_id": entry.guidance_id,
            "target": target,
            "delivery": "current_tool_boundary_or_next_turn",
            "message": "消息已插入目标子代理；它会继续原任务，不需要再用工具推动。",
        }
        return ToolHandlerOutcome(
            _TOOL_NAME,
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )


# LLM: 模型 schema 刻意与 会话运行时 send_input 的核心参数对齐；本项目暂不暴露
# interrupt，因为 cancel_subagents 是独立停止动作，普通补充消息不应隐式终止当前轮。
# 函数用途: 定义模型能看到的 send_guidance 参数、使用场景和反例。
def build_send_guidance_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name=_TOOL_NAME,
        description=(
            "给一个正在运行的下级代理补充消息。只负责插入新要求，不启动、轮询、"
            "推动或验收代理；要停止下级请用 cancel_subagents。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "接收消息的子代理 run_id（来自 create_subagents 或 inspect_agent_tree）。",
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
                "第一次派工使用 create_subagents；只想看状态使用 inspect_agent_tree；"
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
    )
