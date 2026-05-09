# LLM: Consistency checks for parent acceptance follow-up control.
# 模块用途: 校验 follow-up 与当前任务/测试报告是否一致，避免 manager 桥接文件继续膨胀。

from __future__ import annotations

"""Staleness and consistency checks for parent acceptance follow-up."""

from pathlib import Path

from .execution_report import load_test_execution_report
from .models import SubAgentTask
from .parent_acceptance_followup_control import (
    BlockedFollowUpControlInput,
    ParentAcceptanceFollowUpControlResult,
    blocked_followup_control_result,
    followup_command_for_action,
    parent_acceptance_followup_ref,
)
from .parent_acceptance_rescue_followup import task_state_rescue_followup


# LLM: followup_consistency_block protects manual apply from stale or mismatched test evidence.
# 函数用途: 在真正 apply/rescue 前校验 follow-up 与当前事实是否一致；不一致就返回阻断结果。
def followup_consistency_block(
    task: SubAgentTask,
    payload: dict,
    status: str,
    action: str,
) -> ParentAcceptanceFollowUpControlResult | None:
    followup = payload.get("followup") if isinstance(payload, dict) else {}
    followup = followup if isinstance(followup, dict) else {}
    reason = _followup_consistency_reason(task, payload, followup)
    if not reason:
        return None
    return blocked_followup_control_result(
        task,
        BlockedFollowUpControlInput(
            status=reason[0],
            action=action or "run_tests",
            message=reason[1],
            recommended_command=followup_command_for_action(task.id, "run_tests"),
        ),
    )


# LLM: _followup_consistency_reason returns compact machine-readable stale evidence reasons.
# 函数用途: 对 run_id、测试报告引用、失败数和文件新鲜度做最小校验，避免旧 follow-up 推进状态。
def _followup_consistency_reason(
    task: SubAgentTask,
    payload: dict,
    followup: dict,
) -> tuple[str, str] | None:
    status = str(followup.get("status") or "")
    action = str(followup.get("action") or "")
    if str(payload.get("run_id") or followup.get("run_id") or "") != task.id:
        return ("stale_followup", "follow-up run_id does not match this task")
    if task_state_rescue_followup(task, status, action):
        return None
    report_path = Path(str(followup.get("test_execution_ref") or ""))
    current_report = Path(task.reports_dir) / "test_execution.json"
    if not report_path.exists():
        return ("missing_test_execution", "follow-up test_execution_ref is missing")
    if report_path.resolve() != current_report.resolve():
        return ("stale_followup", "follow-up test_execution_ref is not the current task report")
    if report_path.stat().st_mtime > parent_acceptance_followup_ref(task).stat().st_mtime + 0.001:
        return ("stale_followup", "test_execution.json is newer than the follow-up audit")
    return _report_consistency_reason(report_path, followup, status, action)


# LLM: _report_consistency_reason compares stored follow-up counts with the current test report.
# 函数用途: 只读取当前 test_execution.json 的摘要，确认 apply/rescue 建议没有过期。
def _report_consistency_reason(
    report_path: Path,
    followup: dict,
    status: str,
    action: str,
) -> tuple[str, str] | None:
    try:
        report = load_test_execution_report(report_path)
    except (OSError, ValueError):
        return ("invalid_test_execution", "current test_execution.json is not readable")
    followup_failed = int(followup.get("test_failed") or 0)
    if followup_failed != report.failed:
        return ("followup_test_mismatch", "follow-up failed count does not match current test report")
    if action == "apply_acceptance" and (status != "ready_for_manual_apply" or report.failed != 0):
        return ("followup_test_mismatch", "apply follow-up requires a passing current test report")
    if action == "plan_rescue" and (status != "needs_manual_rescue" or report.failed <= 0):
        return ("followup_test_mismatch", "rescue follow-up requires failing current test evidence")
    return None
