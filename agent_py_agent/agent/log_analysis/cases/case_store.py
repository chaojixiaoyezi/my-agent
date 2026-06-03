
from __future__ import annotations

"""Finding-to-case persistence on top of the local log-analysis store."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import CaseRecord, EvidenceRef, Finding, QueryPlan, utc_now_iso
from ..storage.local_store import LocalLogStore
from .case_helpers import (
    _bucket_from_dedup_key,
    _case_type_for_findings,
    _clamp_score,
    _ensure_case,
    _ensure_finding,
    _entity_values,
    _merge_entities,
    _merge_evidence_refs,
    _parse_time,
    _primary_account,
    _primary_attacker,
    _primary_victim,
    _stable_case_id,
    _time_bucket,
    _title_for_finding,
    _unique,
    _unique_values,
)

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
        case.next_queries = _unique_values([*case.next_queries, *finding.next_queries])
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
    if normalized.detector_id in {"vpn_new_geo_login", "bruteforce_then_success"}:
        account = _primary_account(normalized) or "unknown"
        return f"attack={attacker}|account={account}|victim={victim}|bucket={bucket}"
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


__all__ = ["Case", "CaseStore", "dedup_key_for_finding", "min_priority", "priority_for_score"]
