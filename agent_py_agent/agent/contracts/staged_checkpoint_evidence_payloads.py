
from __future__ import annotations

from typing import Any

from ..common.value_parsing import sequence_strings
from .evidence_contract import EvidenceClaim, EvidenceSourceRef


def source_refs(value: object) -> list[EvidenceSourceRef]:
    if not isinstance(value, list):
        return []
    refs: list[EvidenceSourceRef] = []
    for item in value:
        if isinstance(item, dict):
            refs.append(_source_ref(item))
    return refs


def claims(value: object) -> list[EvidenceClaim]:
    if not isinstance(value, list):
        return []
    return [_claim(index, item) for index, item in enumerate(value) if isinstance(item, dict)]


def _source_ref(item: dict[str, Any]) -> EvidenceSourceRef:
    return EvidenceSourceRef(
        source_id=str(item.get("source_id") or ""),
        source_type=str(item.get("source_type") or ""),
        uri=str(item.get("uri") or ""),
        retrieved_at=str(item.get("retrieved_at") or ""),
        artifact_ref=str(item.get("artifact_ref") or ""),
        content_sha256=str(item.get("content_sha256") or ""),
        status=str(item.get("status") or "AVAILABLE"),
        reserved=dict(item.get("reserved")) if isinstance(item.get("reserved"), dict) else {},
    )


def _claim(index: int, item: dict[str, Any]) -> EvidenceClaim:
    return EvidenceClaim(
        claim_id=str(item.get("claim_id") or f"claim-{index}"),
        field=str(item.get("field") or ""),
        value=item.get("value"),
        source_ids=sequence_strings(item.get("source_ids")),
        confidence=float(item.get("confidence", 1.0) or 0.0),
        verification_status=str(item.get("verification_status") or "VERIFIED"),
        value_type=str(item.get("value_type") or "exact"),
        methodology=str(item.get("methodology") or ""),
        reserved=dict(item.get("reserved")) if isinstance(item.get("reserved"), dict) else {},
    )


__all__ = ["claims", "source_refs"]
