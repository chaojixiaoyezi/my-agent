# LLM: Parent acceptance follow-up records the next manual step after guarded test execution.
# 模块用途: 在父级验收测试执行后生成后续动作审计包，只写 refs 和建议，不自动 apply 或 rescue。
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .execution_report import TestExecutionReport, load_test_execution_report
from .models import SubAgentTask
from .parent_acceptance_followup_control import followup_command_for_action
from .parent_acceptance_next_action import (
    ParentAcceptanceNextAction,
    build_parent_acceptance_next_action,
)


# LLM: ParentAcceptanceAutoFollowUp is the scheduler-facing summary after tests finish.
# 类用途: 汇总测试执行后的下一步建议、失败测试摘要和审计引用；它不执行建议命令。
@dataclass(frozen=True)
class ParentAcceptanceAutoFollowUp:
    """Follow-up decision after parent acceptance test execution."""

    __test__: ClassVar[bool] = False

    run_id: str
    status: str
    action: str
    reason: str
    command: str = ""
    execution_ref: str = ""
    test_execution_ref: str = ""
    test_total: int = 0
    test_failed: int = 0
    failed_tests: list[dict[str, str]] = field(default_factory=list)
    next_action: ParentAcceptanceNextAction | None = None
    mutates_task_state: bool = False
    next_action_mutates_task_state: bool = False
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict preserves refs-only JSON while flattening the nested next-action dataclass.
    # 函数用途: 把 follow-up 结果转换为 JSON 字典；只包含摘要和路径引用，不读取正文。
    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        action = self.next_action
        payload["next_action"] = action.to_dict() if action is not None else {}
        return payload


# LLM: build_parent_acceptance_auto_followup turns a completed test report into a manual next-step package.
# 函数用途: 根据最新测试报告重新计算父级下一动作，并写 follow-up 审计文件；不改 task 状态。
def build_parent_acceptance_auto_followup(
    task: SubAgentTask,
    *,
    workspace_root: str | Path,
    execution_ref: str,
    test_execution_ref: str,
) -> ParentAcceptanceAutoFollowUp:
    report = _load_report(test_execution_ref)
    action = build_parent_acceptance_next_action(task, workspace_root=workspace_root)
    followup = ParentAcceptanceAutoFollowUp(
        run_id=task.id,
        status=_followup_status(action),
        action=action.action,
        reason=action.reason,
        # LLM: Keep post-test prompts pointed at the controlled follow-up gate, not legacy direct apply.
        command=followup_command_for_action(task.id, action.action) or action.command,
        execution_ref=execution_ref,
        test_execution_ref=test_execution_ref,
        test_total=report.total_tests,
        test_failed=report.failed,
        failed_tests=_failed_tests(report),
        next_action=action,
        mutates_task_state=False,
        next_action_mutates_task_state=action.mutates_task_state,
        reserved={
            "refs_only": True,
            "reads_artifact_bodies": False,
            "mutates_task_state": False,
            "auto_applies_acceptance": False,
            "auto_starts_rescue": False,
            "requires_explicit_next_step": True,
        },
    )
    write_parent_acceptance_auto_followup_file(task, followup)
    return followup


# LLM: write_parent_acceptance_auto_followup_file persists the follow-up audit beside execution reports.
# 函数用途: 写入 `parent_acceptance_auto_followup.json`；只保存下一步建议和测试摘要。
def write_parent_acceptance_auto_followup_file(
    task: SubAgentTask,
    followup: ParentAcceptanceAutoFollowUp,
    *,
    generated_at: float | None = None,
) -> Path:
    path = Path(task.reports_dir) / "parent_acceptance_auto_followup.json"
    payload = {
        "schema": "parent_acceptance_auto_followup.v1",
        "generated_at": generated_at if generated_at is not None else time.time(),
        "dry_run": True,
        "run_id": task.id,
        "followup": followup.to_dict(),
        "reserved": {
            "refs_only": True,
            "mutates_task_state": False,
            "future_apply_supported": True,
            "future_rescue_supported": True,
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


# LLM: _load_report keeps follow-up construction tolerant of missing or invalid test report refs.
# 函数用途: 读取测试执行报告；路径缺失时返回空报告，让上层得到 blocked follow-up 而不是崩溃。
def _load_report(test_execution_ref: str) -> TestExecutionReport:
    path = Path(test_execution_ref)
    if not test_execution_ref or not path.exists():
        return TestExecutionReport.from_dict({})
    return load_test_execution_report(path)


# LLM: _followup_status converts next-action vocabulary into scheduler-friendly buckets.
# 函数用途: 把下一动作归类为人工 apply、人工救援、人工确认等状态，便于 dispatch/watch 展示。
def _followup_status(action: ParentAcceptanceNextAction) -> str:
    if action.action == "apply_acceptance":
        return "ready_for_manual_apply"
    if action.action == "plan_rescue":
        return "needs_manual_rescue"
    if action.action == "request_human_confirmation":
        return "needs_human_confirmation"
    if action.action == "run_tests":
        return "needs_manual_tests"
    # LLM: Patch review is a manual gate after tests pass, distinct from acceptance apply.
    if action.action == "review_patches":
        return "needs_patch_review"
    if action.action == "none":
        return "complete"
    return "blocked"


# LLM: _failed_tests extracts compact failure hints without copying stdout or stderr bodies.
# 函数用途: 汇总失败测试的名称、命令、方法和错误摘要，避免把大输出写进 follow-up。
def _failed_tests(report: TestExecutionReport) -> list[dict[str, str]]:
    failed: list[dict[str, str]] = []
    for record in report.records:
        if record.passed:
            continue
        failed.append(
            {
                "name": record.test_name,
                "command": record.command,
                "validation_method": record.validation_method,
                "error": record.error,
            }
        )
    return failed
