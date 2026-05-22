# LLM: Recovery action constants keep state-machine and packet recommendations on one vocabulary.
# 模块用途: 统一恢复动作码，避免状态机和恢复包各自发明一套 recommended_action。

from __future__ import annotations

ACTION_CLOSEOUT = "closeout"
ACTION_DISPATCH = "dispatch"
ACTION_WAIT_FOR_ACCEPTANCE = "wait_for_acceptance"
ACTION_REQUEST_APPROVAL_OR_STOP = "request_approval_or_stop"
ACTION_CHANGE_STRATEGY_OR_STOP = "change_strategy_or_stop"
ACTION_WAIT_FOR_LOCAL_PROGRESS = "wait_for_local_progress"
ACTION_WAIT_OR_OBSERVE = "wait_or_observe"
ACTION_REPAIR_OR_PROBE_CHANNEL = "repair_or_probe_channel"
ACTION_REPAIR_OR_REQUEST_CAPABILITY = "repair_or_request_capability"
ACTION_REPAIR = "repair"
ACTION_TAKEOVER_OR_STOP = "takeover_or_stop"
ACTION_MANUAL_REVIEW = "manual_review"
ACTION_NONE = "none"
ACTION_RESUME_SAME_CASE_AFTER_IDLE_TIMEOUT = "resume_same_case_after_idle_timeout"
ACTION_RESUME_SAME_CASE_AFTER_TIMEOUT = "resume_same_case_after_timeout"
ACTION_REPAIR_THEN_RESUME_SAME_CASE = "repair_then_resume_same_case"

__all__ = [
    "ACTION_CHANGE_STRATEGY_OR_STOP",
    "ACTION_CLOSEOUT",
    "ACTION_DISPATCH",
    "ACTION_MANUAL_REVIEW",
    "ACTION_NONE",
    "ACTION_REPAIR",
    "ACTION_REPAIR_OR_PROBE_CHANNEL",
    "ACTION_REPAIR_OR_REQUEST_CAPABILITY",
    "ACTION_REPAIR_THEN_RESUME_SAME_CASE",
    "ACTION_REQUEST_APPROVAL_OR_STOP",
    "ACTION_RESUME_SAME_CASE_AFTER_IDLE_TIMEOUT",
    "ACTION_RESUME_SAME_CASE_AFTER_TIMEOUT",
    "ACTION_TAKEOVER_OR_STOP",
    "ACTION_WAIT_FOR_ACCEPTANCE",
    "ACTION_WAIT_FOR_LOCAL_PROGRESS",
    "ACTION_WAIT_OR_OBSERVE",
]
