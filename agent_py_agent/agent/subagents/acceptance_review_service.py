from __future__ import annotations

"""LLM: single-task acceptance review helper for SubAgentAcceptanceMixin.

给人看的解释：
验收一条任务时既要读 runner 输出又可能写回状态，拆出后 mixin 保持薄门面。
"""

import time
from dataclasses import dataclass, replace
from pathlib import Path

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


def review_acceptance_task(manager, request: AcceptanceReviewRequest) -> AcceptanceReviewRecord:
    """对单个任务执行验收判断，并按需写回状态。"""
    task = request.task
    now = request.now if request.now is not None else time.time()
    before_status = task.status
    before_verification = task.verification_status
    output = _read_json_object(Path(task.output_json))
    runner = _read_json_object(Path(task.runner_result_json))
    findings = manager.acceptance_findings(task, output, runner, now)
    verifier_checks = _build_verifier_checks(task, now)
    review_checks = [*findings, *verifier_checks]
    ok = all(item.ok or item.severity == "P2" for item in review_checks)
    ready = task.status == "AWAITING_ACCEPTANCE" or task.verification_status == "NEEDS_ACCEPTANCE"
    decision = "ACCEPT" if ok else "REJECT"
    message = _acceptance_message(ok, review_checks)
    applied = False

    if request.apply and ready:
        _apply_acceptance_decision(manager, task, ok=ok, message=message, now=now)
        applied = True
        manager._append_task_work_log(
            task,
            f"acceptance_review: decision={decision} reviewer={request.reviewer} message={message}",
        )
    elif request.apply and not ready:
        message = f"任务当前状态不在等待验收范围内，未写回: status={task.status} verify={task.verification_status}"

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
        test_count=len(_dict_list(output.get("tests", []))),
        artifact_count=len(_dict_list(output.get("artifacts", []))),
        findings=findings,
        worker_claims=_worker_claims(task, output, runner),
        evidence_facts=_evidence_facts(task, output),
        parent_conclusions=_parent_conclusions(decision, message, review_checks),
        verifier_checks=verifier_checks,
        evidence_paths=[item.evidence_path for item in review_checks if item.evidence_path],
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


def _build_verifier_checks(task: SubAgentTask, created_at: float) -> list[AcceptanceReviewFinding]:
    """LLM: Deterministic verifier pass over evidence packets and parent findings."""
    packet_ids = {item.id for item in task.evidence_packets if item.id}
    packets_with_refs = [
        item for item in task.evidence_packets if item.evidence_refs or item.artifact_refs
    ]
    unresolved_risks = [
        risk
        for packet in task.evidence_packets
        for risk in packet.unresolved_risks
        if str(risk).strip()
    ]
    findings_without_chain = [
        item
        for item in task.findings
        if not item.evidence_refs
        and not any(packet_id in packet_ids for packet_id in item.evidence_packet_ids)
    ]
    return [
        AcceptanceReviewFinding(
            name="verifier_evidence_packets_traceable",
            ok=len(packets_with_refs) == len(task.evidence_packets) and bool(packets_with_refs),
            severity="P1",
            message="verifier 确认 evidence packets 均有 refs。"
            if len(packets_with_refs) == len(task.evidence_packets) and packets_with_refs
            else "verifier 发现存在缺少 refs 的 evidence packet。",
            evidence_path=task.output_json,
            created_at=created_at,
        ),
        AcceptanceReviewFinding(
            name="verifier_findings_cite_evidence",
            ok=not findings_without_chain,
            severity="P1" if findings_without_chain else "P2",
            message="verifier 确认 findings 引用了 evidence。"
            if not findings_without_chain else f"verifier 发现 {len(findings_without_chain)} 条 finding 缺少 evidence 引用。",
            evidence_path=task.output_json,
            created_at=created_at,
        ),
        AcceptanceReviewFinding(
            name="verifier_no_unresolved_evidence_risks",
            ok=not unresolved_risks,
            severity="P1",
            message="verifier 未发现未解决 evidence risk。"
            if not unresolved_risks else f"verifier 发现未解决风险: {unresolved_risks[0]}",
            evidence_path=task.output_json,
            created_at=created_at,
        ),
    ]
