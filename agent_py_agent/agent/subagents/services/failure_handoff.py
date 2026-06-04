
from __future__ import annotations

"""Failure handoff builders for subagent tasks."""

import time

from ..models import FailureHandoff, SubAgentTask

_HANDOFF_STATUSES = {"FAILED", "ERROR", "TIMEOUT", "BLOCKED"}
_HIGH_RISK_FAILURE_TYPES = {"tool_output_context_overflow", "context_overflow", "blackbox_output_overflow"}


def refresh_failure_handoff(task: SubAgentTask) -> FailureHandoff:
    if not should_write_failure_handoff(task):
        task.failure_handoff = FailureHandoff()
        return task.failure_handoff
    task.failure_handoff = FailureHandoff(
        run_id=task.id,
        status=task.status,
        failure_type=task.failure_type or task.status.lower(),
        risk_level=_risk_level(task),
        warning=task.latest_summary or _default_warning(task),
        last_safe_checkpoint_ref=task.checkpoint_json or task.checkpoint_ref,
        artifact_refs=list(dict.fromkeys(task.artifact_refs)),
        evidence_refs=list(dict.fromkeys(task.evidence_refs)),
        avoid_next_time=_avoid_next_time(task),
        recommended_next_action=_recommended_next_action(task),
        auto_rescue=False,
        created_at=task.updated_at or task.heartbeat_at or task.created_at or time.time(),
    )
    return task.failure_handoff


def should_write_failure_handoff(task: SubAgentTask) -> bool:
    return task.status in _HANDOFF_STATUSES or bool(task.failure_type)


def _risk_level(task: SubAgentTask) -> str:
    if task.failure_type in _HIGH_RISK_FAILURE_TYPES:
        return "high"
    if task.status in {"FAILED", "TIMEOUT"}:
        return "high"
    if task.status == "BLOCKED":
        return "medium"
    return "low"


def _default_warning(task: SubAgentTask) -> str:
    if task.failure_type in _HIGH_RISK_FAILURE_TYPES:
        return "黑盒或工具输出存在撑爆上下文风险，已停止继续展开。"
    return "子代理未能正常完成，后续接管前请先读取 checkpoint 和 evidence refs。"


def _avoid_next_time(task: SubAgentTask) -> list[str]:
    avoid = ["不要机械重试同一工具调用；先读取 checkpoint、artifact manifest 和失败交接记录。"]
    if task.failure_type in _HIGH_RISK_FAILURE_TYPES:
        avoid.append("不要把黑盒大输出直接塞回 prompt；先外置文件，再读取摘要或切片。")
    avoid.extend(f"先处理 blocker: {item}" for item in task.blockers)
    return list(dict.fromkeys(avoid))


def _recommended_next_action(task: SubAgentTask) -> str:
    if task.failure_type in _HIGH_RISK_FAILURE_TYPES:
        return "先外置黑盒输出，再让接管代理读取摘要和 checkpoint。"
    if task.blockers:
        return f"先解决阻塞项：{task.blockers[0]}"
    return "读取 checkpoint、failure_handoff 和 evidence refs 后再决定是否接管。"
