from __future__ import annotations

"""Finding-to-case persistence on top of the local log-analysis store."""

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..models import CaseRecord, EvidenceRef, Finding, utc_now_iso
from ..storage.local_store import LocalLogStore


Case = CaseRecord


class CaseStore:
    def __init__(
        self,
        root: str | Path | LocalLogStore,
        *,
        min_case_confidence: float = 0.6,
        merge_window_minutes: int = 15,
        search_store: Any | None = None,
    ) -> None:
        self.store = root if isinstance(root, LocalLogStore) else LocalLogStore(root)
        self.min_case_confidence = min_case_confidence
        self.merge_window_minutes = merge_window_minutes
        self.search_store = search_store

    @property
    def root(self) -> Path:
        return self.store.root

    def record_finding(self, finding: Finding | Mapping[str, Any]) -> CaseRecord | None:
        normalized = _ensure_finding(finding)
        self.store.upsert_finding(normalized)
        if (normalized.confidence if normalized.confidence is not None else normalized.risk_score) < self.min_case_confidence:
            return None
        return self.upsert_case_for_finding(normalized)

    def record_findings(self, findings: Sequence[Finding | Mapping[str, Any]]) -> list[CaseRecord]:
        cases: list[CaseRecord] = []
        seen: set[str] = set()
        for finding in findings:
            case = self.record_finding(finding)
            if case is None or case.case_id in seen:
                continue
            seen.add(case.case_id)
            cases.append(case)
        return cases

    def upsert_case_for_finding(self, finding: Finding | Mapping[str, Any]) -> CaseRecord:
        normalized = _ensure_finding(finding)
        dedup_key = dedup_key_for_finding(normalized, self.merge_window_minutes)
        case = self.get_case_by_dedup_key(dedup_key) or self._find_merge_candidate(normalized, dedup_key)
        if case is None:
            case = CaseRecord(
                case_id=_stable_case_id(dedup_key),
                title=_title_for_finding(normalized),
                priority=priority_for_score(normalized.risk_score),
                risk_score=normalized.risk_score,
                dedup_key=dedup_key,
                created_at=utc_now_iso(),
                updated_at=utc_now_iso(),
                case_type=_case_type_for_findings([normalized]),
            )
        self._merge_finding(case, normalized)
        self.store.upsert_case(case)
        self._index_case(case)
        return case

    def save_case(self, case: CaseRecord | Mapping[str, Any]) -> None:
        self.store.upsert_case(_ensure_case(case))

    def get_case(self, case_id: str) -> CaseRecord | None:
        payload = self.store.get_case(case_id)
        return CaseRecord.from_dict(payload) if payload else None

    def get_case_by_dedup_key(self, dedup_key: str) -> CaseRecord | None:
        for case in self.list_cases():
            if case.dedup_key == dedup_key:
                return case
        return None

    def list_cases(self) -> list[CaseRecord]:
        cases = [CaseRecord.from_dict(item) for item in self.store.list_cases()]
        return sorted(cases, key=lambda item: (item.priority, -item.risk_score, item.updated_at))

    def load_findings(self) -> list[Finding]:
        return [Finding.from_dict(item) for item in self.store.list_findings()]

    def _find_merge_candidate(self, finding: Finding, dedup_key: str) -> CaseRecord | None:
        victim = _primary_victim(finding)
        attacker = _primary_attacker(finding)
        bucket = _bucket_from_dedup_key(dedup_key)
        if not victim or not bucket:
            return None
        for case in self.list_cases():
            if case.status in {"CLOSED", "SUPPRESSED"}:
                continue
            if _bucket_from_dedup_key(case.dedup_key) != bucket:
                continue
            case_victims = set(_entity_values(case.entities, "victim_ip", "dst_ip", "host", "asset_id"))
            if victim not in case_victims:
                continue
            case_attackers = set(_entity_values(case.entities, "attacker_ip"))
            if not attacker or not case_attackers or attacker in case_attackers:
                return case
        return None

    def _merge_finding(self, case: CaseRecord, finding: Finding) -> None:
        case.risk_score = max(case.risk_score, finding.risk_score)
        case.priority = min_priority(case.priority, priority_for_score(case.risk_score))
        case.finding_refs = _unique([*case.finding_refs, finding.finding_id])
        case.evidence_refs = _merge_evidence_refs(case.evidence_refs, finding.evidence_refs)
        case.entities = _merge_entities(case.entities, finding.entities)
        case.facts = _unique([*case.facts, f"{finding.detector_id} finding observed"])
        if finding.hypothesis:
            case.inferences = _unique([*case.inferences, finding.hypothesis])
        case.gaps = _unique([*case.gaps, *finding.gaps])
        case.next_queries = _unique([*case.next_queries, *finding.next_queries])
        attributes = case.attributes if isinstance(case.attributes, dict) else {}
        summaries = list(attributes.get("finding_summaries", []))
        if not any(item.get("finding_id") == finding.finding_id for item in summaries if isinstance(item, dict)):
            summaries.append(finding.to_dict())
        case.attributes = {**attributes, "finding_summaries": summaries}
        case.case_type = _case_type_for_findings([Finding.from_dict(item) for item in summaries if isinstance(item, dict)])
        case.updated_at = utc_now_iso()

    def _index_case(self, case: CaseRecord) -> None:
        if self.search_store is None or not hasattr(self.search_store, "upsert_record"):
            return
        self.search_store.upsert_record(
            source_type="log_analysis_case",
            source_id=case.case_id,
            title=case.title or case.case_id,
            content=case.to_json(),
            metadata={
                "priority": case.priority,
                "risk_score": case.risk_score,
                "dedup_key": case.dedup_key,
                "finding_count": len(case.finding_refs),
            },
        )


