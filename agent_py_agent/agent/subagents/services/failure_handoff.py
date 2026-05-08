# LLM: Failure handoff helpers create recovery warnings without triggering rescue automatically.
# 模块用途: 根据任务失败/阻塞状态生成失败交接记录，供后续接管和恢复读取。

from __future__ import annotations

"""Failure handoff builders for subagent tasks."""

import time

from ..models import FailureHandoff, SubAgentTask

_HANDOFF_STATUSES = {"FAILED", "ERROR", "TIMEOUT", "BLOCKED"}
_HIGH_RISK_FAILURE_TYPES = {"tool_output_context_overflow", "context_overflow", "blackbox_output_overflow"}


# LLM: refresh_failure_handoff updates task-local failure recovery facts only when risk is visible.
# 函数用途: 在失败或阻塞时刷新 failure handoff，未失败任务保持空记录。
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
        created_at=task.updated_at or task.heartbeat_at or task.created_at or time.time(),
        reserved={
            "schema_name": "subagent_failure_handoff",
            "schema_version": 1,
            "auto_rescue": False,
        },
    )
    return task.failure_handoff


# LLM: should_write_failure_handoff gates recovery handoff creation to visible failure states.
# 函数用途: 判断当前任务是否需要写失败交接，避免正常任务产生误导性恢复包。
def should_write_failure_handoff(task: SubAgentTask) -> bool:
    return task.status in _HANDOFF_STATUSES or bool(task.failure_type)


# LLM: _risk_level maps failure state into a small stable recovery risk vocabulary.
# 函数用途: 根据失败类型和状态生成 risk level，供接管面板排序和提示。
def _risk_level(task: SubAgentTask) -> str:
    if task.failure_type in _HIGH_RISK_FAILURE_TYPES:
        return "high"
    if task.status in {"FAILED", "TIMEOUT"}:
        return "high"
    if task.status == "BLOCKED":
        return "medium"
    return "low"


# LLM: _default_warning provides a safe fallback when the task did not write a summary.
# 函数用途: 为失败交接补默认警告，提醒接管者先读恢复锚点。
def _default_warning(task: SubAgentTask) -> str:
    if task.failure_type in _HIGH_RISK_FAILURE_TYPES:
        return "黑盒或工具输出存在撑爆上下文风险，已停止继续展开。"
    return "子代理未能正常完成，后续接管前请先读取 checkpoint 和 evidence refs。"


# LLM: _avoid_next_time keeps retry guidance factual and task-local.
# 函数用途: 生成避坑建议，避免后续代理机械重复同一失败路径。
def _avoid_next_time(task: SubAgentTask) -> list[str]:
    avoid = ["不要机械重试同一工具调用；先读取 checkpoint、artifact manifest 和失败交接记录。"]
    if task.failure_type in _HIGH_RISK_FAILURE_TYPES:
        avoid.append("不要把黑盒大输出直接塞回 prompt；先外置文件，再读取摘要或切片。")
    avoid.extend(f"先处理 blocker: {item}" for item in task.blockers)
    return list(dict.fromkeys(avoid))


# LLM: _recommended_next_action chooses the first safe recovery move without triggering it.
# 函数用途: 给接管者一个下一步建议，但不自动 rescue 或重试。
def _recommended_next_action(task: SubAgentTask) -> str:
    if task.failure_type in _HIGH_RISK_FAILURE_TYPES:
        return "先外置黑盒输出，再让接管代理读取摘要和 checkpoint。"
    if task.blockers:
        return f"先解决阻塞项：{task.blockers[0]}"
    return "读取 checkpoint、failure_handoff 和 evidence refs 后再决定是否接管。"
