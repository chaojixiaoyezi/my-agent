from __future__ import annotations

"""LLM: 会话 transcript 权威标记只能由结构化 task_attributes 传递，禁止从 prompt 推断。

模块用途: 判断当前轮是否应以 ConversationStore 作为普通多轮对话的唯一事实源。
"""

from collections.abc import Mapping

CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR = "conversation_transcript_authoritative"
CONVERSATION_REQUEST_ID_ATTR = "conversation_request_id"
# A sticky workspace is present on later turns, while this transient flag is
# set only after a task-promoting tool actually executes in the current turn.
CONVERSATION_TASK_TURN_ACTIVE_ATTR = "conversation_task_turn_active"


# LLM: True 时调用方必须排除 legacy dialogue memory，并禁止重复写 owner-global dialogue。
# 函数用途: 读取本轮结构化属性中的会话 transcript 权威标记。
def conversation_transcript_is_authoritative(attributes: object) -> bool:
    """当前回合是否以 ConversationStore 会话流水作为普通对话的唯一事实源。"""
    return bool(
        isinstance(attributes, Mapping)
        and attributes.get(CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR) is True
    )


__all__ = [
    "CONVERSATION_REQUEST_ID_ATTR",
    "CONVERSATION_TASK_TURN_ACTIVE_ATTR",
    "CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR",
    "conversation_transcript_is_authoritative",
]
