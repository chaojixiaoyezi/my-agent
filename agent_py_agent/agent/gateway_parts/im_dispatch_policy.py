# LLM: 新 IM 入口在原消息回执提交前冻结当天执行偏好；恢复只消费原 prepared_request，不根据零点后开关改变旧任务。
# 模块用途: 将用户委派偏好绑定到被接受的消息，保持原 TUI、关闭模式及已排队消息语义。
from __future__ import annotations

from ..capability.config import CapabilityConfig
from ..capability.runtime_config_reload import capability_config_for_agent
from ..conversation.channels import supports_transcript_delivery
from ..settings.im_task_preferences import delegation_preference

IM_DISPATCH_KEY = "host_im_dispatch.v1"


# LLM: scope 与 owner 原工具政策决定资格；不读 body 自报开关。显式无 Agent 的队列服务器不生成偏好快照，HTTP 身份门仍由原中间件负责。
# 函数用途: 在 IM 受理时冻结发送者的执行方式；仅运输队列的宿主保持原请求，零点、关闭或重送不改变已受理记录。
def capture_im_dispatch(base_agent, scope) -> dict | None:
    if base_agent is None or supports_transcript_delivery(scope.channel):
        return None
    from .control_service import _request_agent_for_scope

    agent = _request_agent_for_scope(base_agent, scope)
    config = capability_config_for_agent(agent) or CapabilityConfig()
    preference = delegation_preference(agent, scope.channel, scope.user_id)
    enabled = (config.im_task_delegation_enabled and agent.config.enable_subagents and preference["enabled"]
               and "create_subagents" not in getattr(getattr(agent, "owner_policy", None), "disabled_tools", ()))
    return {"mode": "delegated" if enabled else "original", "channel": scope.channel,
            "actor_id": scope.user_id, "conversation_id": scope.conversation_id, "owner_id": str(agent.home_paths.owner_id),
            "preference_revision": preference["revision"], "expires_at": preference["expires_at"]}
