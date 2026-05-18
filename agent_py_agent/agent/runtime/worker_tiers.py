from __future__ import annotations

from enum import StrEnum
from typing import Any


class WorkerTier(StrEnum):
    TOOL_WORKER = "tool_worker"
    WEAK_SUBAGENT = "weak_subagent"
    TASK_AGENT = "task_agent"


def choose_worker_tier(
    *,
    complexity: str,
    requires_user_memory: bool,
    requires_feedback: bool,
) -> WorkerTier:
    normalized = complexity.lower().strip()
    if normalized == "tool":
        return WorkerTier.TOOL_WORKER
    if normalized in {"large", "complex"} or requires_user_memory or requires_feedback:
        return WorkerTier.TASK_AGENT
    return WorkerTier.WEAK_SUBAGENT


def build_worker_context(
    tier: WorkerTier,
    *,
    task_id: str,
    goal: str,
    user_id: str,
    session_id: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if tier == WorkerTier.TASK_AGENT:
        return {
            "worker_tier": tier.value,
            "task_id": task_id,
            "goal": goal,
            "user_id": user_id,
            "session_id": session_id,
            "context_bundle_level": "full_task",
            "feedback_channel": f"session:{session_id}",
            "can_request_user_input": True,
            "metadata": dict(metadata or {}),
        }
    if tier == WorkerTier.WEAK_SUBAGENT:
        return {
            "worker_tier": tier.value,
            "task_id": task_id,
            "goal": goal,
            "user_id": user_id,
            "session_id": session_id,
            "context_bundle_level": "narrow_task",
            "feedback_channel": None,
            "can_request_user_input": False,
            "metadata": dict(metadata or {}),
        }
    return {
        "worker_tier": tier.value,
        "task_id": task_id,
        "goal": goal,
        "user_id": user_id,
        "session_id": session_id,
        "context_bundle_level": "tool_only",
        "feedback_channel": None,
        "can_request_user_input": False,
        "metadata": dict(metadata or {}),
    }
