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
# A child-lifecycle continuation records the tree phase seen before its first
# model sample.  Finalization uses this snapshot only as an event freshness
# fence; it never judges whether the user's objective is good enough.
CONVERSATION_BACKGROUND_SUBAGENT_PHASE_ATTR = "conversation_background_subagent_phase_at_start"
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
# The client project cwd is a trusted thread setting, not a model-selected task path. Gateway
# validation persists it and projects it into each foreground/background turn.
CONVERSATION_EXECUTION_CWD_ATTR = "conversation_execution_cwd"
CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR = "conversation_runtime_workspace_roots"


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


# LLM: The Gateway-validated client cwd is the only per-thread override for relative path
# semantics. Callers must not fall back to prompt text, task titles, or hidden task_root fields.
# 函数用途: 从当前会话属性读取 TUI/CLI 指定并由 Gateway 校验过的真实工作目录。
def conversation_execution_cwd(attributes: object) -> str:
    if not isinstance(attributes, Mapping):
        return ""
    return str(attributes.get(CONVERSATION_EXECUTION_CWD_ATTR) or "").strip()


# LLM: Runtime roots are persisted with cwd and only narrow/extend the same validated local
# client scope. Missing roots fall back to cwd so every invocation has one explicit root.
# 函数用途: 读取本会话允许作为工具工作区的根目录列表。
def conversation_runtime_workspace_roots(attributes: object) -> tuple[str, ...]:
    if not isinstance(attributes, Mapping):
        return ()
    raw = attributes.get(CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR)
    values = raw if isinstance(raw, (list, tuple)) else ()
    roots = tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
    cwd = conversation_execution_cwd(attributes)
    return roots or ((cwd,) if cwd else ())


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
    "CONVERSATION_BACKGROUND_SUBAGENT_PHASE_ATTR",
    "CONVERSATION_AUDIT_PREPARE_ATTR",
    "CONVERSATION_TRANSIENT_WORKSPACE_ATTR",
    "CONVERSATION_WORKSPACE_EXECUTION_RUNNING_ATTR",
    "CONVERSATION_WORKSPACE_EXECUTION_STATE_AVAILABLE_ATTR",
    "CONVERSATION_WORKSPACE_TASK_ID_ATTR",
    "CONVERSATION_WORKSPACE_TASK_STATUS_ATTR",
    "CONVERSATION_EXECUTION_CWD_ATTR",
    "CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR",
    "current_conversation_task_attributes",
    "conversation_execution_cwd",
    "conversation_runtime_workspace_roots",
    "conversation_transcript_is_authoritative",
]
