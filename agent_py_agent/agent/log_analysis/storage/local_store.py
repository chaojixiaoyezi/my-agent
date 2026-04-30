from __future__ import annotations

"""JSONL-backed local store for the first log-analysis milestone."""

import json
from pathlib import Path
from typing import Any, Iterable

from ...io import append_jsonl
from .base import (
    CASE_ID_FIELDS,
    EVENT_ID_FIELDS,
    EVIDENCE_ID_FIELDS,
    FINDING_ID_FIELDS,
    Case,
    EvidenceRef,
    Finding,
    NormalizedEvent,
    QueryRecord,
    canonical_json,
    default_log_analysis_root,
    model_to_dict,
    record_identity,
)


class LocalLogStore:
    """Small append-friendly store whose public contract can move to DuckDB later."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else default_log_analysis_root()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "evidence").mkdir(parents=True, exist_ok=True)

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def findings_path(self) -> Path:
        return self.root / "findings.jsonl"

    @property
    def cases_path(self) -> Path:
        return self.root / "cases.jsonl"

    @property
    def evidence_refs_path(self) -> Path:
        return self.root / "evidence_refs.jsonl"

    @property
    def queries_path(self) -> Path:
        return self.root / "queries.jsonl"

    def upsert_event(self, event: NormalizedEvent | dict[str, Any]) -> bool:
        return self._upsert_one(self.events_path, event, EVENT_ID_FIELDS)

    def upsert_events(self, events: Iterable[NormalizedEvent | dict[str, Any]]) -> int:
        return sum(1 for event in events if self.upsert_event(event))

    def list_events(self) -> list[dict[str, Any]]:
        return list(self._read_unique(self.events_path, EVENT_ID_FIELDS).values())

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        return self._read_unique(self.events_path, EVENT_ID_FIELDS).get(event_id)

    def upsert_finding(self, finding: Finding | dict[str, Any]) -> bool:
        return self._upsert_one(self.findings_path, finding, FINDING_ID_FIELDS)

    def upsert_findings(self, findings: Iterable[Finding | dict[str, Any]]) -> int:
        return sum(1 for finding in findings if self.upsert_finding(finding))

    def list_findings(self) -> list[dict[str, Any]]:
        return list(self._read_unique(self.findings_path, FINDING_ID_FIELDS).values())

    def get_finding(self, finding_id: str) -> dict[str, Any] | None:
        return self._read_unique(self.findings_path, FINDING_ID_FIELDS).get(finding_id)

    def upsert_case(self, case: Case | dict[str, Any]) -> bool:
        return self._upsert_one(self.cases_path, case, CASE_ID_FIELDS)

    def upsert_cases(self, cases: Iterable[Case | dict[str, Any]]) -> int:
        return sum(1 for case in cases if self.upsert_case(case))

    def list_cases(self) -> list[dict[str, Any]]:
        return list(self._read_unique(self.cases_path, CASE_ID_FIELDS).values())

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        return self._read_unique(self.cases_path, CASE_ID_FIELDS).get(case_id)

    def upsert_evidence_ref(self, ref: EvidenceRef | dict[str, Any]) -> bool:
        return self._upsert_one(self.evidence_refs_path, ref, EVIDENCE_ID_FIELDS)

    def upsert_evidence_refs(self, refs: Iterable[EvidenceRef | dict[str, Any]]) -> int:
        return sum(1 for ref in refs if self.upsert_evidence_ref(ref))

    def list_evidence_refs(self) -> list[dict[str, Any]]:
        return list(self._read_unique(self.evidence_refs_path, EVIDENCE_ID_FIELDS).values())

    def get_evidence_ref(self, evidence_id: str) -> dict[str, Any] | None:
        return self._read_unique(self.evidence_refs_path, EVIDENCE_ID_FIELDS).get(evidence_id)

    def save_query_record(self, record: QueryRecord | dict[str, Any]) -> None:
        append_jsonl(self.queries_path, model_to_dict(record), sort_keys=True)

    def list_query_records(self) -> list[dict[str, Any]]:
        return self._read_jsonl(self.queries_path)

    def _upsert_one(self, path: Path, record: Any, id_fields: tuple[str, ...]) -> bool:
        payload = model_to_dict(record)
        record_id = record_identity(payload, id_fields)
        existing = self._read_unique(path, id_fields)
        current = existing.get(record_id)
        if current is not None and canonical_json(current) == canonical_json(payload):
            return False
        append_jsonl(path, payload, sort_keys=True)
        return True

    def _read_unique(self, path: Path, id_fields: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for payload in self._read_jsonl(path):
            records[record_identity(payload, id_fields)] = payload
        return records

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            records.append(json.loads(line))
        return records
