# LLM: /delegate 只处理可信控制 scope 的发送者偏好，不改变管理员身份、工具权限或正在执行的任务；普通语言不能写此配置。
# 模块用途: 在飞书等 IM 查看或修改当天的自动委派设置，返回确定的控制回执。
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ..capability.config import CapabilityConfig
from ..capability.runtime_config_reload import capability_config_for_agent
from ..conversation.control_commands import ConversationControlResult
from ..runtime_errors import DataCorruptionError
from ..settings.im_task_preferences import delegation_preference, set_delegation_preference


# LLM: owner 解析沿原 Gateway 控制授权，user_id/channel 来自 scope，不接受命令参数指定别的用户；写设置不启动模型。
# 函数用途: 执行 /delegate enable、disable、status，开启只到业务零点，关闭不停止已接任务。
def execute_delegation_control(base_agent, command, scope):
    from .control_service import _request_agent_for_scope

    if not command.valid:
        return ConversationControlResult("delegate", False, command.usage)
    try:
        agent = _request_agent_for_scope(base_agent, scope)
        config = capability_config_for_agent(agent) or CapabilityConfig()
        if command.value == "enable" and not (config.im_task_delegation_enabled and agent.config.enable_subagents
                and "create_subagents" not in getattr(getattr(agent, "owner_policy", None), "disabled_tools", ())):
            return ConversationControlResult("delegate", False, "当前系统未启用自动委派能力。")
        preference = (delegation_preference(agent, scope.channel, scope.user_id)
                      if command.value in {"", "status"} else set_delegation_preference(
                          agent, scope.channel, scope.user_id, command.value == "enable"))
        if preference["enabled"]:
            expiry = datetime.fromtimestamp(preference["expires_at"], ZoneInfo(preference["timezone"])).isoformat()
            message = f"当天自动委派：已开启，有效至 {expiry}。只影响新任务，已接任务保持原方式。"
        else:
            message = "当天自动委派：关闭。新任务按原方式执行，已接任务继续。"
        return ConversationControlResult("delegate", True, message)
    except (ValueError, OSError, DataCorruptionError):
        return ConversationControlResult("delegate", False, "无法读取或修改当前 IM 用户的委派设置。", error_code="TOOL_EXECUTION_FAILED")
