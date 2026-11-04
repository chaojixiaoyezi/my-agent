"""退役delegate入口仅给迁移提示，不读写设置或影响已受理任务。"""
from __future__ import annotations

from ..conversation.control_commands import ConversationControlResult


# LLM: 旧命令不解析其他owner、不读写设置、不启动模型；已有任务保持原快照。
# 函数用途: 对所有delegate写法返回唯一route迁移提示。
def execute_delegation_control(base_agent, command, scope):
    return ConversationControlResult("delegate", False,
        "/delegate 已退役；请用 /route true、/route false 或 /route status。旧的当天 enable 不会自动开启永久模式；已接任务继续。")
