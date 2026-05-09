# LLM: Rescue follow-up helpers for failed or blocked parent acceptance tasks.
# 模块用途: 处理没有测试 follow-up 的失败/阻塞任务，把它们转换成受控接管建议而不挤大 manager 桥接文件。

from __future__ import annotations

"""Helpers for task-state based parent acceptance rescue follow-up."""

from pathlib import Path

from .models import SubAgentTask
from .parent_acceptance_followup_control import (
    ParentAcceptanceFollowUpControlResult,
    followup_command_for_action,
    parent_acceptance_followup_control_ref,
    parent_acceptance_followup_ref,
)
from .parent_acceptance_next_action import build_parent_acceptance_next_action


# LLM: missing_followup_rescue_preview makes blocked runner handoff actionable without fake tests.
# 函数用途: 对已失败/阻塞且没有测试 follow-up 的任务，直接返回接管建议；不写文件、不改状态。
def missing_followup_rescue_preview(
    task: SubAgentTask,
    *,
    workspace_root: Path,
) -> ParentAcceptanceFollowUpControlResult | None:
    payload = missing_followup_rescue_payload(task, workspace_root=workspace_root)
    if not payload:
        return None
    followup = payload["followup"]
    return ParentAcceptanceFollowUpControlResult(
        run_id=task.id,
        status=str(followup["status"]),
        action=str(followup["action"]),
        applied=False,
        ok=True,
        message=str(followup["reason"]),
        followup_ref=str(parent_acceptance_followup_ref(task)),
        control_ref=str(parent_acceptance_followup_control_ref(task)),
        recommended_command=followup_command_for_action(task.id, "plan_rescue"),
        evidence_refs=_rescue_followup_refs(followup),
        mutates_task_state=False,
        reserved={"synthetic_from_task_state": True},
    )


# LLM: missing_followup_rescue_payload normalizes failed/blocked tasks into a rescue follow-up shape.
# 函数用途: 没有 test_execution 的 runner 失败也能复用 follow-up control；不会伪造测试通过或失败数量。
def missing_followup_rescue_payload(task: SubAgentTask, *, workspace_root: Path) -> dict:
    if parent_acceptance_followup_ref(task).exists():
        return {}
    action = build_parent_acceptance_next_action(task, workspace_root=workspace_root)
    if action.action != "plan_rescue":
        return {}
    return {
        "run_id": task.id,
        "followup": {
            "run_id": task.id,
            "status": "needs_manual_rescue",
            "action": "plan_rescue",
            "reason": action.reason,
            "command": followup_command_for_action(task.id, "plan_rescue"),
            "test_execution_ref": "",
            "test_failed": 0,
            "next_action": action.to_dict(),
            "reserved": {
                "refs_only": True,
                "synthetic_from_task_state": True,
                "requires_explicit_next_step": True,
            },
        },
    }


# LLM: task_state_rescue_followup lets currently failed tasks enter takeover without failed test evidence.
# 函数用途: 区分“任务状态已失败/阻塞”的救援入口和“测试失败后救援”；前者可直接进入接管门。
def task_state_rescue_followup(task: SubAgentTask, status: str, action: str) -> bool:
    task_status = str(getattr(task, "status", "") or "").upper()
    verification = str(getattr(task, "verification_status", "") or "").upper()
    failed_task = (
        bool(getattr(task, "failure_type", "") or "")
        or task_status in {"FAILED", "ERROR", "TIMEOUT", "BLOCKED"}
        or verification == "FAILED"
    )
    return failed_task and status == "needs_manual_rescue" and action == "plan_rescue"


# LLM: _rescue_followup_refs keeps synthetic rescue previews traceable.
# 函数用途: 从 next_action 中提取失败交接和接管准备引用，供 CLI/report 展示。
def _rescue_followup_refs(followup: dict) -> list[str]:
    action = followup.get("next_action") if isinstance(followup, dict) else {}
    action = action if isinstance(action, dict) else {}
    return [
        str(action.get(key) or "")
        for key in ("failure_handoff_ref", "takeover_readiness_ref")
        if str(action.get(key) or "")
    ]
