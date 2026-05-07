from __future__ import annotations

"""LLM: deterministic verifier checks for acceptance review records."""

from .models import SubAgentTask
from .reports import AcceptanceReviewFinding


def build_verifier_checks(task: SubAgentTask, created_at: float) -> list[AcceptanceReviewFinding]:
    """Verify evidence packets and parent findings before acceptance."""
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
        _verifier_packets_finding(task, packets_with_refs, created_at),
        _verifier_findings_finding(task, findings_without_chain, created_at),
        _verifier_risks_finding(task, unresolved_risks, created_at),
    ]


def _verifier_packets_finding(task, packets_with_refs, created_at):
    ok = len(packets_with_refs) == len(task.evidence_packets) and bool(packets_with_refs)
    return AcceptanceReviewFinding(
        name="verifier_evidence_packets_traceable",
        ok=ok,
        severity="P1",
        message="verifier 确认 evidence packets 均有 refs。"
        if ok else "verifier 发现存在缺少 refs 的 evidence packet。",
        evidence_path=task.output_json,
        created_at=created_at,
    )


def _verifier_findings_finding(task, findings_without_chain, created_at):
    return AcceptanceReviewFinding(
        name="verifier_findings_cite_evidence",
        ok=not findings_without_chain,
        severity="P1" if findings_without_chain else "P2",
        message="verifier 确认 findings 引用了 evidence。"
        if not findings_without_chain
        else f"verifier 发现 {len(findings_without_chain)} 条 finding 缺少 evidence 引用。",
        evidence_path=task.output_json,
        created_at=created_at,
    )


def _verifier_risks_finding(task, unresolved_risks, created_at):
    return AcceptanceReviewFinding(
        name="verifier_no_unresolved_evidence_risks",
        ok=not unresolved_risks,
        severity="P1",
        message="verifier 未发现未解决 evidence risk。"
        if not unresolved_risks else f"verifier 发现未解决风险: {unresolved_risks[0]}",
        evidence_path=task.output_json,
        created_at=created_at,
    )
