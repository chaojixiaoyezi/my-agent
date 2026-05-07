from __future__ import annotations

"""LLM: single-task acceptance review helper for SubAgentAcceptanceMixin.

给人看的解释：
验收一条任务时既要读 runner 输出又可能写回状态，拆出后 mixin 保持薄门面。
"""

import time
from dataclasses import dataclass, replace
from pathlib import Path

from .acceptance_review_verifier import build_verifier_checks
from .models import SubAgentTask
from .parsing import _dict_list
from .reports import AcceptanceReviewFinding, AcceptanceReviewRecord
from .utils import _new_id, _read_json_object


@dataclass(frozen=True)
class AcceptanceReviewOptions:
    """Options bundle for acceptance review report entrypoints."""

    # LLM: report-level acceptance options stay bundled while per-task review uses a request.
    apply: bool = False
    reviewer: str = "parent"
    note: str = ""
    limit: int = 0
    now: float | None = None

    @classmethod
    def from_values(
        cls,
        options: AcceptanceReviewOptions | None = None,
        *,
        apply: bool | None = None,
        reviewer: str | None = None,
        note: str | None = None,
        limit: int | None = None,
        now: float | None = None,
    ):
        base = options or cls()
        updates = {
            "apply": apply,
            "reviewer": reviewer,
            "note": note,
            "limit": limit,
            "now": now,
        }
        clean = {key: value for key, value in updates.items() if value is not None}
        return replace(base, **clean)


def acceptance_review_options(
    options: AcceptanceReviewOptions | None = None,
    *,
    apply: bool = False,
    reviewer: str = "parent",
    note: str = "",
    limit: int = 0,
) -> AcceptanceReviewOptions:
    """Coerce legacy fields into the report-level acceptance options bundle."""

    if options is not None and (apply, reviewer, note, limit) == (False, "parent", "", 0):
        return options
    return AcceptanceReviewOptions.from_values(
        options,
        apply=apply,
        reviewer=reviewer,
        note=note,
        limit=limit,
    )


@dataclass(frozen=True)
class AcceptanceReviewRequest:
    """LLM: Bundle one acceptance review request so future gate fields do not widen signatures."""

    task: SubAgentTask
    apply: bool = False
    reviewer: str = "parent"
    note: str = ""
    now: float | None = None


@dataclass(frozen=True)
class AcceptanceReviewInputs:
    """LLM: loaded runner data for a single acceptance review."""

    output: dict
    runner: dict
    findings: list[AcceptanceReviewFinding]
    verifier_checks: list[AcceptanceReviewFinding]


@dataclass(frozen=True)
class AcceptanceDecisionRequest:
    """LLM: bundle acceptance status writes so mutation arguments do not drift."""

    ok: bool
    message: str
    now: float


def review_acceptance_task(manager, request: AcceptanceReviewRequest) -> AcceptanceReviewRecord:
    """对单个任务执行验收判断，并按需写回状态。"""
    task = request.task
    now = request.now if request.now is not None else time.time()
    before_status = task.status
    before_verification = task.verification_status
    inputs = _acceptance_review_inputs(manager, task, now)
    review_checks = [*inputs.findings, *inputs.verifier_checks]
    ok = all(item.ok or item.severity == "P2" for item in review_checks)
    ready = task.status == "AWAITING_ACCEPTANCE" or task.verification_status == "NEEDS_ACCEPTANCE"
    decision = "ACCEPT" if ok else "REJECT"
    message = _acceptance_message(ok, review_checks)
    applied = False

    if request.apply and ready:
        _apply_acceptance_decision(manager, task, request=AcceptanceDecisionRequest(ok, message, now))
        applied = True
        manager._append_task_work_log(
            task,
            f"acceptance_review: decision={decision} reviewer={request.reviewer} message={message}",
        )
    elif request.apply and not ready:
        message = f"任务当前状态不在等待验收范围内，未写回: status={task.status} verify={task.verification_status}"

    return _acceptance_record(
        request,
        inputs,
        review_checks=review_checks,
        decision=decision,
        message=message,
        before_status=before_status,
        before_verification=before_verification,
        applied=applied,
        ok=ok,
        now=now,
    )


