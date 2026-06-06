
from __future__ import annotations

from ..common.value_parsing import text_or_sequence_strings
from .models import EvidencePacket, Finding, SubAgentTask, VerificationEvidence
from .result_artifact_evidence import normalize_artifact_ref
from .utils import _merge_list, _new_id


def process_evidence_items(parsed, task: SubAgentTask, now: float) -> int:
    count = 0
    for item in parsed.evidence:
        summary = str(item.get("summary", "")).strip()
        if not summary:
            continue
        task.evidence.append(
            VerificationEvidence(
                kind=str(item.get("kind", "note") or "note"),
                summary=summary,
                command=str(item.get("command", "") or ""),
                path=str(item.get("path", "") or ""),
                url=str(item.get("url", "") or ""),
                ok=_evidence_ok(item),
                created_at=now,
            )
        )
        count += 1
    return count


def process_evidence_packets(parsed, task: SubAgentTask, now: float) -> list[dict[str, object]]:
    packets: list[dict[str, object]] = []
    for item in parsed.evidence_packets:
        packet = _evidence_packet_from_item(task, item, now)
        if packet is None:
            continue
        task.evidence_packets.append(packet)
        task.evidence_refs = _merge_list(task.evidence_refs, packet.evidence_refs)
        task.artifact_refs = _merge_list(task.artifact_refs, packet.artifact_refs)
        packets.append(_evidence_packet_dict(packet))
    return packets


def process_findings(parsed, task: SubAgentTask, now: float) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for item in parsed.findings:
        finding = _finding_from_item(item, now)
        if finding is None:
            continue
        task.findings.append(finding)
        task.evidence_refs = _merge_list(task.evidence_refs, finding.evidence_refs)
        findings.append(_finding_dict(finding))
    return findings


def _evidence_packet_from_item(task: SubAgentTask, item: dict[str, object], now: float) -> EvidencePacket | None:
    claim = str(item.get("claim", "") or "").strip()
    evidence_refs = _string_refs(item.get("evidence_refs", []))
    artifact_refs = [normalize_artifact_ref(task, ref) for ref in _string_refs(item.get("artifact_refs", []))]
    if not claim or not (evidence_refs or artifact_refs):
        return None
    return EvidencePacket(
        id=str(item.get("id", "") or _new_id("evpkt")),
        claim=claim,
        checked_scope=str(item.get("checked_scope", "") or ""),
        evidence_refs=evidence_refs,
        artifact_refs=artifact_refs,
        counter_evidence_refs=_string_refs(item.get("counter_evidence_refs", [])),
        confidence=_float_confidence(item.get("confidence", 0.0)),
        unresolved_risks=_string_refs(item.get("unresolved_risks", [])),
        created_at=now,
    )


def _finding_from_item(item: dict[str, object], now: float) -> Finding | None:
    claim = str(item.get("claim", "") or "").strip()
    evidence_packet_ids = _string_refs(item.get("evidence_packet_ids", []))
    evidence_refs = _string_refs(item.get("evidence_refs", []))
    if not claim or not (evidence_packet_ids or evidence_refs):
        return None
    return Finding(
        id=str(item.get("id", "") or _new_id("finding")),
        claim=claim,
        status=str(item.get("status", "OPEN") or "OPEN"),
        severity=str(item.get("severity", "") or ""),
        confidence=_float_confidence(item.get("confidence", 0.0)),
        evidence_packet_ids=evidence_packet_ids,
        evidence_refs=evidence_refs,
        counter_evidence_refs=_string_refs(item.get("counter_evidence_refs", [])),
        created_at=now,
    )


def _evidence_packet_dict(packet: EvidencePacket) -> dict[str, object]:
    return {
        "id": packet.id,
        "claim": packet.claim,
        "checked_scope": packet.checked_scope,
        "evidence_refs": packet.evidence_refs,
        "artifact_refs": packet.artifact_refs,
        "counter_evidence_refs": packet.counter_evidence_refs,
        "confidence": packet.confidence,
        "unresolved_risks": packet.unresolved_risks,
        "created_at": packet.created_at,
    }


def _finding_dict(finding: Finding) -> dict[str, object]:
    return {
        "id": finding.id,
        "claim": finding.claim,
        "status": finding.status,
        "severity": finding.severity,
        "confidence": finding.confidence,
        "evidence_packet_ids": finding.evidence_packet_ids,
        "evidence_refs": finding.evidence_refs,
        "counter_evidence_refs": finding.counter_evidence_refs,
        "created_at": finding.created_at,
    }


def _evidence_ok(item: dict[str, object]) -> bool:
    raw_ok = bool(item.get("ok", True))
    if raw_ok:
        return True
    kind = str(item.get("kind", "") or "").lower()
    pattern = str(item.get("content_pattern", "") or "").strip()
    mode = str(item.get("match_mode") or "").strip().lower()
    explicit_absent = bool(item.get("expect_absent"))
    return kind == "content_check" and bool(pattern) and (explicit_absent or mode == "not_contains")


def _string_refs(value: object) -> list[str]:
    return text_or_sequence_strings(value)


def _float_confidence(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
