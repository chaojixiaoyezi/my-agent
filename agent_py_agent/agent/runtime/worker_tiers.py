from __future__ import annotations

# LLM: Worker tier contracts separate TaskAgent, weak subagent, and pure tool execution.
# 模块用途: 根据任务复杂度和反馈需求选择执行层级，并构造结构化 worker 上下文。
from enum import StrEnum
from typing import Any


 # LLM: WorkerTier is persisted into TaskCard metadata and must remain stable.
 # 类用途: 表示任务应该由工具、弱子代理或后台 TaskAgent 执行。
class WorkerTier(StrEnum):
    TOOL_WORKER = "tool_worker"
    WEAK_SUBAGENT = "weak_subagent"
    TASK_AGENT = "task_agent"


 # LLM: choose_worker_tier maps machine facts to an execution tier without prompt parsing.
 # 函数用途: 根据复杂度、用户记忆和反馈需求选择 worker 层级。
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


 # LLM: build_worker_context creates the structured context envelope for a worker tier.
 # 函数用途: 为不同 worker 层级生成可落盘、可测试的上下文合同。
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
