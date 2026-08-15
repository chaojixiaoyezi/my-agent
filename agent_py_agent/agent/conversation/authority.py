from __future__ import annotations

"""LLM: 会话 transcript 权威标记只能由结构化 task_attributes 传递，禁止从 prompt 推断。

模块用途: 判断当前轮是否应以 ConversationStore 作为普通多轮对话的唯一事实源。
"""

from collections.abc import Mapping

CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR = "conversation_transcript_authoritative"
CONVERSATION_REQUEST_ID_ATTR = "conversation_request_id"
# The exact Gateway turn id is separate from the durable conversation task id.
# A named Audit uses CONVERSATION_REQUEST_ID_ATTR for its stable root lineage,
# while this value fences same-turn prepare publication and stale recovery.
CONVERSATION_TURN_REQUEST_ID_ATTR = "conversation_turn_request_id"
# A sticky workspace is present on later turns, while this transient flag is
# set only after a task-promoting tool actually executes in the current turn.
CONVERSATION_TASK_TURN_ACTIVE_ATTR = "conversation_task_turn_active"
CONVERSATION_WORK_KIND_ATTR = "conversation_work_kind"
CONVERSATION_WORK_NAME_ATTR = "conversation_work_name"
CONVERSATION_WORK_DURATION_ATTR = "conversation_work_duration_seconds"
CONVERSATION_CANCELLATION_SCOPE_ATTR = "conversation_cancellation_scope"
# A typed background event may own the purpose of one continuation turn.
# Presentation helpers must not replace that event with a generic named-work
# receipt; the value comes from the scheduler, never from user/model prose.
CONVERSATION_BACKGROUND_EVENT_REASON_ATTR = "conversation_background_event_reason"
# A prepare turn is scoped to one exact durable Audit without activating its
# long-running guarantee.  The stable id and workspace are injected by the
# gateway after owner/thread-scoped resolution; user prose never supplies them.
CONVERSATION_AUDIT_PREPARE_ATTR = "conversation_audit_prepare"
CONVERSATION_TRANSIENT_WORKSPACE_ATTR = "conversation_transient_workspace"
# The sticky workspace and the live task are deliberately different facts.
# A completed/interrupted task may still own the directory inherited by the
# next turn, but its terminal lifecycle must never become execution authority.
CONVERSATION_WORKSPACE_TASK_ID_ATTR = "conversation_workspace_task_id"
CONVERSATION_WORKSPACE_TASK_STATUS_ATTR = "conversation_workspace_task_status"
CONVERSATION_WORKSPACE_EXECUTION_RUNNING_ATTR = "conversation_workspace_execution_running"
CONVERSATION_WORKSPACE_EXECUTION_STATE_AVAILABLE_ATTR = (
    "conversation_workspace_execution_state_available"
)


# LLM: True 时调用方必须排除 legacy dialogue memory，并禁止重复写 owner-global dialogue。
# 函数用途: 读取本轮结构化属性中的会话 transcript 权威标记。
def conversation_transcript_is_authoritative(attributes: object) -> bool:
    """当前回合是否以 ConversationStore 会话流水作为普通对话的唯一事实源。"""
    return bool(
        isinstance(attributes, Mapping)
        and attributes.get(CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR) is True
    )


def current_conversation_task_attributes(agent: object) -> dict[str, object]:
    """Return the current structured task scope for a main or delegated turn."""

    current = getattr(agent, "_current_run_params", None)
    attributes = getattr(current, "task_attributes", None)
    if isinstance(attributes, dict):
        return attributes
    try:
        from ..agent_core.runner.context import current_task_attributes

        delegated = current_task_attributes(agent)
    except (AttributeError, ImportError, RuntimeError):
        delegated = None
    return delegated if isinstance(delegated, dict) else {}


__all__ = [
    "CONVERSATION_REQUEST_ID_ATTR",
    "CONVERSATION_TURN_REQUEST_ID_ATTR",
    "CONVERSATION_TASK_TURN_ACTIVE_ATTR",
    "CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR",
    "CONVERSATION_WORK_KIND_ATTR",
    "CONVERSATION_WORK_NAME_ATTR",
    "CONVERSATION_WORK_DURATION_ATTR",
    "CONVERSATION_CANCELLATION_SCOPE_ATTR",
    "CONVERSATION_BACKGROUND_EVENT_REASON_ATTR",
    "CONVERSATION_AUDIT_PREPARE_ATTR",
    "CONVERSATION_TRANSIENT_WORKSPACE_ATTR",
    "CONVERSATION_WORKSPACE_EXECUTION_RUNNING_ATTR",
    "CONVERSATION_WORKSPACE_EXECUTION_STATE_AVAILABLE_ATTR",
    "CONVERSATION_WORKSPACE_TASK_ID_ATTR",
    "CONVERSATION_WORKSPACE_TASK_STATUS_ATTR",
    "current_conversation_task_attributes",
    "conversation_transcript_is_authoritative",
]
