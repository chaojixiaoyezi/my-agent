from __future__ import annotations

"""LLM: single-task acceptance review helper for SubAgentAcceptanceMixin.

给人看的解释：
验收一条任务时既要读 runner 输出又可能写回状态，拆出后 mixin 保持薄门面。
"""

import time
from pathlib import Path

from .models import SubAgentTask
from .parsing import _dict_list
from .reports import AcceptanceReviewRecord
from .utils import _new_id, _read_json_object


def review_acceptance_task(manager, task: SubAgentTask, *, apply: bool, reviewer: str, note: str) -> AcceptanceReviewRecord:
    """对单个任务执行验收判断，并按需写回状态。"""
    now = time.time()
    before_status = task.status
    before_verification = task.verification_status
    output = _read_json_object(Path(task.output_json))
    runner = _read_json_object(Path(task.runner_result_json))
    findings = manager.acceptance_findings(task, output, runner, now)
    ok = all(item.ok or item.severity == "P2" for item in findings)
    ready = task.status == "AWAITING_ACCEPTANCE" or task.verification_status == "NEEDS_ACCEPTANCE"
    decision = "ACCEPT" if ok else "REJECT"
    message = _acceptance_message(ok, findings)
    applied = False

    if apply and ready:
        _apply_acceptance_decision(manager, task, ok=ok, message=message, now=now)
        applied = True
        manager._append_task_work_log(
            task,
            f"acceptance_review: decision={decision} reviewer={reviewer} message={message}",
        )
    elif apply and not ready:
        message = f"任务当前状态不在等待验收范围内，未写回: status={task.status} verify={task.verification_status}"

    return AcceptanceReviewRecord(
        id=_new_id("accept"),
        run_id=task.id,
        dry_run=not apply,
        applied=applied,
        ok=ok,
        decision=decision,
        message=message,
        before_status=before_status,
        after_status=task.status,
        before_verification_status=before_verification,
        after_verification_status=task.verification_status,
        reviewer=reviewer,
        note=note,
        evidence_count=len(task.evidence),
        test_count=len(_dict_list(output.get("tests", []))),
        artifact_count=len(_dict_list(output.get("artifacts", []))),
        findings=findings,
        evidence_paths=[item.evidence_path for item in findings if item.evidence_path],
        created_at=now,
    )


def _acceptance_message(ok: bool, findings) -> str:
    if ok:
        return "验收通过。"
    failed = [item.message for item in findings if not item.ok and item.severity != "P2"]
    return "验收未通过: " + "；".join(failed[:3])


def _apply_acceptance_decision(manager, task: SubAgentTask, *, ok: bool, message: str, now: float) -> None:
    if ok:
        task.status = "DONE"
        task.verification_status = "VERIFIED"
        task.failure_type = ""
        task.result = task.result or message
    else:
        task.status = "BLOCKED"
        task.verification_status = "FAILED"
        task.failure_type = "acceptance_failed"
        task.result = message
    task.ended_at = now
    task.updated_at = now
    task.heartbeat_at = now
    manager.save(task)
