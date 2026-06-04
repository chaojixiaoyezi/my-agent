
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
        tool_call_ref=str(item.get("tool_call_ref") or ""),
        tool_call_id=str(item.get("tool_call_id") or ""),
        operation_id=str(item.get("operation_id") or ""),
        tool_result_id=str(item.get("tool_result_id") or ""),
        http_status=_optional_int(item.get("http_status")),
        metric_kind=str(item.get("metric_kind") or ""),
        window_start=str(item.get("window_start") or ""),
        window_end=str(item.get("window_end") or ""),
        time_window=dict(item.get("time_window")) if isinstance(item.get("time_window"), dict) else {},
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
        metric_kind=str(item.get("metric_kind") or ""),
        observed_metric_kind=str(item.get("observed_metric_kind") or ""),
        window_start=str(item.get("window_start") or ""),
        window_end=str(item.get("window_end") or ""),
        time_window=dict(item.get("time_window")) if isinstance(item.get("time_window"), dict) else {},
        limitations=item.get("limitations") or "",
        uncertainty_notes=item.get("uncertainty_notes") or "",
        item_path=str(item.get("item_path") or ""),
        item_key=dict(item.get("item_key")) if isinstance(item.get("item_key"), dict) else {},
        item_index=_optional_int(item.get("item_index")),
        group_index=_optional_int(item.get("group_index")),
        group_name=str(item.get("group_name") or ""),
    )


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


__all__ = ["claims", "source_refs"]