def _acceptance_review_inputs(manager, task: SubAgentTask, now: float) -> AcceptanceReviewInputs:
    output = _read_json_object(Path(task.output_json))
    runner = _read_json_object(Path(task.runner_result_json))
    return AcceptanceReviewInputs(
        output=output,
        runner=runner,
        findings=manager.acceptance_findings(task, output, runner, now),
        verifier_checks=build_verifier_checks(task, now),
    )


def _acceptance_record(
    request: AcceptanceReviewRequest,
    inputs: AcceptanceReviewInputs,
    *,
    review_checks: list[AcceptanceReviewFinding],
    decision: str,
    message: str,
    before_status: str,
    before_verification: str,
    applied: bool,
    ok: bool,
    now: float,
) -> AcceptanceReviewRecord:
    task = request.task
    return AcceptanceReviewRecord(
        id=_new_id("accept"),
        run_id=task.id,
        dry_run=not request.apply,
        applied=applied,
        ok=ok,
        decision=decision,
        message=message,
        before_status=before_status,
        after_status=task.status,
        before_verification_status=before_verification,
        after_verification_status=task.verification_status,
        reviewer=request.reviewer,
        note=request.note,
        evidence_count=len(task.evidence),
        test_count=len(_dict_list(inputs.output.get("tests", []))),
        artifact_count=len(_dict_list(inputs.output.get("artifacts", []))),
        findings=inputs.findings,
        worker_claims=_worker_claims(task, inputs.output, inputs.runner),
        evidence_facts=_evidence_facts(task, inputs.output),
        parent_conclusions=_parent_conclusions(decision, message, review_checks),
        verifier_checks=inputs.verifier_checks,
        evidence_paths=[item.evidence_path for item in review_checks if item.evidence_path],
        created_at=now,
    )


def _acceptance_message(ok: bool, findings) -> str:
    if ok:
        return "验收通过。"
    failed = [item.message for item in findings if not item.ok and item.severity != "P2"]
    return "验收未通过: " + "；".join(failed[:3])


def _apply_acceptance_decision(
    manager,
    task: SubAgentTask,
    *,
    request: AcceptanceDecisionRequest,
) -> None:
    ok = request.ok
    message = request.message
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
    task.ended_at = request.now
    task.updated_at = request.now
    task.heartbeat_at = request.now
    manager.save(task)


def _worker_claims(task: SubAgentTask, output: dict, runner: dict) -> list[str]:
    """LLM: Preserve worker self-report separately from parent conclusions."""
    claims: list[str] = []
    for value in [
        output.get("summary"),
        runner.get("message"),
        task.latest_summary,
        task.result,
    ]:
        text = str(value or "").strip()
        if text and text not in claims:
            claims.append(text)
    status = str(output.get("status") or task.status or "").strip()
    if status:
        claims.append(f"worker_status={status}")
    return claims[:8]


def _evidence_facts(task: SubAgentTask, output: dict) -> list[str]:
    tests = _dict_list(output.get("tests", []))
    artifacts = _dict_list(output.get("artifacts", []))
    facts = [
        f"verification_evidence={len(task.evidence)}",
        f"evidence_packets={len(task.evidence_packets)}",
        f"findings={len(task.findings)}",
        f"tests={len(tests)}",
        f"artifacts={len(artifacts)}",
    ]
    for packet in task.evidence_packets[:5]:
        refs = [*packet.evidence_refs, *packet.artifact_refs]
        facts.append(f"packet:{packet.id or 'none'} claim={packet.claim} refs={len(refs)}")
    return facts


def _parent_conclusions(
    decision: str,
    message: str,
    checks: list[AcceptanceReviewFinding],
) -> list[str]:
    failed = [item for item in checks if not item.ok and item.severity != "P2"]
    conclusions = [f"decision={decision}", message]
    conclusions.extend(f"{item.severity}:{item.name}" for item in failed[:5])
    return conclusions
