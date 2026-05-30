# LLM: send_guidance is the unified soft steering tool for active main/sub agents.
# 模块用途: 把人工补充、父级纠偏、调度提示统一写入 guidance 账本，下一轮由目标代理读取。

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..conversation.store_guidance import normalize_guidance_target_type
from ..tools import BaseTool, ToolExecutionResult, ToolSpec
from .runner_context import current_subagent_run_id

if TYPE_CHECKING:
    from ..core import SimpleAgent

_TOOL_NAME = "send_guidance"


# LLM: GuidanceToolRequest is the normalized input bundle for send_guidance.
# 类用途: 保存目标、正文、发送者和投递元数据，避免工具入口散落参数判断。
@dataclass(frozen=True)
class GuidanceToolRequest:
    target_type: str
    target_id: str
    message: str
    sender: str
    priority: str
    delivery: str
    metadata: dict[str, Any]


# LLM: SendGuidanceTool only writes a soft inbox row; it never runs or blocks the target.
# 类用途: 给主代理、子代理或长期会话补充下一步提示，作为运行中软引导的统一入口。
class SendGuidanceTool(BaseTool):
    # LLM: __init__ stores the agent facade and static tool spec.
    # 函数用途: 初始化 send_guidance 工具，不读取或写入 guidance 账本。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_send_guidance_spec()

    # LLM: execute normalizes open-world target aliases and stores guidance refs.
    # 函数用途: 解析 target/message，写入 ConversationStore guidance 账本并返回 guidance_id。
    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        request = _guidance_request(self.agent, params)
        if isinstance(request, ToolExecutionResult):
            return request
        entry = self.agent.conversation_store.append_guidance(
            {
                "target_type": request.target_type,
                "target_id": request.target_id,
                "message": request.message,
                "sender": request.sender,
                "priority": request.priority,
                "delivery": request.delivery,
                "metadata": request.metadata,
            }
        )
        payload = {
            "ok": True,
            "guidance_id": entry.guidance_id,
            "target": {"type": entry.target_type, "id": entry.target_id},
            "delivery": entry.delivery,
            "message": "已写入软提示；目标代理下一轮会读取，不会被强制停止或硬阻断。",
        }
        return ToolExecutionResult(_TOOL_NAME, True, json.dumps(payload, ensure_ascii=False, indent=2))


# LLM: build_send_guidance_spec exposes guidance as a soft orchestration tool.
# 函数用途: 构建模型可见的 send_guidance 工具说明，区别于 dispatch 的推进职责。
def build_send_guidance_spec() -> ToolSpec:
    return ToolSpec(
        name=_TOOL_NAME,
        category="orchestration",
        effect="mutating",
        requires_idempotency=True,
        description="给正在运行的主代理、子代理、孙代理、会话或任务追加一条软提示；只影响下一轮判断，不推进、不验收、不硬卡。",
        use_cases=[
            "用户在任务运行中补一句要求、纠偏或提醒",
            "父代理想提醒某个下级换来源、补证据、先写草稿或尽快汇报",
            "需要给一个 thread/task/case 留下一条后续醒来可见的提示",
        ],
        avoid_when=["需要真正推进、重跑或恢复子代理时继续用 dispatch_subagents；第一次派工继续用 create_subagents"],
        keywords=["补充提示", "引导", "纠偏", "催一下", "steer", "guidance", "message"],
        parameters={
            "target": "目标对象，可写 {type,id}；type 常见值 agent_run/thread/task/case，未知类型也会按原名保存",
            "target_type": "不使用 target 时可直接写 target_type",
            "target_id": "不使用 target 时可直接写 target_id",
            "run_id": "agent_run 目标别名",
            "thread_id": "thread 目标别名",
            "task_id": "task 目标别名",
            "case_id": "case 目标别名",
            "message": "要给目标下一轮看的补充提示，必须是具体可执行的人话",
            "priority": "软优先级文本，默认 normal",
            "delivery": "投递方式提示，默认 next_turn",
        },
        examples=[
            '{"tool":"send_guidance","target":{"type":"agent_run","id":"child-1"},"message":"换一个数据来源核对，不要重复查同一个页面。"}',
            '{"tool":"send_guidance","thread_id":"thread-1","message":"用户补充：最终报告里要把未命中的来源也写清楚。"}',
        ],
    )


# LLM: _guidance_request validates only target/message shape, not business content.
# 函数用途: 将工具参数归一成 GuidanceToolRequest；错误时返回结构化工具失败。
def _guidance_request(agent: object, params: dict[str, object]) -> GuidanceToolRequest | ToolExecutionResult:
    target_type, target_id = _target_from_params(params)
    message = str(params.get("message") or "").strip()
    if not target_type or not target_id:
        return _guidance_error("缺少 target；请提供 target:{type,id}、run_id、thread_id、task_id 或 case_id。")
    if not message:
        return _guidance_error("缺少 message；send_guidance 只记录具体补充提示。")
    sender = str(params.get("sender") or current_subagent_run_id(agent) or "main_agent").strip()
    metadata = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
    return GuidanceToolRequest(
        target_type=target_type,
        target_id=target_id,
        message=message,
        sender=sender,
        priority=str(params.get("priority") or "normal").strip() or "normal",
        delivery=str(params.get("delivery") or "next_turn").strip() or "next_turn",
        metadata=metadata,
    )


# LLM: _target_from_params accepts open-world target aliases without a closed enum hard gate.
# 函数用途: 从 target/run_id/thread_id/task_id/case_id 等字段解析 guidance 目标。
def _target_from_params(params: dict[str, object]) -> tuple[str, str]:
    target = params.get("target")
    if isinstance(target, dict):
        target_type = normalize_guidance_target_type(target.get("type") or target.get("target_type"))
        target_id = str(target.get("id") or target.get("target_id") or "").strip()
        if target_type and target_id:
            return target_type, target_id
    alias_pairs = (
        ("run_id", "agent_run"),
        ("agent_run_id", "agent_run"),
        ("thread_id", "thread"),
        ("task_id", "task"),
        ("case_id", "case"),
    )
    for key, target_type in alias_pairs:
        value = str(params.get(key) or "").strip()
        if value:
            return target_type, value
    return (
        normalize_guidance_target_type(params.get("target_type") or params.get("type")),
        str(params.get("target_id") or params.get("id") or "").strip(),
    )


# LLM: _guidance_error keeps invalid guidance calls visible to the model.
# 函数用途: 返回 send_guidance 的 JSON 失败结果，不抛异常中断主循环。
def _guidance_error(message: str) -> ToolExecutionResult:
    payload = {"ok": False, "error": "invalid_guidance_request", "message": message}
    return ToolExecutionResult(_TOOL_NAME, False, json.dumps(payload, ensure_ascii=False, indent=2))