def dedup_key_for_finding(finding: Finding | Mapping[str, Any], window_minutes: int = 15) -> str:
    normalized = _ensure_finding(finding)
    attacker = _primary_attacker(normalized) or "unknown"
    victim = _primary_victim(normalized) or "unknown"
    bucket = _time_bucket(normalized.window[0] if normalized.window else "", window_minutes)
    return f"attack={attacker}|victim={victim}|bucket={bucket}"


def priority_for_score(score: float) -> str:
    clean = _clamp_score(score)
    if clean >= 0.85:
        return "P0"
    if clean >= 0.7:
        return "P1"
    if clean >= 0.55:
        return "P2"
    return "P3"


def min_priority(left: str, right: str) -> str:
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    return left if order.get(left, 99) <= order.get(right, 99) else right


def _ensure_finding(value: Finding | Mapping[str, Any]) -> Finding:
    return value if isinstance(value, Finding) else Finding.from_dict(value)


def _ensure_case(value: CaseRecord | Mapping[str, Any]) -> CaseRecord:
    return value if isinstance(value, CaseRecord) else CaseRecord.from_dict(value)


def _title_for_finding(finding: Finding) -> str:
    victim = _primary_victim(finding) or "unknown asset"
    return f"{finding.detector_id}: {victim}"


def _primary_attacker(finding: Finding) -> str:
    values = _entity_values(finding.entities, "attacker_ip")
    return values[0] if values else ""


def _primary_victim(finding: Finding) -> str:
    values = _entity_values(finding.entities, "victim_ip", "dst_ip", "host", "asset_id")
    if values:
        return values[0]
    source_values = _entity_values(finding.entities, "src_ip")
    return source_values[0] if source_values and not _primary_attacker(finding) else ""


def _entity_values(entities: Mapping[str, Sequence[Any]], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        for value in entities.get(key, []):
            text = str(value or "").strip()
            if text and text not in values:
                values.append(text)
    return values


def _merge_entities(left: Mapping[str, Sequence[Any]], right: Mapping[str, Sequence[Any]]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {key: [str(value) for value in values] for key, values in left.items()}
    for key, values in right.items():
        merged.setdefault(key, [])
        merged[key].extend(str(value) for value in values)
    return _normalize_entities(merged)


def _normalize_entities(entities: Mapping[str, Sequence[Any]]) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    for key, values in entities.items():
        clean_key = str(key or "").strip()
        if not clean_key:
            continue
        normalized[clean_key] = _unique(str(value).strip() for value in values if str(value or "").strip())
    return {key: values for key, values in sorted(normalized.items()) if values}


def _merge_evidence_refs(left: Sequence[EvidenceRef], right: Sequence[EvidenceRef]) -> list[EvidenceRef]:
    merged: dict[str, EvidenceRef] = {}
    for ref in [*left, *right]:
        evidence = ref if isinstance(ref, EvidenceRef) else EvidenceRef.from_dict(ref)
        merged[evidence.evidence_id] = evidence
    return list(merged.values())


def _case_type_for_findings(findings: Sequence[Finding]) -> str:
    detector_ids = {finding.detector_id for finding in findings}
    if {"waf_attack_success_candidate", "web_to_process_anomaly", "rare_egress_after_alert"}.issubset(detector_ids):
        return "suspected_zero_day_intrusion"
    if "vpn_new_geo_login" in detector_ids or "bruteforce_then_success" in detector_ids:
        return "suspected_credential_intrusion"
    if "waf_attack_success_candidate" in detector_ids:
        return "web_intrusion_candidate"
    return "security_investigation"


def _unique(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _time_bucket(value: str, minutes: int) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return "unknown-time"
    minute = (parsed.minute // max(minutes, 1)) * max(minutes, 1)
    bucket = parsed.astimezone(timezone.utc).replace(minute=minute, second=0, microsecond=0)
    return bucket.isoformat().replace("+00:00", "Z")


def _bucket_from_dedup_key(value: str) -> str:
    for part in value.split("|"):
        if part.startswith("bucket="):
            return part.removeprefix("bucket=")
    return ""


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stable_case_id(seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"case-{today}-{digest[:10]}"


def _clamp_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, score))


__all__ = ["Case", "CaseStore", "dedup_key_for_finding", "min_priority", "priority_for_score"]
